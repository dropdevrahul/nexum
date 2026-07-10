//! Application state and the actions the key handlers call.
//!
//! Interactive agents are PTY-backed processes owned here (`agents`), rendered
//! *inside* the TUI. Observed plugin sessions still come from the DB (read-only).

use crate::agent::Row;
use crate::pty::AgentProc;
use crate::store::{AgentRecord, Store};
use anyhow::Result;
use std::collections::{HashMap, HashSet};
use tui_textarea::TextArea;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Normal,
    NewAgent,
    Help,
    ConfirmStop,
    ConfirmStopAll,
    ConfirmQuit,
    /// Confirm committing + pushing the selected worktree (+ opening a PR).
    ConfirmPush,
    /// Edit persisted launch defaults.
    Settings,
    /// Confirm removing the marked agents (bulk).
    ConfirmRemove,
    /// Text entry to label the selected agent.
    Rename,
    /// Esc pressed in the new-agent form with unsaved input — confirm discard.
    ConfirmDiscard,
    Filter,
    /// Focused into the selected agent's embedded terminal — keys go to the PTY.
    Terminal,
    /// Scrollable view of the selected agent's worktree diff.
    Diff,
    /// Scrollable tail of a headless agent's log file.
    Log,
    /// Pick a harness to re-delegate (retry) the selected row's task on.
    Retry,
}

pub const HARNESSES: [&str; 3] = ["claude", "opencode", "cursor"];

/// Quick category filter (k9s-style number keys). Narrows the list to one class.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Category {
    All,
    Running,
    Agents,   // managed (nexum-launched / delegated), not observed sessions
    Sessions, // observed plugin sessions
}

impl Category {
    pub fn label(self) -> &'static str {
        match self {
            Category::All => "all",
            Category::Running => "running",
            Category::Agents => "agents",
            Category::Sessions => "sessions",
        }
    }
    pub fn from_key(s: &str) -> Category {
        match s {
            "running" => Category::Running,
            "agents" => Category::Agents,
            "sessions" => Category::Sessions,
            _ => Category::All,
        }
    }
}

/// Sort order for the list, cycled with `t`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SortKey {
    Status, // default: running → failed → resumable → done → exited → observed
    Cost,   // most expensive first
    Recent, // most recently updated first
}

impl SortKey {
    pub fn label(self) -> &'static str {
        match self {
            SortKey::Status => "status",
            SortKey::Cost => "cost",
            SortKey::Recent => "recent",
        }
    }
    pub fn next(self) -> SortKey {
        match self {
            SortKey::Status => SortKey::Cost,
            SortKey::Cost => SortKey::Recent,
            SortKey::Recent => SortKey::Status,
        }
    }
    pub fn from_key(s: &str) -> SortKey {
        match s {
            "cost" => SortKey::Cost,
            "recent" => SortKey::Recent,
            _ => SortKey::Status,
        }
    }
}

/// Execution workflows the launcher can seed. Index 0 = plain single agent; the
/// rest steer how a plan gets executed (which harness runs the build, or stop
/// after planning for review).
pub const WORKFLOWS: [&str; 5] = [
    "chat (single agent)",
    "plan → build (same harness)",
    "plan → build on cursor",
    "plan → build on opencode",
    "plan only (stop for review)",
];

/// Model presets per harness — pick with ←/→ instead of typing.
pub fn model_presets(harness_idx: usize) -> &'static [&'static str] {
    match harness_idx {
        0 => &["sonnet", "opus", "haiku"],
        1 => &["anthropic/claude-sonnet-4-6", "anthropic/claude-opus-4", "openai/gpt-5"],
        _ => &["auto", "sonnet", "gpt-5"],
    }
}

// New-agent form fields (task is a multiline editor and comes first).
pub const FIELD_TASK: usize = 0;
pub const FIELD_WORKFLOW: usize = 1;
pub const FIELD_HARNESS: usize = 2;
pub const FIELD_MODEL: usize = 3;
pub const FIELD_WORKTREE: usize = 4;
pub const FIELD_IMAGES: usize = 5;
pub const FIELD_COUNT: usize = 6;

pub struct NewAgentForm {
    pub task: TextArea<'static>,
    /// Index into WORKFLOWS: how the task is executed (plain chat, or plan→build
    /// steered to a harness, or plan-only for review).
    pub workflow_idx: usize,
    pub harness_idx: usize,
    pub model_idx: usize,
    /// true → isolate in a fresh git worktree; false → run in the repo itself.
    pub worktree_new: bool,
    /// image file paths attached to the task.
    pub images: Vec<String>,
    /// when Some, we're capturing an image path to add.
    pub image_input: Option<String>,
    pub field: usize,
    pub error: Option<String>,
}

impl Default for NewAgentForm {
    fn default() -> Self {
        let mut task = TextArea::default();
        task.set_placeholder_text("Describe the task… (Enter = newline)");
        NewAgentForm {
            task,
            workflow_idx: 0,
            harness_idx: 0,
            model_idx: 0,
            worktree_new: true,
            images: Vec::new(),
            image_input: None,
            field: FIELD_TASK,
            error: None,
        }
    }
}

impl NewAgentForm {
    pub fn harness(&self) -> &'static str {
        HARNESSES[self.harness_idx.min(2)]
    }
    pub fn model(&self) -> &'static str {
        let p = model_presets(self.harness_idx);
        p[self.model_idx.min(p.len() - 1)]
    }
    pub fn cycle_model(&mut self, d: i64) {
        let n = model_presets(self.harness_idx).len() as i64;
        self.model_idx = (((self.model_idx as i64 + d) % n + n) % n) as usize;
    }
    pub fn workflow(&self) -> &'static str {
        WORKFLOWS[self.workflow_idx.min(WORKFLOWS.len() - 1)]
    }
    pub fn cycle_workflow(&mut self, d: i64) {
        let n = WORKFLOWS.len() as i64;
        self.workflow_idx = (((self.workflow_idx as i64 + d) % n + n) % n) as usize;
    }
    pub fn cycle_harness(&mut self, d: i64) {
        let n = HARNESSES.len() as i64;
        self.harness_idx = (((self.harness_idx as i64 + d) % n + n) % n) as usize;
        self.model_idx = 0;
    }
    pub fn task_text(&self) -> String {
        self.task.lines().join("\n")
    }
    /// Full prompt sent to the agent: task wrapped in the selected workflow's
    /// seed instruction, plus any attached image paths.
    pub fn full_prompt(&self) -> String {
        let task = self.task_text();
        let mut s = match self.workflow_idx {
            1 => format!(
                "Run /nx-plan to decompose this into routed steps, then /nx-build to \
                 dispatch each step to the cheapest capable agent.\n\nTask: {}", task),
            2 => format!(
                "Run /nx-plan to decompose this, then /nx-build --harness cursor to \
                 execute every step on the cursor harness.\n\nTask: {}", task),
            3 => format!(
                "Run /nx-plan to decompose this, then /nx-build --harness opencode to \
                 execute every step on the opencode harness.\n\nTask: {}", task),
            4 => format!(
                "Run /nx-plan to decompose this into routed steps. STOP after planning \
                 — print the plan and wait for review; do not start execution.\n\nTask: {}", task),
            _ => task,
        };
        for img in &self.images {
            s.push_str(&format!("\n[image] {}", img));
        }
        s
    }
}

pub struct App {
    pub store: Store,
    /// Live PTY-backed interactive agents (owned; die with the TUI).
    pub agents: Vec<AgentProc>,
    /// Persisted agents (survive restart; resumable via the harness).
    pub registry: Vec<AgentRecord>,
    pub rows: Vec<Row>,
    pub selected: usize,
    pub mode: Mode,
    pub status_msg: String,
    pub new_form: NewAgentForm,
    pub should_quit: bool,
    pub show_observed: bool,
    pub all_rows: Vec<Row>,
    pub filter_text: String,
    pub category: Category,
    pub sort: SortKey,
    /// Size (cols, rows) the terminal pane was last rendered at — used to resize.
    pub term_size: (u16, u16),
    /// Scroll offset for the help overlay (fits short terminals).
    pub help_scroll: u16,
    /// Diff text + scroll offset for the Diff view.
    pub diff_text: String,
    pub diff_scroll: u16,
    /// Log text + scroll offset for the Log view.
    pub log_text: String,
    pub log_scroll: u16,
    /// Path of the log currently open in the Log view (for live follow).
    pub log_path_open: Option<String>,
    /// In-viewer (diff/log) incremental search: query + whether we're typing it.
    pub find_query: String,
    pub find_input: bool,
    /// Current git branch of the repo root (shown in the header, k9s-style).
    pub repo_branch: String,
    /// Retry modal state: the task to re-delegate + which harness is selected.
    pub retry_task: String,
    pub retry_harness_idx: usize,
    /// Multi-select: ids of marked rows for bulk stop/remove.
    pub marked: HashSet<String>,
    /// Queue of (worktree, branch, commit-msg) to commit+push (1 = selected,
    /// many = the marked set).
    pub push_queue: Vec<(String, String, String)>,
    /// Last-seen status per agent id — to notify on running→done/failed.
    pub prev_status: HashMap<String, String>,
    /// When set, the event loop suspends the TUI, runs (cwd, argv), then restores
    /// — used to open an editor or a shell in a worktree.
    pub pending_shell: Option<(String, Vec<String>)>,
    /// Working copy of launch defaults while the Settings modal is open.
    pub settings: Option<crate::store::LaunchPrefs>,
    pub settings_field: usize,
    /// Text buffer while labeling an agent (Mode::Rename).
    pub rename_buf: String,
    /// Cached git status of the selected worktree: (path, summary, is_clean).
    pub worktree_status: Option<(String, String, bool)>,
    /// Cached remote state of the selected branch (not pushed / ↑a ↓b / up to date).
    pub sel_remote: Option<String>,
    /// Open PRs by branch name → (number, url), polled from `gh` periodically.
    pub pr_map: HashMap<String, (i64, String)>,
    /// Per-agent-id: worktree has uncommitted changes (computed in refresh).
    pub dirty: HashMap<String, bool>,
}

impl App {
    pub fn new(store: Store) -> App {
        let registry = store.load_registry();
        let repo_branch = git_branch(&store.repo_root.to_string_lossy());
        let prefs = store.load_prefs(); // remembered UI state
        App {
            store,
            agents: Vec::new(),
            registry,
            rows: Vec::new(),
            selected: 0,
            mode: Mode::Normal,
            status_msg: String::new(),
            new_form: NewAgentForm::default(),
            should_quit: false,
            show_observed: prefs.show_observed,
            all_rows: Vec::new(),
            filter_text: String::new(),
            category: Category::from_key(&prefs.category),
            sort: SortKey::from_key(&prefs.sort),
            term_size: (80, 24),
            help_scroll: 0,
            diff_text: String::new(),
            diff_scroll: 0,
            log_text: String::new(),
            log_scroll: 0,
            log_path_open: None,
            find_query: String::new(),
            find_input: false,
            repo_branch,
            retry_task: String::new(),
            retry_harness_idx: 0,
            marked: HashSet::new(),
            push_queue: Vec::new(),
            prev_status: HashMap::new(),
            pending_shell: None,
            settings: None,
            settings_field: 0,
            rename_buf: String::new(),
            worktree_status: None,
            sel_remote: None,
            pr_map: HashMap::new(),
            dirty: HashMap::new(),
        }
    }

    /// Poll `gh` for open PRs and index them by head branch. Called on a slow
    /// timer (gh is a network call). No-op without gh / GitHub remote.
    pub fn poll_prs(&mut self) {
        if !gh_available() {
            return;
        }
        let repo = self.store.repo_root.to_string_lossy().to_string();
        let (ok, out, _) = run_in(&repo, "gh",
            &["pr", "list", "--state", "open", "--json", "number,headRefName,url", "--limit", "50"]);
        if !ok {
            return;
        }
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&out) {
            let mut m = HashMap::new();
            if let Some(arr) = v.as_array() {
                for it in arr {
                    if let (Some(br), Some(n)) =
                        (it["headRefName"].as_str(), it["number"].as_i64())
                    {
                        m.insert(br.to_string(), (n, it["url"].as_str().unwrap_or("").to_string()));
                    }
                }
            }
            self.pr_map = m;
        }
    }

    /// The open PR (number, url) for the selected agent's branch, if any.
    pub fn selected_pr(&self) -> Option<&(i64, String)> {
        let branch = self.selected_row()?.branch.clone();
        let branch = if branch.is_empty() { self.selected_branch()? } else { branch };
        self.pr_map.get(&branch)
    }

    /// Refresh the cached git status for the selected worktree (cheap: 1–2 git
    /// calls; called on refresh and on selection move).
    /// Per-row uncommitted-changes flags (heavier: one `git status` per row).
    /// Called on a slow timer, capped, and skipped while chatting.
    pub fn compute_dirty(&mut self) {
        self.dirty.clear();
        for r in self.all_rows.iter().take(20) {
            if let Some(wt) = &r.worktree {
                self.dirty.insert(r.id.clone(), !worktree_clean(wt));
            }
        }
    }

    pub fn update_sel_status(&mut self) {
        let wt = self.selected_row().and_then(|r| r.worktree.clone());
        self.worktree_status = wt.as_ref().map(|wt| {
            let (sum, clean) = worktree_summary(wt);
            (wt.clone(), sum, clean)
        });
        self.sel_remote = wt.as_deref().map(remote_state);
    }

    // ── rename / label ──────────────────────────────────────────────────────
    /// Open the label editor for the selected crew-launched agent.
    pub fn open_rename(&mut self) {
        let Some(r) = self.selected_row().cloned() else { return };
        if !self.registry.iter().any(|rec| rec.id == r.id) {
            self.status_msg = "only crew-launched agents can be labeled".into();
            return;
        }
        self.rename_buf = r.label.clone().unwrap_or_default();
        self.mode = Mode::Rename;
    }
    /// Save the label onto the registry entry (empty clears it).
    pub fn commit_rename(&mut self) {
        let Some(id) = self.selected_row().map(|r| r.id.clone()) else {
            self.mode = Mode::Normal;
            return;
        };
        let label = self.rename_buf.trim().to_string();
        if let Some(rec) = self.registry.iter_mut().find(|r| r.id == id) {
            rec.label = if label.is_empty() { None } else { Some(label) };
            self.store.save_registry(&self.registry);
        }
        self.mode = Mode::Normal;
        let _ = self.refresh();
    }

    // ── settings (persisted launch defaults) ────────────────────────────────
    pub fn open_settings(&mut self) {
        self.settings = Some(self.store.load_prefs());
        self.settings_field = 0;
        self.mode = Mode::Settings;
    }
    pub fn settings_move(&mut self, d: i64) {
        self.settings_field = (((self.settings_field as i64 + d) % 4 + 4) % 4) as usize;
    }
    pub fn settings_change(&mut self, d: i64) {
        let Some(p) = self.settings.as_mut() else { return };
        match self.settings_field {
            0 => {
                let n = HARNESSES.len() as i64;
                p.harness_idx = (((p.harness_idx as i64 + d) % n + n) % n) as usize;
                p.model_idx = 0; // model list depends on harness
            }
            1 => {
                let n = model_presets(p.harness_idx).len() as i64;
                p.model_idx = (((p.model_idx as i64 + d) % n + n) % n) as usize;
            }
            2 => {
                let n = WORKFLOWS.len() as i64;
                p.workflow_idx = (((p.workflow_idx as i64 + d) % n + n) % n) as usize;
            }
            _ => p.worktree_new = !p.worktree_new,
        }
    }
    pub fn save_settings(&mut self) {
        if let Some(mut p) = self.settings.take() {
            p.model_idx = p.model_idx.min(model_presets(p.harness_idx).len() - 1);
            self.store.save_prefs(&p);
            self.status_msg = "settings saved".into();
        }
        self.mode = Mode::Normal;
    }
    pub fn cancel_settings(&mut self) {
        self.settings = None;
        self.mode = Mode::Normal;
    }

    /// Request opening the selected worktree in `$EDITOR` (default `vi`).
    pub fn open_editor(&mut self) {
        let Some(wt) = self.selected_row().and_then(|r| r.worktree.clone()) else {
            self.status_msg = "no worktree to open".into();
            return;
        };
        let ed = std::env::var("EDITOR").unwrap_or_else(|_| "vi".into());
        // split EDITOR so values like "code -w" work; then the worktree path
        let mut argv: Vec<String> = ed.split_whitespace().map(String::from).collect();
        argv.push(wt.clone());
        self.pending_shell = Some((wt, argv));
    }

    /// Request a `$SHELL` (default `bash`) in the selected worktree.
    pub fn open_shell(&mut self) {
        let Some(wt) = self.selected_row().and_then(|r| r.worktree.clone()) else {
            self.status_msg = "no worktree for a shell".into();
            return;
        };
        let sh = std::env::var("SHELL").unwrap_or_else(|_| "bash".into());
        self.pending_shell = Some((wt, vec![sh]));
    }

    /// Detect agents that just finished (running → done/failed) since the last
    /// refresh and surface a toast. Updates the remembered statuses.
    fn notify_completions(&mut self) {
        let mut just_done: Vec<(&str, &str)> = Vec::new();
        for r in &self.all_rows {
            if let Some(prev) = self.prev_status.get(&r.id) {
                if prev == "running" && (r.status == "done" || r.status == "failed") {
                    just_done.push((r.status.as_str(), r.task.as_str()));
                }
            }
        }
        if let Some((status, task)) = just_done.last() {
            let icon = if *status == "done" { "✓" } else { "✗" };
            let short: String = task.chars().take(40).collect();
            let more = if just_done.len() > 1 { format!(" (+{} more)", just_done.len() - 1) } else { String::new() };
            self.status_msg = format!("{} {} — {}{}", icon, status, short, more);
        }
        self.prev_status = self.all_rows.iter().map(|r| (r.id.clone(), r.status.clone())).collect();
    }

    pub fn apply_filter(&mut self) {
        let q = self.filter_text.to_lowercase();
        let cat = self.category;
        let in_cat = |r: &Row| match cat {
            Category::All => true,
            Category::Running => r.status == "running",
            Category::Agents => r.kind == crate::agent::Kind::Managed,
            Category::Sessions => r.kind == crate::agent::Kind::Observed,
        };
        let matches_text = |r: &Row| {
            q.is_empty()
                || r.display().to_lowercase().contains(&q)
                || r.task.to_lowercase().contains(&q)
                || r.harness.to_lowercase().contains(&q)
                || r.id.to_lowercase().contains(&q)
                || r.status.to_lowercase().contains(&q)
        };
        let mut rows: Vec<Row> = self
            .all_rows
            .iter()
            .filter(|r| in_cat(r) && matches_text(r))
            .cloned()
            .collect();
        // all_rows is already status-sorted (merge_keep); only re-sort for others.
        match self.sort {
            SortKey::Status => {}
            SortKey::Cost => rows.sort_by(|a, b| b.cost_usd.total_cmp(&a.cost_usd)),
            SortKey::Recent => rows.sort_by(|a, b| b.updated_ts.total_cmp(&a.updated_ts)),
        }
        self.rows = rows;
        if self.selected >= self.rows.len() && !self.rows.is_empty() {
            self.selected = self.rows.len() - 1;
        }
    }

    pub fn set_category(&mut self, c: Category) {
        self.category = c;
        self.selected = 0;
        self.apply_filter();
    }

    pub fn cycle_sort(&mut self) {
        self.sort = self.sort.next();
        self.apply_filter();
        self.status_msg = format!("sort: {}", self.sort.label());
    }

    pub fn refresh(&mut self) -> Result<()> {
        // remember which agent is selected so the cursor follows it across a
        // re-sort (statuses change → row order changes) instead of jumping.
        let sel_id = self.selected_row().map(|r| r.id.clone());
        // live interactive agents first (from the owned PTY procs)
        let mut rows: Vec<Row> = self
            .agents
            .iter_mut()
            .enumerate()
            .map(|(i, p)| Row::from_proc(i, p))
            .collect();
        let mut seen: HashSet<String> = rows.iter().map(|r| r.id.clone()).collect();
        // persisted agents that aren't currently live → resumable
        for rec in &self.registry {
            if seen.insert(rec.id.clone()) {
                rows.push(Row::from_record(rec));
            }
        }
        // headless sub-agents delegated via dispatch/MCP (SQLite agents table)
        for a in self.store.managed_agents() {
            if seen.insert(a.agent_id.clone()) {
                rows.push(Row::from_managed(&a));
            }
        }
        // observed plugin sessions (read-only, from the DB)
        if self.show_observed {
            let sessions = self.store.sessions().unwrap_or_default();
            rows.extend(sessions.iter().map(Row::from_session));
        }
        rows = merge_keep(rows);
        self.all_rows = rows;
        // overlay registry labels onto live rows (from_record already carries it)
        for r in &mut self.all_rows {
            if r.label.is_none() {
                if let Some(rec) = self.registry.iter().find(|x| x.id == r.id) {
                    r.label = rec.label.clone();
                }
            }
        }
        self.notify_completions();
        self.apply_filter();
        // re-anchor the cursor on the same agent if it's still visible
        if let Some(id) = sel_id {
            if let Some(i) = self.rows.iter().position(|r| r.id == id) {
                self.selected = i;
            }
        }
        // One or two git calls for the selected worktree only — cheap. (The
        // per-row dirty scan is heavier and runs on its own slow timer.) Skipped
        // while chatting to keep the PTY responsive.
        if self.mode != Mode::Terminal {
            self.update_sel_status();
        }
        Ok(())
    }

    pub fn selected_row(&self) -> Option<&Row> {
        self.rows.get(self.selected)
    }

    /// The AgentProc behind the selected row, if it's a live interactive agent.
    pub fn selected_proc(&mut self) -> Option<&mut AgentProc> {
        let idx = self.selected_row().and_then(|r| r.proc_idx)?;
        self.agents.get_mut(idx)
    }

    pub fn move_sel(&mut self, delta: i64) {
        if self.rows.is_empty() {
            return;
        }
        let n = self.rows.len() as i64;
        let i = (self.selected as i64 + delta).clamp(0, n - 1);
        self.selected = i as usize;
        // NB: don't probe git here — scrolling must stay smooth. The selected
        // worktree's status refreshes on the next tick (≤800ms).
    }

    pub fn sel_top(&mut self) {
        self.selected = 0;
    }

    /// Jump to the next/prev failed agent (triage), wrapping around.
    pub fn jump_failed(&mut self, dir: i64) {
        let n = self.rows.len();
        if n == 0 {
            return;
        }
        for step in 1..=n {
            let i = (self.selected as i64 + dir * step as i64).rem_euclid(n as i64) as usize;
            if self.rows[i].status == "failed" {
                self.selected = i;
                return;
            }
        }
        self.status_msg = "no failed agents".into();
    }
    pub fn sel_bottom(&mut self) {
        if !self.rows.is_empty() {
            self.selected = self.rows.len() - 1;
        }
    }

    /// Copy the selected agent's worktree path to the system clipboard.
    pub fn yank_worktree(&mut self) {
        let Some(wt) = self.selected_row().and_then(|r| r.worktree.clone()) else {
            self.status_msg = "no worktree to copy".into();
            return;
        };
        self.yank_text(&wt, "worktree path");
    }

    /// Copy the selected agent's branch name to the clipboard.
    pub fn yank_branch(&mut self) {
        let Some(branch) = self.selected_branch() else {
            self.status_msg = "no branch to copy".into();
            return;
        };
        self.yank_text(&branch, "branch");
    }

    /// Branch of the selected row: the stored branch, else read from its worktree.
    fn selected_branch(&self) -> Option<String> {
        let r = self.selected_row()?;
        if !r.branch.is_empty() && r.branch != "-" {
            return Some(r.branch.clone());
        }
        let wt = r.worktree.as_ref()?;
        let b = git_out(wt, &["rev-parse", "--abbrev-ref", "HEAD"]);
        if b.is_empty() { None } else { Some(b) }
    }

    /// Copy `text` to the clipboard (`pbcopy`/`xclip`/`wl-copy`). Best-effort.
    fn yank_text(&mut self, text: &str, label: &str) {
        let candidates: [&[&str]; 3] =
            [&["pbcopy"], &["xclip", "-selection", "clipboard"], &["wl-copy"]];
        for argv in candidates {
            use std::io::Write;
            use std::process::{Command, Stdio};
            if let Ok(mut child) = Command::new(argv[0]).args(&argv[1..])
                .stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()
            {
                if let Some(mut si) = child.stdin.take() {
                    let _ = si.write_all(text.as_bytes());
                }
                let _ = child.wait();
                self.status_msg = format!("copied {} ({})", label, argv[0]);
                return;
            }
        }
        self.status_msg = "no clipboard tool (pbcopy/xclip/wl-copy) found".into();
    }

    /// Fetch origin for the selected worktree (detached — never blocks the UI).
    /// The ahead/behind indicator refreshes on the next status tick.
    pub fn fetch_selected(&mut self) {
        let Some(wt) = self.selected_row().and_then(|r| r.worktree.clone()) else {
            self.status_msg = "no worktree to fetch".into();
            return;
        };
        use std::process::{Command, Stdio};
        let ok = Command::new("git").current_dir(&wt).args(["fetch", "--quiet"])
            .stdout(Stdio::null()).stderr(Stdio::null()).stdin(Stdio::null()).spawn().is_ok();
        self.status_msg = if ok { "fetching origin…".into() } else { "git fetch failed to start".into() };
    }

    /// Open the selected agent's PR (if known) or its branch on the remote.
    pub fn open_branch_web(&mut self) {
        // prefer the actual PR page when we have one
        if let Some((n, url)) = self.selected_pr().cloned() {
            if !url.is_empty() && open_url(&url) {
                self.status_msg = format!("opening PR #{}", n);
                return;
            }
        }
        if !gh_available() {
            self.status_msg = "open-in-browser needs the gh CLI".into();
            return;
        }
        let Some(r) = self.selected_row().cloned() else { return };
        let Some(wt) = r.worktree.clone() else {
            self.status_msg = "no worktree".into();
            return;
        };
        let Some(branch) = self.selected_branch() else {
            self.status_msg = "no branch to open".into();
            return;
        };
        use std::process::{Command, Stdio};
        let ok = Command::new("gh").args(["browse", "--branch", &branch])
            .current_dir(&wt).stdout(Stdio::null()).stderr(Stdio::null()).spawn().is_ok();
        self.status_msg = if ok {
            format!("opening {} in browser", branch)
        } else {
            "could not launch gh browse".into()
        };
    }

    pub fn open_new_agent(&mut self) {
        let mut form = NewAgentForm::default();
        // pre-select whatever you launched with last time
        let p = self.store.load_prefs();
        let hn = HARNESSES.len();
        form.harness_idx = p.harness_idx.min(hn - 1);
        form.model_idx = p.model_idx.min(model_presets(form.harness_idx).len() - 1);
        form.workflow_idx = p.workflow_idx.min(WORKFLOWS.len() - 1);
        form.worktree_new = p.worktree_new;
        self.new_form = form;
        self.mode = Mode::NewAgent;
    }

    /// Open the new-agent form prefilled from the selected row (task + harness +
    /// model), so you can tweak and relaunch a variant.
    pub fn duplicate_selected(&mut self) {
        let Some(r) = self.selected_row().cloned() else { return };
        let mut form = NewAgentForm::default();
        form.task.insert_str(&r.task);
        form.harness_idx = HARNESSES.iter().position(|h| *h == r.harness).unwrap_or(0);
        form.model_idx = model_presets(form.harness_idx)
            .iter().position(|m| *m == r.model).unwrap_or(0);
        self.new_form = form;
        self.mode = Mode::NewAgent;
    }

    // ── git: commit + push + PR (lazygit/claude-squad style) ────────────────
    /// Queue the selected agent — or all marked agents — for commit+push+PR.
    pub fn open_push(&mut self) {
        let rows: Vec<Row> = if !self.marked.is_empty() {
            self.rows.iter().filter(|r| self.marked.contains(&r.id)).cloned().collect()
        } else {
            self.selected_row().cloned().into_iter().collect()
        };
        let mut queue = Vec::new();
        for r in rows {
            if let Some(wt) = r.worktree.clone() {
                let branch = git_out(&wt, &["rev-parse", "--abbrev-ref", "HEAD"]);
                let branch = if branch.is_empty() { "HEAD".into() } else { branch };
                let msg = if r.task.trim().is_empty() { "crew: work".into() } else { r.task.clone() };
                queue.push((wt, branch, msg));
            }
        }
        if queue.is_empty() {
            self.status_msg = "no worktree to push".into();
            return;
        }
        self.push_queue = queue;
        self.mode = Mode::ConfirmPush;
    }

    /// Commit + push every queued worktree, opening a PR with `gh` per branch.
    pub fn confirm_push(&mut self) {
        let queue = std::mem::take(&mut self.push_queue);
        let gh = on_path("gh");
        let (mut pushed, mut prs, mut failed) = (0, 0, 0);
        let mut last = String::new();
        for (wt, branch, task) in &queue {
            let msg = task.lines().next().unwrap_or("crew: work");
            let _ = run_in(wt, "git", &["add", "-A"]);
            let _ = run_in(wt, "git", &["commit", "-m", msg]); // may be empty; ok
            let (ok, _o, perr) = run_in(wt, "git", &["push", "-u", "origin", branch]);
            if !ok {
                failed += 1;
                last = format!("{}: {}", branch, perr.lines().last().unwrap_or("").trim());
                continue;
            }
            pushed += 1;
            last = branch.clone();
            if gh {
                let (pok, pout, _perr) = run_in(wt, "gh", &["pr", "create", "--fill", "--head", branch]);
                if pok {
                    prs += 1;
                    last = pout.trim().to_string();
                }
            }
        }
        self.status_msg = if queue.len() == 1 && failed == 0 {
            if prs > 0 { format!("pushed + PR: {}", last) } else { format!("pushed {}", last) }
        } else if failed == 0 {
            format!("pushed {} · {} PR(s)", pushed, prs)
        } else {
            format!("pushed {} · {} failed ({})", pushed, failed, last)
        };
        self.marked.clear();
        self.mode = Mode::Normal;
        let _ = self.refresh();
    }

    /// Open the retry modal for the selected managed row: re-delegate its task to
    /// a harness you pick (defaults to the row's current harness).
    pub fn open_retry(&mut self) {
        if !self.store.has_engine() {
            self.status_msg = "retry needs the nexum engine (dispatch.py) — not present".into();
            return;
        }
        let Some(r) = self.selected_row().cloned() else { return };
        if r.kind != crate::agent::Kind::Managed || r.task.trim().is_empty() {
            self.status_msg = "select a managed agent with a task to retry".into();
            return;
        }
        self.retry_task = r.task.clone();
        self.retry_harness_idx = HARNESSES.iter().position(|h| *h == r.harness).unwrap_or(0);
        self.mode = Mode::Retry;
    }

    pub fn cycle_retry_harness(&mut self, d: i64) {
        let n = HARNESSES.len() as i64;
        self.retry_harness_idx = (((self.retry_harness_idx as i64 + d) % n + n) % n) as usize;
    }

    /// Fire the retry: dispatch the task headless on the chosen harness in a
    /// fresh worktree (detached — shows up as a new managed row).
    pub fn confirm_retry(&mut self) {
        let harness = HARNESSES[self.retry_harness_idx.min(HARNESSES.len() - 1)];
        let model = model_presets(self.retry_harness_idx)[0];
        let task = self.retry_task.clone();
        match self.dispatch_detached(&task, harness, model) {
            Ok(id) => self.status_msg = format!("retrying on {} → {}", harness, id),
            Err(e) => self.status_msg = format!("retry failed: {}", e),
        }
        self.mode = Mode::Normal;
        let _ = self.refresh();
    }

    /// Spawn dispatch.py detached to run `task` on `harness` in a new worktree.
    /// Returns the chosen agent id. Mirrors the delegation MCP's async path.
    fn dispatch_detached(&self, task: &str, harness: &str, model: &str) -> Result<String> {
        use std::process::{Command, Stdio};
        let id = format!("retry_{}", now_millis());
        let steps_dir = self.store.repo_root.join(".nexum-data").join("steps");
        std::fs::create_dir_all(&steps_dir)?;
        let step_path = steps_dir.join(format!("{}.json", id));
        let step = serde_json::json!({
            "title": task.chars().take(80).collect::<String>(),
            "objective": task, "contract": "", "scope_deny": [],
            "acceptance": "", "files": [],
        });
        std::fs::write(&step_path, serde_json::to_string(&step)?)?;
        let slug = slugify(task);
        Command::new(&self.store.python)
            .arg(self.store.scripts_dir.join("dispatch.py"))
            .args(["--harness", harness, "--model", model, "--repo"])
            .arg(&self.store.repo_root)
            .args(["--new-worktree", "--slug", &slug, "--agent-id", &id, "--step-file"])
            .arg(&step_path)
            .stdout(Stdio::null()).stderr(Stdio::null()).stdin(Stdio::null())
            .spawn()?;
        Ok(id)
    }

    /// Launch an interactive PTY agent from the form. Task is required.
    pub fn spawn_from_form(&mut self) {
        if self.new_form.task_text().trim().is_empty() {
            self.new_form.error = Some("task is required".into());
            self.new_form.field = FIELD_TASK;
            return;
        }
        let harness = self.new_form.harness().to_string();
        // NEXUM_INTERACTIVE_CMD_<H> override means the CLI needn't be on PATH (tests)
        let overridden = std::env::var(format!("NEXUM_INTERACTIVE_CMD_{}", harness.to_uppercase()))
            .map(|v| !v.trim().is_empty()).unwrap_or(false);
        if !overridden && !harness_installed(&harness) {
            self.new_form.error = Some(format!(
                "{} CLI ('{}') not found on PATH", harness, harness_bin(&harness)));
            self.new_form.field = FIELD_HARNESS;
            return;
        }
        let model = self.new_form.model().to_string();
        let prompt = self.new_form.full_prompt();
        let (cols, rows) = self.term_size;

        // choose worktree: new isolated one, or the repo itself
        let (id, worktree) = if self.new_form.worktree_new {
            let slug = slugify(&self.new_form.task_text());
            match self.store.create_worktree(&slug) {
                Ok(wt) => (format!("agent_{}", slug), wt),
                Err(e) => {
                    self.new_form.error = Some(format!("worktree failed: {}", e));
                    return;
                }
            }
        } else {
            (
                format!("agent_{}", slugify(&self.new_form.task_text())),
                self.store.repo_root.to_string_lossy().to_string(),
            )
        };

        let argv = interactive_argv(&harness, &model);
        let task_text = self.new_form.task_text();
        match AgentProc::spawn(
            id.clone(), harness.clone(), model.clone(), prompt, worktree.clone(), &argv, rows, cols,
        ) {
            Ok(proc) => {
                self.agents.push(proc);
                // persist so it can be resumed after the TUI closes
                self.registry.retain(|r| r.id != id);
                self.registry.push(AgentRecord {
                    id: id.clone(), harness, model, worktree, task: task_text, label: None,
                });
                self.store.save_registry(&self.registry);
                // remember these launch choices (preserving stored UI state)
                let mut p = self.store.load_prefs();
                p.harness_idx = self.new_form.harness_idx;
                p.model_idx = self.new_form.model_idx;
                p.workflow_idx = self.new_form.workflow_idx;
                p.worktree_new = self.new_form.worktree_new;
                self.store.save_prefs(&p);
                let _ = self.refresh();
                if let Some(i) = self.rows.iter().position(|r| r.id == id) {
                    self.selected = i;
                }
                self.mode = Mode::Terminal; // drop straight into the chat
                self.status_msg = format!("launched {} — Ctrl-o to leave the terminal", id);
            }
            Err(e) => self.new_form.error = Some(format!("{}", e)),
        }
    }

    /// Enter/activate the selected row: focus a live terminal, or resume a
    /// persisted agent by relaunching its harness (which resumes its session)
    /// in the same worktree.
    pub fn open_selected(&mut self) {
        let Some(r) = self.selected_row().cloned() else { return };
        if r.interactive {
            self.mode = Mode::Terminal;
        } else if r.resumable {
            self.resume(&r.id);
        } else {
            self.status_msg = "not an interactive agent".into();
        }
    }

    fn resume(&mut self, id: &str) {
        let Some(rec) = self.registry.iter().find(|r| r.id == id).cloned() else { return };
        let (cols, rows) = self.term_size;
        let argv = resume_argv(&rec.harness, &rec.model);
        // no task seed on resume — the harness restores the prior conversation
        match AgentProc::spawn(
            rec.id.clone(), rec.harness.clone(), rec.model.clone(), String::new(),
            rec.worktree.clone(), &argv, rows, cols,
        ) {
            Ok(proc) => {
                self.agents.push(proc);
                let _ = self.refresh();
                if let Some(i) = self.rows.iter().position(|r| r.id == rec.id) {
                    self.selected = i;
                }
                self.mode = Mode::Terminal;
                self.status_msg = format!("resumed {} — Ctrl-o to leave", rec.id);
            }
            Err(e) => self.status_msg = format!("resume failed: {}", e),
        }
    }

    /// Forward raw bytes (a keypress) to the focused agent's PTY.
    pub fn send_to_terminal(&mut self, bytes: &[u8]) {
        if let Some(p) = self.selected_proc() {
            p.send(bytes);
        }
    }

    pub fn stop_selected(&mut self) {
        let ids = self.target_ids();
        let mut n = 0;
        for p in &mut self.agents {
            if ids.contains(&p.id) && p.is_alive() {
                p.kill();
                n += 1;
            }
        }
        self.marked.clear();
        let _ = self.refresh();
        self.status_msg = if n > 0 {
            format!("stopped {}", n)
        } else {
            "no live agents in selection".into()
        };
    }

    // ── multi-select ──────────────────────────────────────────────────────
    /// Toggle the mark on the selected row and advance (fast bulk marking).
    pub fn toggle_mark(&mut self) {
        if let Some(id) = self.selected_row().map(|r| r.id.clone()) {
            if !self.marked.remove(&id) {
                self.marked.insert(id);
            }
        }
        self.move_sel(1);
    }
    pub fn mark_all_visible(&mut self) {
        for r in &self.rows {
            self.marked.insert(r.id.clone());
        }
        self.status_msg = format!("{} marked", self.marked.len());
    }
    pub fn clear_marks(&mut self) {
        self.marked.clear();
    }
    /// Ids to act on: the marked set (intersected with visible rows) if any,
    /// else just the selected row.
    fn target_ids(&self) -> HashSet<String> {
        if !self.marked.is_empty() {
            let visible: HashSet<&String> = self.rows.iter().map(|r| &r.id).collect();
            self.marked.iter().filter(|id| visible.contains(id)).cloned().collect()
        } else {
            self.selected_row().map(|r| r.id.clone()).into_iter().collect()
        }
    }

    /// Remove the selected agent (or all marked): kill any live process, forget
    /// the persisted record, and delete the store row. Worktrees stay on disk.
    pub fn remove_selected(&mut self) {
        let ids = self.target_ids();
        if ids.is_empty() {
            return;
        }
        // capture worktrees before the rows disappear, to prune clean ones after
        let worktrees: Vec<String> = self
            .rows
            .iter()
            .filter(|r| ids.contains(&r.id))
            .filter_map(|r| r.worktree.clone())
            .collect();
        // kill + drop any live PTY procs in the set
        for p in &mut self.agents {
            if ids.contains(&p.id) {
                p.kill();
            }
        }
        self.agents.retain(|p| !ids.contains(&p.id));
        let before = self.registry.len();
        self.registry.retain(|rec| !ids.contains(&rec.id));
        if self.registry.len() != before {
            self.store.save_registry(&self.registry);
        }
        // drop any headless/managed rows from the store too
        for id in &ids {
            self.store.delete_agent(id);
        }
        // prune crew worktrees that are clean (leave anything with changes)
        let repo = self.store.repo_root.to_string_lossy().to_string();
        let mut pruned = 0;
        for wt in &worktrees {
            if wt.contains("/.crew/worktrees/") && worktree_clean(wt) {
                if run_in(&repo, "git", &["worktree", "remove", wt]).0 {
                    pruned += 1;
                }
            }
        }
        let n = ids.len();
        self.marked.clear();
        let _ = self.refresh();
        self.status_msg = if pruned > 0 {
            format!("removed {} · pruned {} clean worktree(s)", n, pruned)
        } else {
            format!("removed {}", n)
        };
    }

    pub fn show_worktree(&mut self) {
        if let Some(r) = self.selected_row() {
            self.status_msg = r.worktree.clone().unwrap_or_else(|| "no worktree".into());
        }
    }

    /// Show the git diff of the selected agent's worktree (tracked changes plus a
    /// list of untracked files). Read-only — never mutates the checkout.
    pub fn show_diff(&mut self) {
        let Some(wt) = self.selected_row().and_then(|r| r.worktree.clone()) else {
            self.status_msg = "no worktree for this row".into();
            return;
        };
        self.diff_text = worktree_diff(&wt);
        self.diff_scroll = 0;
        self.mode = Mode::Diff;
    }

    pub fn scroll_diff(&mut self, delta: i64) {
        let max = self.diff_text.lines().count().saturating_sub(1) as i64;
        self.diff_scroll = (self.diff_scroll as i64).saturating_add(delta).clamp(0, max) as u16;
    }

    /// Show the selected headless agent's log file (tail). Live PTY agents use
    /// the embedded terminal instead, so this targets managed/delegated rows.
    pub fn show_log(&mut self) {
        let Some(r) = self.selected_row().cloned() else { return };
        if r.interactive {
            self.status_msg = "this agent has a live terminal — press enter to chat".into();
            return;
        }
        let Some(path) = r.log_path.clone() else {
            self.status_msg = "no log for this row".into();
            return;
        };
        self.log_text = read_log_tail(&path);
        self.log_path_open = Some(path);
        // jump to the bottom (freshest output)
        self.log_scroll = self.log_text.lines().count().saturating_sub(1) as u16;
        self.mode = Mode::Log;
    }

    pub fn scroll_log(&mut self, delta: i64) {
        let max = self.log_text.lines().count().saturating_sub(1) as i64;
        self.log_scroll = (self.log_scroll as i64).saturating_add(delta).clamp(0, max) as u16;
    }

    /// Scroll whichever full-screen viewer is open (diff or log).
    pub fn scroll_viewer(&mut self, delta: i64) {
        match self.mode {
            Mode::Diff => self.scroll_diff(delta),
            Mode::Log => self.scroll_log(delta),
            _ => {}
        }
    }

    /// Jump to the next/prev line matching `find_query` in the open viewer,
    /// wrapping around. Sets the viewer scroll to the matching line.
    pub fn find_next(&mut self, dir: i64) {
        if self.find_query.is_empty() {
            return;
        }
        let q = self.find_query.to_lowercase();
        let (text, cur) = match self.mode {
            Mode::Diff => (&self.diff_text, self.diff_scroll as i64),
            Mode::Log => (&self.log_text, self.log_scroll as i64),
            _ => return,
        };
        let lines: Vec<String> = text.lines().map(|l| l.to_lowercase()).collect();
        let n = lines.len() as i64;
        if n == 0 {
            return;
        }
        for step in 1..=n {
            let i = (cur + dir * step).rem_euclid(n);
            if lines[i as usize].contains(&q) {
                let target = i as u16;
                match self.mode {
                    Mode::Diff => self.diff_scroll = target,
                    Mode::Log => self.log_scroll = target,
                    _ => {}
                }
                self.status_msg = format!("/{}", self.find_query);
                return;
            }
        }
        self.status_msg = format!("no match for /{}", self.find_query);
    }

    /// Re-read the open log (called on the refresh tick while in Log mode). If
    /// the viewer is parked at the bottom, follow new output (tail -f); otherwise
    /// keep the reader's scroll position so they can study earlier lines.
    pub fn tick_log(&mut self) {
        let Some(path) = self.log_path_open.clone() else { return };
        let old_max = self.log_text.lines().count().saturating_sub(1) as u16;
        let following = self.log_scroll >= old_max;
        self.log_text = read_log_tail(&path);
        let new_max = self.log_text.lines().count().saturating_sub(1) as u16;
        if following {
            self.log_scroll = new_max;
        } else {
            self.log_scroll = self.log_scroll.min(new_max);
        }
    }

    /// Persist the current UI state (view toggles) so the next run restores it.
    /// Preserves the launch-choice fields already on disk.
    pub fn save_ui_prefs(&self) {
        let mut p = self.store.load_prefs();
        p.show_observed = self.show_observed;
        p.sort = self.sort.label().to_string();
        p.category = self.category.label().to_string();
        self.store.save_prefs(&p);
    }

    pub fn running_count(&self) -> usize {
        self.rows.iter().filter(|r| r.status == "running").count()
    }

    /// Kill every live interactive agent at once. Registry rows are kept so they
    /// stay resumable.
    pub fn stop_all(&mut self) {
        let mut n = 0;
        for p in &mut self.agents {
            if p.is_alive() {
                p.kill();
                n += 1;
            }
        }
        let _ = self.refresh();
        self.status_msg = format!("stopped {} agent(s)", n);
    }

    /// Drop finished work: remove exited live procs and forget persisted rows
    /// that aren't currently running. Keeps everything still alive.
    pub fn clear_finished(&mut self) {
        let alive: HashSet<String> = self
            .agents
            .iter_mut()
            .filter_map(|p| if p.is_alive() { Some(p.id.clone()) } else { None })
            .collect();
        let before_agents = self.agents.len();
        self.agents.retain_mut(|p| p.is_alive());
        let before_reg = self.registry.len();
        self.registry.retain(|r| alive.contains(&r.id));
        if self.registry.len() != before_reg {
            self.store.save_registry(&self.registry);
        }
        let removed = (before_agents - self.agents.len()) + (before_reg - self.registry.len());
        let _ = self.refresh();
        self.status_msg = format!("cleared {} finished", removed);
    }
}

/// Sort rows (running first, then exited, then observed), newest kept order.
fn merge_keep(mut rows: Vec<Row>) -> Vec<Row> {
    rows.sort_by_key(|r| match r.status.as_str() {
        "running" => 0,
        "failed" => 1, // surface failures near the top — they need attention
        "resumable" => 2,
        "done" => 3,
        "exited" => 4,
        _ => 5, // observed sessions last
    });
    rows
}

/// argv for a harness's INTERACTIVE REPL. Overridable via
/// `NEXUM_INTERACTIVE_CMD_<HARNESS>` (whitespace-split) so tests inject a stub.
pub fn interactive_argv(harness: &str, model: &str) -> Vec<String> {
    if let Ok(over) = std::env::var(format!("NEXUM_INTERACTIVE_CMD_{}", harness.to_uppercase())) {
        if !over.trim().is_empty() {
            return over.split_whitespace().map(|s| s.to_string()).collect();
        }
    }
    match harness {
        "claude" => vec!["claude".into(), "--model".into(), model.into()],
        "opencode" => vec!["opencode".into()],
        "cursor" => vec!["cursor-agent".into(), "--model".into(), model.into()],
        _ => vec!["claude".into()],
    }
}

/// argv to RESUME a harness in an existing worktree (harness restores the prior
/// session). Overridable via `NEXUM_RESUME_CMD_<HARNESS>`.
pub fn resume_argv(harness: &str, model: &str) -> Vec<String> {
    if let Ok(over) = std::env::var(format!("NEXUM_RESUME_CMD_{}", harness.to_uppercase())) {
        if !over.trim().is_empty() {
            return over.split_whitespace().map(|s| s.to_string()).collect();
        }
    }
    match harness {
        "claude" => vec!["claude".into(), "--continue".into(), "--model".into(), model.into()],
        "opencode" => vec!["opencode".into(), "--continue".into()],
        "cursor" => vec!["cursor-agent".into(), "--resume".into()],
        _ => interactive_argv(harness, model),
    }
}

/// Read the last 2000 lines of a log file, with friendly fallbacks.
fn read_log_tail(path: &str) -> String {
    match std::fs::read_to_string(path) {
        Ok(s) if !s.trim().is_empty() => {
            let lines: Vec<&str> = s.lines().collect();
            let start = lines.len().saturating_sub(2000);
            lines[start..].join("\n")
        }
        Ok(_) => format!("(log is empty: {})", path),
        Err(e) => format!("(cannot read {}: {})", path, e),
    }
}

/// Run `prog args` with cwd=`dir`; return (success, stdout, stderr).
fn run_in(dir: &str, prog: &str, args: &[&str]) -> (bool, String, String) {
    match std::process::Command::new(prog).current_dir(dir).args(args).output() {
        Ok(o) => (
            o.status.success(),
            String::from_utf8_lossy(&o.stdout).into_owned(),
            String::from_utf8_lossy(&o.stderr).into_owned(),
        ),
        Err(e) => (false, String::new(), e.to_string()),
    }
}

/// Branch state vs its upstream: "not pushed" (no upstream), "up to date", or
/// "↑ahead ↓behind". Empty string if not a git worktree.
fn remote_state(wt: &str) -> String {
    if !std::path::Path::new(wt).is_dir() {
        return String::new();
    }
    // no upstream configured → never pushed
    if !run_in(wt, "git", &["rev-parse", "--abbrev-ref", "@{upstream}"]).0 {
        return "not pushed".into();
    }
    let (ok, out, _) = run_in(wt, "git", &["rev-list", "--left-right", "--count", "@{upstream}...HEAD"]);
    if !ok {
        return String::new();
    }
    let mut it = out.split_whitespace();
    let behind = it.next().unwrap_or("0");
    let ahead = it.next().unwrap_or("0");
    if ahead == "0" && behind == "0" {
        "up to date".into()
    } else {
        format!("↑{} ↓{}", ahead, behind)
    }
}

/// A short summary of a worktree's git status: ("clean", true) or
/// ("N files · +ins -del", false); ("—", true) if not a worktree.
fn worktree_summary(wt: &str) -> (String, bool) {
    if !std::path::Path::new(wt).is_dir() {
        return ("—".into(), true);
    }
    let (ok, st, _) = run_in(wt, "git", &["status", "--porcelain"]);
    if !ok {
        return ("—".into(), true);
    }
    let files = st.lines().count();
    if files == 0 {
        return ("clean".into(), true);
    }
    // insertions/deletions from shortstat (tracked changes only)
    let (_, ss, _) = run_in(wt, "git", &["diff", "--shortstat"]);
    let pick = |kw: &str| ss.split(',').find(|s| s.contains(kw))
        .and_then(|s| s.trim().split_whitespace().next()).unwrap_or("0").to_string();
    let (ins, del) = (pick("insertion"), pick("deletion"));
    (format!("{} file(s) · +{} -{}", files, ins, del), false)
}

/// A worktree with no uncommitted or untracked changes (safe to `git worktree
/// remove`). False if the path is gone or git errors — never prune on doubt.
fn worktree_clean(wt: &str) -> bool {
    if !std::path::Path::new(wt).is_dir() {
        return false;
    }
    let (ok, out, _) = run_in(wt, "git", &["status", "--porcelain"]);
    ok && out.trim().is_empty()
}

/// `git args` run in `dir`; stdout trimmed (empty on failure).
fn git_out(dir: &str, args: &[&str]) -> String {
    let (ok, out, _) = run_in(dir, "git", args);
    if ok { out.trim().to_string() } else { String::new() }
}

/// Whether the GitHub CLI is installed (for auto-PR after push).
pub fn gh_available() -> bool {
    on_path("gh")
}

/// Open a URL in the default browser (`open` on macOS, `xdg-open` on Linux).
fn open_url(url: &str) -> bool {
    let opener = if cfg!(target_os = "macos") { "open" } else { "xdg-open" };
    std::process::Command::new(opener).arg(url)
        .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null())
        .spawn().is_ok()
}

/// The CLI binary name for a harness.
pub fn harness_bin(harness: &str) -> &'static str {
    match harness {
        "opencode" => "opencode",
        "cursor" => "cursor-agent",
        _ => "claude",
    }
}

/// Whether a harness's CLI is installed (on PATH).
pub fn harness_installed(harness: &str) -> bool {
    on_path(harness_bin(harness))
}

/// True if an executable named `bin` exists on `$PATH` (no process spawned).
pub fn on_path(bin: &str) -> bool {
    std::env::var_os("PATH")
        .map(|paths| {
            std::env::split_paths(&paths).any(|dir| {
                let p = dir.join(bin);
                p.is_file()
            })
        })
        .unwrap_or(false)
}

fn now_millis() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// Current branch name of a git repo (empty if not a repo / detached).
fn git_branch(repo: &str) -> String {
    std::process::Command::new("git")
        .arg("-C").arg(repo).args(["rev-parse", "--abbrev-ref", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default()
}

/// `git diff` for a worktree, with an appended list of untracked files. Returns
/// a friendly message rather than erroring when there's nothing / no git.
fn worktree_diff(wt: &str) -> String {
    let run = |args: &[&str]| -> String {
        std::process::Command::new("git")
            .arg("-C").arg(wt).args(args)
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
            .unwrap_or_default()
    };
    let mut out = run(&["diff"]);
    let untracked = run(&["ls-files", "--others", "--exclude-standard"]);
    if !untracked.trim().is_empty() {
        out.push_str("\n── untracked files ──\n");
        for f in untracked.lines() {
            out.push_str(&format!("+ {}\n", f));
        }
    }
    if out.trim().is_empty() {
        format!("no changes in {}", wt)
    } else {
        out
    }
}

pub fn slugify(task: &str) -> String {
    let base: String = task
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c.to_ascii_lowercase() } else { '-' })
        .collect();
    let base: String = base.split('-').filter(|s| !s.is_empty()).collect::<Vec<_>>().join("-");
    let base: String = base.chars().take(20).collect();
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() % 1_000_000)
        .unwrap_or(0);
    if base.is_empty() { format!("agent-{}", ts) } else { format!("{}-{}", base, ts) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn app() -> App {
        App::new(Store::new(PathBuf::from("/tmp/r"), PathBuf::from("/tmp/r/scripts")))
    }

    #[test]
    fn model_picker_cycles_per_harness() {
        let mut a = app();
        a.open_new_agent();
        assert_eq!(a.new_form.model(), "sonnet");
        a.new_form.cycle_model(1);
        assert_eq!(a.new_form.model(), "opus");
        a.new_form.cycle_harness(2); // claude -> cursor
        assert_eq!(a.new_form.harness(), "cursor");
        assert_eq!(a.new_form.model(), "auto");
    }

    #[test]
    fn empty_task_blocks_launch() {
        let mut a = app();
        a.open_new_agent();
        a.spawn_from_form();
        assert_eq!(a.mode, Mode::NewAgent);
        assert!(a.new_form.error.is_some());
        assert_eq!(a.new_form.field, FIELD_TASK);
    }

    #[test]
    fn workflow_seeds_plan_build_and_steers_executor() {
        let mut f = NewAgentForm::default();
        f.task.insert_str("ship the billing endpoint");
        // 0 = chat: no workflow seed
        assert!(!f.full_prompt().contains("nx-plan"));
        // 1 = plan → build (same harness)
        f.workflow_idx = 1;
        let p = f.full_prompt();
        assert!(p.contains("/nx-plan") && p.contains("/nx-build"));
        assert!(p.contains("ship the billing endpoint"));
        // 2 = steer executor to cursor
        f.workflow_idx = 2;
        assert!(f.full_prompt().contains("/nx-build --harness cursor"));
        // 3 = steer executor to opencode
        f.workflow_idx = 3;
        assert!(f.full_prompt().contains("/nx-build --harness opencode"));
        // 4 = plan only: no nx-build
        f.workflow_idx = 4;
        let p = f.full_prompt();
        assert!(p.contains("/nx-plan") && !p.contains("/nx-build"));
    }

    #[test]
    fn selected_pr_matches_branch() {
        use crate::agent::{ManagedAgent, Row};
        let row = Row::from_managed(&ManagedAgent {
            agent_id: "a".into(), harness: Some("cursor".into()), model: None,
            worktree: None, branch: Some("crew/x".into()), status: Some("done".into()),
            cost_usd: None, task: None, step_index: None, updated_ts: None, pid: None, log_path: None,
        });
        let mut a = app();
        a.rows = vec![row];
        a.selected = 0;
        assert!(a.selected_pr().is_none());
        a.pr_map.insert("crew/x".into(), (42, "https://example/pr/42".into()));
        assert_eq!(a.selected_pr().unwrap().0, 42);
    }

    #[test]
    fn remote_state_reports_push_state() {
        use std::process::Command;
        let base = std::env::temp_dir().join(format!("crew-rem-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let origin = base.join("o.git");
        let work = base.join("w");
        Command::new("git").args(["init", "--bare", "-q"]).arg(&origin).output().unwrap();
        std::fs::create_dir_all(&work).unwrap();
        let g = |a: &[&str]| { Command::new("git").arg("-C").arg(&work).args(a).output().unwrap(); };
        g(&["init", "-q"]); g(&["config", "user.email", "t@t"]); g(&["config", "user.name", "t"]);
        std::fs::write(work.join("f.txt"), "1").unwrap();
        g(&["add", "f.txt"]); g(&["commit", "-qm", "init"]);
        g(&["remote", "add", "origin", origin.to_str().unwrap()]);
        let ws = work.to_string_lossy().to_string();

        assert_eq!(remote_state(&ws), "not pushed");
        g(&["push", "-qu", "origin", "HEAD"]);
        assert_eq!(remote_state(&ws), "up to date");
        std::fs::write(work.join("f.txt"), "2").unwrap();
        g(&["commit", "-qam", "more"]);
        assert_eq!(remote_state(&ws), "↑1 ↓0");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn worktree_summary_reports_clean_and_dirty() {
        use std::process::Command;
        let repo = std::env::temp_dir().join(format!("crew-sum-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&repo);
        std::fs::create_dir_all(&repo).unwrap();
        let g = |a: &[&str]| { Command::new("git").arg("-C").arg(&repo).args(a).output().unwrap(); };
        g(&["init", "-q"]); g(&["config", "user.email", "t@t"]); g(&["config", "user.name", "t"]);
        std::fs::write(repo.join("seed.txt"), "one\n").unwrap();
        g(&["add", "seed.txt"]); g(&["commit", "-qm", "init"]);
        let rs = repo.to_string_lossy().to_string();

        let (s, clean) = worktree_summary(&rs);
        assert!(clean && s == "clean", "{s}");
        std::fs::write(repo.join("seed.txt"), "one\ntwo\n").unwrap(); // modify tracked
        let (s2, clean2) = worktree_summary(&rs);
        assert!(!clean2, "should be dirty");
        assert!(s2.contains("file(s)"), "{s2}");
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn remove_prunes_clean_crew_worktree() {
        use std::process::Command;
        let repo = std::env::temp_dir().join(format!("crew-prune-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&repo);
        std::fs::create_dir_all(&repo).unwrap();
        let g = |a: &[&str]| { Command::new("git").arg("-C").arg(&repo).args(a).output().unwrap(); };
        g(&["init", "-q"]); g(&["config", "user.email", "t@t"]); g(&["config", "user.name", "t"]);
        std::fs::write(repo.join("seed.txt"), "x").unwrap();
        g(&["add", "seed.txt"]); g(&["commit", "-qm", "init"]);
        let repo = std::fs::canonicalize(&repo).unwrap();

        let mut a = App::new(Store::new(repo.clone(), repo.join("scripts")));
        let wt = a.store.create_worktree("prune-me").unwrap();
        assert!(std::path::Path::new(&wt).is_dir());
        a.registry.push(AgentRecord {
            id: "p".into(), harness: "claude".into(), model: "m".into(),
            worktree: wt.clone(), task: "t".into(), label: None,
        });
        a.refresh().unwrap();
        a.selected = a.rows.iter().position(|r| r.id == "p").unwrap();
        a.remove_selected();
        assert!(!std::path::Path::new(&wt).exists(), "clean worktree should be pruned");
        assert!(a.status_msg.contains("pruned"), "{}", a.status_msg);
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[test]
    fn label_shows_in_list_and_persists() {
        let mut a = app();
        a.registry.push(AgentRecord {
            id: "lbl1".into(), harness: "claude".into(), model: "sonnet".into(),
            worktree: "/tmp/wt".into(), task: "do the thing".into(), label: None,
        });
        a.refresh().unwrap();
        a.selected = a.rows.iter().position(|r| r.id == "lbl1").unwrap();
        a.open_rename();
        assert_eq!(a.mode, Mode::Rename);
        a.rename_buf = "nice name".into();
        a.commit_rename();
        // registry updated + row displays the label instead of the task
        let rec = a.registry.iter().find(|r| r.id == "lbl1").unwrap();
        assert_eq!(rec.label.as_deref(), Some("nice name"));
        let row = a.rows.iter().find(|r| r.id == "lbl1").unwrap();
        assert_eq!(row.display(), "nice name");
    }

    #[test]
    fn ui_state_persists_across_sessions() {
        let dir = std::env::temp_dir().join(format!("crew-ui-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let mut a = App::new(Store::new(dir.clone(), dir.join("scripts")));
        a.show_observed = false;
        a.sort = SortKey::Cost;
        a.category = Category::Running;
        a.save_ui_prefs();
        // a fresh session restores the view
        let b = App::new(Store::new(dir.clone(), dir.join("scripts")));
        assert!(!b.show_observed);
        assert_eq!(b.sort, SortKey::Cost);
        assert_eq!(b.category, Category::Running);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn settings_edit_and_persist() {
        let dir = std::env::temp_dir().join(format!("crew-set-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let mut a = App::new(Store::new(dir.clone(), dir.join("scripts")));
        a.open_settings();
        assert_eq!(a.mode, Mode::Settings);
        a.settings_change(1); // harness 0 -> 1
        a.settings_field = 2;
        a.settings_change(1); // workflow 0 -> 1
        a.settings_field = 3;
        a.settings_change(1); // worktree toggle true -> false
        a.save_settings();
        assert_eq!(a.mode, Mode::Normal);
        let p = a.store.load_prefs();
        assert_eq!(p.harness_idx, 1);
        assert_eq!(p.workflow_idx, 1);
        assert!(!p.worktree_new);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn on_path_detects_binaries() {
        assert!(on_path("sh"), "sh should be on PATH");
        assert!(!on_path("definitely-not-a-real-binary-xyz-123"));
        assert_eq!(harness_bin("cursor"), "cursor-agent");
        assert_eq!(harness_bin("claude"), "claude");
    }

    #[test]
    fn editor_and_shell_target_the_worktree() {
        use crate::agent::{ManagedAgent, Row};
        let row = Row::from_managed(&ManagedAgent {
            agent_id: "a".into(), harness: Some("cursor".into()), model: None,
            worktree: Some("/tmp/wt".into()), branch: None, status: Some("done".into()),
            cost_usd: None, task: None, step_index: None, updated_ts: None, pid: None, log_path: None,
        });
        let mut a = app();
        a.rows = vec![row];
        a.selected = 0;
        std::env::set_var("EDITOR", "myed -w");
        a.open_editor();
        let (cwd, argv) = a.pending_shell.take().unwrap();
        assert_eq!(cwd, "/tmp/wt");
        assert_eq!(argv, vec!["myed", "-w", "/tmp/wt"]);
        std::env::set_var("SHELL", "myshell");
        a.open_shell();
        let (_c, argv) = a.pending_shell.take().unwrap();
        assert_eq!(argv, vec!["myshell"]);
    }

    #[test]
    fn notifies_on_running_to_done_transition() {
        use crate::agent::{ManagedAgent, Row};
        let mk = |id: &str, st: &str| Row::from_managed(&ManagedAgent {
            agent_id: id.into(), harness: Some("cursor".into()), model: None, worktree: None,
            branch: None, status: Some(st.into()), cost_usd: None, task: Some("do the thing".into()),
            step_index: None, updated_ts: None, pid: None, log_path: None,
        });
        let mut a = app();
        a.all_rows = vec![mk("x", "running")];
        a.notify_completions();
        assert!(a.status_msg.is_empty()); // first sighting: no toast
        a.all_rows = vec![mk("x", "done")];
        a.notify_completions();
        assert!(a.status_msg.contains('✓'), "{}", a.status_msg);
        assert!(a.status_msg.contains("do the thing"));
    }

    #[test]
    fn marking_rows_and_clearing() {
        use crate::agent::{ManagedAgent, Row};
        let mk = |id: &str| Row::from_managed(&ManagedAgent {
            agent_id: id.into(), harness: None, model: None, worktree: None,
            branch: None, status: Some("done".into()), cost_usd: None, task: None,
            step_index: None, updated_ts: None, pid: None, log_path: None,
        });
        let mut a = app();
        a.rows = vec![mk("a"), mk("b"), mk("c")];
        a.selected = 0;
        a.toggle_mark(); // marks "a", advances to 1
        assert!(a.marked.contains("a"));
        assert_eq!(a.selected, 1);
        a.toggle_mark(); // marks "b"
        assert_eq!(a.marked.len(), 2);
        a.toggle_mark(); // marks "c"
        a.selected = 2;
        a.toggle_mark(); // toggles "c" back off
        assert!(!a.marked.contains("c"));
        a.clear_marks();
        assert!(a.marked.is_empty());
        a.mark_all_visible();
        assert_eq!(a.marked.len(), 3);
    }

    #[test]
    fn jump_failed_cycles_failed_rows() {
        use crate::agent::{ManagedAgent, Row};
        let mk = |id: &str, st: &str| Row::from_managed(&ManagedAgent {
            agent_id: id.into(), harness: None, model: None, worktree: None,
            branch: None, status: Some(st.into()), cost_usd: None, task: None,
            step_index: None, updated_ts: None, pid: None, log_path: None,
        });
        let mut a = app();
        a.rows = vec![mk("a", "done"), mk("b", "failed"), mk("c", "running"), mk("d", "failed")];
        a.selected = 0;
        a.jump_failed(1);
        assert_eq!(a.selected, 1);
        a.jump_failed(1);
        assert_eq!(a.selected, 3);
        a.jump_failed(1); // wrap
        assert_eq!(a.selected, 1);
        a.jump_failed(-1);
        assert_eq!(a.selected, 3);
    }

    #[test]
    fn sel_top_and_bottom() {
        use crate::agent::{ManagedAgent, Row};
        let mk = |id: &str| Row::from_managed(&ManagedAgent {
            agent_id: id.into(), harness: None, model: None, worktree: None,
            branch: None, status: Some("done".into()), cost_usd: None,
            task: None, step_index: None, updated_ts: None, pid: None, log_path: None,
        });
        let mut a = app();
        a.rows = vec![mk("a"), mk("b"), mk("c")];
        a.sel_bottom();
        assert_eq!(a.selected, 2);
        a.sel_top();
        assert_eq!(a.selected, 0);
    }

    #[test]
    fn find_next_cycles_matches_and_wraps() {
        let mut a = app();
        a.mode = Mode::Diff;
        a.diff_text = "alpha\nbeta\nGAMMA match\ndelta\nmatch again\n".into();
        a.find_query = "match".into();
        a.diff_scroll = 0;
        a.find_next(1);
        assert_eq!(a.diff_scroll, 2); // first match (case-insensitive)
        a.find_next(1);
        assert_eq!(a.diff_scroll, 4);
        a.find_next(1); // wrap to top
        assert_eq!(a.diff_scroll, 2);
        a.find_next(-1); // backward wraps
        assert_eq!(a.diff_scroll, 4);
        // empty query is a no-op
        a.find_query.clear();
        a.find_next(1);
        assert_eq!(a.diff_scroll, 4);
    }

    #[test]
    fn sort_key_cycles() {
        assert_eq!(SortKey::Status.next(), SortKey::Cost);
        assert_eq!(SortKey::Cost.next(), SortKey::Recent);
        assert_eq!(SortKey::Recent.next(), SortKey::Status);
    }

    #[test]
    fn category_and_sort_filter_rows() {
        use crate::agent::{ManagedAgent, Row};
        let managed = |id: &str, status: &str, cost: f64, ts: f64| ManagedAgent {
            agent_id: id.into(), harness: Some("cursor".into()), model: None,
            worktree: None, branch: None, status: Some(status.into()),
            cost_usd: Some(cost), task: Some("t".into()), step_index: None,
            updated_ts: Some(ts), pid: None, log_path: None,
        };
        let mut a = app();
        a.all_rows = vec![
            Row::from_managed(&managed("a", "running", 0.10, 100.0)),
            Row::from_managed(&managed("b", "done", 0.50, 200.0)),
            Row::from_managed(&managed("c", "failed", 0.02, 300.0)),
        ];
        // category: running only
        a.set_category(Category::Running);
        assert_eq!(a.rows.len(), 1);
        assert_eq!(a.rows[0].id, "a");
        // back to all, sort by cost desc
        a.set_category(Category::All);
        a.sort = SortKey::Cost;
        a.apply_filter();
        assert_eq!(a.rows[0].id, "b"); // 0.50 first
        // sort by recent desc
        a.sort = SortKey::Recent;
        a.apply_filter();
        assert_eq!(a.rows[0].id, "c"); // ts 300 first
    }

    #[test]
    fn workflow_cycles_and_wraps() {
        let mut f = NewAgentForm::default();
        assert_eq!(f.workflow_idx, 0);
        f.cycle_workflow(-1);
        assert_eq!(f.workflow_idx, WORKFLOWS.len() - 1);
        f.cycle_workflow(1);
        assert_eq!(f.workflow_idx, 0);
    }

    #[test]
    fn full_prompt_appends_images() {
        let mut f = NewAgentForm::default();
        f.task.insert_str("fix the header");
        f.images.push("/tmp/shot.png".into());
        let p = f.full_prompt();
        assert!(p.contains("fix the header"));
        assert!(p.contains("[image] /tmp/shot.png"));
    }
}
