//! Persistence + optional engine integration.
//!
//! `crew` runs **standalone**: it manages its own agent registry (`.crew/
//! agents.json`) and creates git worktrees natively (no external process). If a
//! nexum Python engine is present alongside it (`store.py`/`dispatch.py`), crew
//! *additionally* surfaces that engine's observed sessions and headless
//! delegated agents — but nothing here requires it. Every engine call is
//! fail-open: absent engine → empty results, never an error.

use crate::agent::{ManagedAgent, Session};
use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::Command;

/// A launched agent persisted to disk so it survives a TUI restart. The harness
/// itself supports resume (claude/opencode/cursor), so relaunching in the same
/// worktree picks up where it left off.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentRecord {
    pub id: String,
    pub harness: String,
    pub model: String,
    pub worktree: String,
    pub task: String,
    /// Optional friendly label shown instead of the task in the list.
    #[serde(default)]
    pub label: Option<String>,
}

/// Remembered launch choices, persisted to `.crew/config.json` so the new-agent
/// form pre-selects what you used last time.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LaunchPrefs {
    pub harness_idx: usize,
    pub model_idx: usize,
    pub workflow_idx: usize,
    pub worktree_new: bool,
    // ── remembered UI state (serde defaults so old configs still load) ──
    #[serde(default = "yes")]
    pub show_observed: bool,
    #[serde(default)]
    pub sort: String, // "status" | "cost" | "recent"
    #[serde(default)]
    pub category: String, // "all" | "running" | "agents" | "sessions"
}

fn yes() -> bool {
    true
}

impl Default for LaunchPrefs {
    fn default() -> Self {
        LaunchPrefs {
            harness_idx: 0, model_idx: 0, workflow_idx: 0, worktree_new: true,
            show_observed: true, sort: String::new(), category: String::new(),
        }
    }
}

pub struct Store {
    /// Absolute repo root (git toplevel) — the scoping key for every query.
    pub repo_root: PathBuf,
    /// Directory containing store.py / worktree.py.
    pub scripts_dir: PathBuf,
    /// python interpreter to use.
    pub python: String,
}

impl Store {
    pub fn new(repo_root: PathBuf, scripts_dir: PathBuf) -> Store {
        let python = std::env::var("NEXUM_PYTHON").unwrap_or_else(|_| "python3".to_string());
        Store { repo_root, scripts_dir, python }
    }

    fn store_py(&self) -> PathBuf {
        self.scripts_dir.join("store.py")
    }

    /// True when the optional nexum Python engine is available (enables observed
    /// sessions, delegated-agent listing, and dispatch-based retry).
    pub fn has_engine(&self) -> bool {
        self.store_py().is_file()
    }

    fn repo_arg(&self) -> String {
        self.repo_root.to_string_lossy().to_string()
    }

    /// Observed plugin sessions for this repo (`session-list --repo <root> --json`).
    pub fn sessions(&self) -> Result<Vec<Session>> {
        let out = Command::new(&self.python)
            .arg(self.store_py())
            .args(["session-list", "--repo", &self.repo_arg(), "--json"])
            .output()
            .context("spawn store.py session-list")?;
        let stdout = String::from_utf8_lossy(&out.stdout);
        let v: Vec<Session> = serde_json::from_str(stdout.trim()).unwrap_or_default();
        Ok(v)
    }

    /// Headless/offloaded agents recorded in the SQLite `agents` table by
    /// dispatch.py — i.e. every sub-agent delegated via `/nx-build --harness` or
    /// the delegation MCP. This is how the TUI surfaces work it didn't spawn as a
    /// live PTY itself.
    pub fn managed_agents(&self) -> Vec<ManagedAgent> {
        Command::new(&self.python)
            .arg(self.store_py())
            .args(["agent-list", "--repo", &self.repo_arg(), "--json"])
            .output()
            .ok()
            .and_then(|out| {
                serde_json::from_str(String::from_utf8_lossy(&out.stdout).trim()).ok()
            })
            .unwrap_or_default()
    }

    fn config_path(&self) -> PathBuf {
        self.repo_root.join(".crew").join("config.json")
    }

    /// Load remembered launch defaults (defaults if absent/unparseable).
    pub fn load_prefs(&self) -> LaunchPrefs {
        std::fs::read_to_string(self.config_path())
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }

    /// Persist launch defaults (best-effort).
    pub fn save_prefs(&self, p: &LaunchPrefs) {
        let path = self.config_path();
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(j) = serde_json::to_string_pretty(p) {
            let _ = std::fs::write(path, j);
        }
    }

    /// Delete a headless agent row from the store (`agent-del`). Best-effort;
    /// a no-op for ids that aren't in the SQLite `agents` table.
    pub fn delete_agent(&self, id: &str) {
        let _ = Command::new(&self.python)
            .arg(self.store_py())
            .args(["agent-del", "--id", id])
            .output();
    }

    fn registry_path(&self) -> PathBuf {
        self.repo_root.join(".crew").join("agents.json")
    }

    /// Load the persisted (resumable) agent records for this repo.
    pub fn load_registry(&self) -> Vec<AgentRecord> {
        std::fs::read_to_string(self.registry_path())
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }

    /// Persist the agent registry (best-effort).
    pub fn save_registry(&self, records: &[AgentRecord]) {
        let path = self.registry_path();
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Ok(json) = serde_json::to_string_pretty(records) {
            let _ = std::fs::write(path, json);
        }
    }

    /// Create an isolated git worktree natively (no external engine): a new
    /// branch `crew/<slug>` at `<repo>/.crew/worktrees/<slug>`. If the branch
    /// already exists, checks it out into the worktree instead. Returns the path.
    pub fn create_worktree(&self, slug: &str) -> Result<String> {
        let dest = self.repo_root.join(".crew").join("worktrees").join(slug);
        if let Some(parent) = dest.parent() {
            std::fs::create_dir_all(parent).context("mkdir worktrees")?;
        }
        let dest_s = dest.to_string_lossy().to_string();
        let branch = format!("crew/{}", slug);
        // fresh branch off HEAD; on failure (branch exists) attach the existing one
        if matches!(self.git(&["worktree", "add", "-b", &branch, &dest_s]), Some((true, _))) {
            return Ok(dest_s);
        }
        let attach = self.git(&["worktree", "add", &dest_s, &branch]);
        match attach {
            Some((true, _)) => Ok(dest_s),
            Some((false, err)) => Err(anyhow!("git worktree add failed: {}", err.trim())),
            None => Err(anyhow!("could not run git")),
        }
    }

    /// Run a git command in the repo root; returns (success, stderr).
    fn git(&self, args: &[&str]) -> Option<(bool, String)> {
        let out = Command::new("git")
            .arg("-C").arg(&self.repo_root)
            .args(args)
            .output()
            .ok()?;
        Some((out.status.success(), String::from_utf8_lossy(&out.stderr).into_owned()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn git(repo: &Path, args: &[&str]) {
        Command::new("git").arg("-C").arg(repo).args(args).output().unwrap();
    }

    #[test]
    fn launch_prefs_round_trip() {
        let repo = std::env::temp_dir().join(format!("crew-prefs-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&repo);
        std::fs::create_dir_all(&repo).unwrap();
        let st = Store::new(repo.clone(), repo.join("scripts"));
        assert_eq!(st.load_prefs().harness_idx, 0); // default when absent
        st.save_prefs(&LaunchPrefs { harness_idx: 2, model_idx: 1, workflow_idx: 3, worktree_new: false, ..Default::default() });
        let p = st.load_prefs();
        assert_eq!((p.harness_idx, p.model_idx, p.workflow_idx, p.worktree_new), (2, 1, 3, false));
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn native_worktree_needs_no_engine() {
        let repo = std::env::temp_dir().join(format!("crew-wt-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&repo);
        std::fs::create_dir_all(&repo).unwrap();
        git(&repo, &["init", "-q"]);
        git(&repo, &["config", "user.email", "t@t"]);
        git(&repo, &["config", "user.name", "t"]);
        std::fs::write(repo.join("seed.txt"), "x").unwrap();
        git(&repo, &["add", "seed.txt"]);
        git(&repo, &["commit", "-qm", "init"]);

        let repo = std::fs::canonicalize(&repo).unwrap();
        // scripts dir intentionally missing → standalone, no engine
        let st = Store::new(repo.clone(), repo.join("no-such-scripts"));
        assert!(!st.has_engine(), "should be standalone");

        let wt = st.create_worktree("my-task").unwrap();
        assert!(std::path::Path::new(&wt).is_dir(), "worktree dir missing: {}", wt);
        assert!(wt.contains(".crew/worktrees/my-task"));
        // the branch crew/my-task exists
        let out = Command::new("git").arg("-C").arg(&repo)
            .args(["branch", "--list", "crew/my-task"]).output().unwrap();
        assert!(String::from_utf8_lossy(&out.stdout).contains("crew/my-task"));

        let _ = std::fs::remove_dir_all(&repo);
    }
}

/// True if `dir` is inside a git working tree.
pub fn is_git_repo(dir: &Path) -> bool {
    Command::new("git")
        .args(["-C", &dir.to_string_lossy(), "rev-parse", "--is-inside-work-tree"])
        .output()
        .map(|o| o.status.success() && String::from_utf8_lossy(&o.stdout).trim() == "true")
        .unwrap_or(false)
}

/// Resolve the git toplevel of `start` (falls back to `start` itself).
pub fn git_toplevel(start: &Path) -> PathBuf {
    let out = Command::new("git")
        .args(["-C", &start.to_string_lossy(), "rev-parse", "--show-toplevel"])
        .output();
    if let Ok(o) = out {
        if o.status.success() {
            let s = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if !s.is_empty() {
                return PathBuf::from(s);
            }
        }
    }
    start.to_path_buf()
}
