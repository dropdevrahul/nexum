//! Row models: a live interactive agent (`from_proc`) or an observed plugin
//! session (`from_session`), normalized into one `Row` for the list.

use crate::pty::AgentProc;
use crate::store::AgentRecord;
use serde::Deserialize;

/// An observed plugin session (from `store.py session-list --json`).
#[derive(Debug, Clone, Deserialize)]
pub struct Session {
    pub session_id: String,
    pub task: Option<String>,
    pub cost_usd: Option<f64>,
    pub context_pct: Option<f64>,
    pub updated_ts: Option<f64>,
}

/// A row from the SQLite `agents` table (`store.py agent-list --json`): a
/// headless sub-agent delegated via dispatch.py / the delegation MCP. Extra
/// columns are ignored; every field is optional to stay forward-compatible.
#[derive(Debug, Clone, Deserialize)]
pub struct ManagedAgent {
    pub agent_id: String,
    pub harness: Option<String>,
    pub model: Option<String>,
    pub worktree: Option<String>,
    pub branch: Option<String>,
    pub status: Option<String>,
    pub cost_usd: Option<f64>,
    pub task: Option<String>,
    pub step_index: Option<i64>,
    pub updated_ts: Option<f64>,
    pub pid: Option<i64>,
    pub log_path: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Managed,
    Observed,
}

/// One normalized row for the unified list. Some fields are carried for future
/// use / observed sessions and aren't read on every path.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct Row {
    pub kind: Kind,
    pub id: String,
    pub harness: String,
    pub model: String,
    pub task: String,
    pub branch: String,
    pub status: String,
    pub cost_usd: f64,
    pub ctx_pct: Option<f64>,
    pub updated_ts: f64,
    pub pid: Option<i64>,
    pub worktree: Option<String>,
    pub log_path: Option<String>,
    pub plan_hash: Option<String>,
    pub session_id: Option<String>,
    pub step_index: Option<i64>,
    pub steps: Option<(u32, u32)>,
    /// True for an in-TUI interactive agent (has a live PTY terminal).
    pub interactive: bool,
    /// Index into `App.agents` when this row is a live PTY agent.
    pub proc_idx: Option<usize>,
    /// A persisted-but-not-running agent that can be resumed in its worktree.
    pub resumable: bool,
    /// Optional friendly label (from the registry) shown instead of the task.
    pub label: Option<String>,
}

impl Row {
    /// What to show in the list: the label if set, else the task.
    pub fn display(&self) -> &str {
        match &self.label {
            Some(l) if !l.is_empty() => l.as_str(),
            _ => &self.task,
        }
    }
}

fn s(o: &Option<String>, dflt: &str) -> String {
    match o {
        Some(v) if !v.is_empty() => v.clone(),
        _ => dflt.to_string(),
    }
}

impl Row {
    /// Build a row from a live in-TUI interactive agent (PTY-backed).
    pub fn from_proc(idx: usize, p: &mut AgentProc) -> Row {
        let alive = p.is_alive();
        Row {
            kind: Kind::Managed,
            id: p.id.clone(),
            harness: p.harness.clone(),
            model: p.model.clone(),
            task: p.task.clone(),
            branch: String::new(),
            status: if alive { "running".into() } else { "exited".into() },
            cost_usd: 0.0,
            ctx_pct: None,
            updated_ts: p.started, // show how long it's been running
            pid: None,
            worktree: Some(p.worktree.clone()),
            log_path: None,
            plan_hash: None,
            session_id: None,
            step_index: None,
            steps: None,
            interactive: true,
            proc_idx: Some(idx),
            resumable: false,
            label: None,
        }
    }

    /// A persisted agent (not currently running) that can be resumed.
    pub fn from_record(rec: &AgentRecord) -> Row {
        Row {
            kind: Kind::Managed,
            id: rec.id.clone(),
            harness: rec.harness.clone(),
            model: rec.model.clone(),
            task: rec.task.clone(),
            branch: String::new(),
            status: "resumable".into(),
            cost_usd: 0.0,
            ctx_pct: None,
            updated_ts: 0.0,
            pid: None,
            worktree: Some(rec.worktree.clone()),
            log_path: None,
            plan_hash: None,
            session_id: None,
            step_index: None,
            steps: None,
            interactive: false,
            proc_idx: None,
            resumable: true,
            label: rec.label.clone(),
        }
    }

    /// A headless sub-agent delegated via dispatch/MCP (SQLite `agents` row).
    pub fn from_managed(a: &ManagedAgent) -> Row {
        Row {
            kind: Kind::Managed,
            id: a.agent_id.clone(),
            harness: a.harness.clone().unwrap_or_else(|| "?".into()),
            model: a.model.clone().unwrap_or_default(),
            task: a.task.clone().unwrap_or_default(),
            branch: a.branch.clone().unwrap_or_default(),
            status: a.status.clone().unwrap_or_else(|| "exited".into()),
            cost_usd: a.cost_usd.unwrap_or(0.0),
            ctx_pct: None,
            updated_ts: a.updated_ts.unwrap_or(0.0),
            pid: a.pid,
            worktree: a.worktree.clone(),
            log_path: a.log_path.clone(),
            plan_hash: None,
            session_id: None,
            step_index: a.step_index,
            steps: None,
            interactive: false,
            proc_idx: None,
            resumable: false,
            label: None,
        }
    }

    pub fn from_session(sess: &Session) -> Row {
        Row {
            kind: Kind::Observed,
            id: sess.session_id.clone(),
            harness: "session".to_string(),
            model: "-".to_string(),
            task: s(&sess.task, ""),
            branch: "-".to_string(),
            status: "observed".to_string(),
            cost_usd: sess.cost_usd.unwrap_or(0.0),
            ctx_pct: sess.context_pct,
            updated_ts: sess.updated_ts.unwrap_or(0.0),
            pid: None,
            worktree: None,
            log_path: None,
            plan_hash: None,
            session_id: None,
            step_index: None,
            steps: None,
            interactive: false,
            proc_idx: None,
            resumable: false,
            label: None,
        }
    }
}

/// Human "3m ago" relative time from epoch seconds.
pub fn rel_time(ts: f64, now: f64) -> String {
    if ts <= 0.0 {
        return "-".to_string();
    }
    let d = (now - ts).max(0.0) as u64;
    if d < 60 {
        format!("{}s ago", d)
    } else if d < 3600 {
        format!("{}m ago", d / 60)
    } else if d < 86400 {
        format!("{}h ago", d / 3600)
    } else {
        format!("{}d ago", d / 86400)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rel_time_buckets() {
        assert_eq!(rel_time(0.0, 100.0), "-");
        assert_eq!(rel_time(90.0, 100.0), "10s ago");
        assert_eq!(rel_time(40.0, 100.0), "1m ago");
        assert_eq!(rel_time(100.0, 100.0 + 300.0), "5m ago");
        assert_eq!(rel_time(100.0, 100.0 + 7200.0), "2h ago");
    }

    #[test]
    fn live_row_carries_start_time() {
        let dir = std::env::temp_dir().to_string_lossy().to_string();
        let argv = vec!["cat".to_string()];
        let mut p = crate::pty::AgentProc::spawn(
            "t".into(), "stub".into(), "m".into(), String::new(), dir, &argv, 24, 80,
        ).unwrap();
        let r = Row::from_proc(0, &mut p);
        assert!(r.updated_ts > 0.0);
        assert_eq!(r.updated_ts, p.started);
        p.kill();
    }

    #[test]
    fn session_row_is_observed() {
        let sess = Session {
            session_id: "s1".into(),
            task: Some("observed work".into()),
            cost_usd: Some(1.0),
            context_pct: Some(42.0),
            updated_ts: Some(5.0),
        };
        let r = Row::from_session(&sess);
        assert_eq!(r.kind, Kind::Observed);
        assert!(!r.interactive);
        assert_eq!(r.ctx_pct, Some(42.0));
    }
}
