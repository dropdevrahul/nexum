//! Subprocess JSON client for the nexum Python engine.
//!
//! The TUI shells `python3 <scripts>/store.py … --json` and `worktree.py …`, so
//! the schema/logic stays owned by the Python engine and the Rust side is thin.

use crate::agent::Session;
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

    fn registry_path(&self) -> PathBuf {
        self.repo_root.join(".nexum-data").join("tui-agents.json")
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

    /// Create an isolated git worktree via worktree.py; returns its path.
    pub fn create_worktree(&self, slug: &str) -> Result<String> {
        let out = Command::new(&self.python)
            .arg(self.scripts_dir.join("worktree.py"))
            .args(["--create", "--repo", &self.repo_arg(), "--slug", slug])
            .output()
            .context("spawn worktree.py")?;
        let v: serde_json::Value =
            serde_json::from_str(String::from_utf8_lossy(&out.stdout).trim())
                .context("parse worktree.py output")?;
        v.get("worktree")
            .and_then(|x| x.as_str())
            .map(|s| s.to_string())
            .ok_or_else(|| anyhow!("worktree creation failed"))
    }
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
