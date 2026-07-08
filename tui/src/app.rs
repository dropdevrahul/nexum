//! Application state and the actions the key handlers call.
//!
//! Interactive agents are PTY-backed processes owned here (`agents`), rendered
//! *inside* the TUI. Observed plugin sessions still come from the DB (read-only).

use crate::agent::Row;
use crate::pty::AgentProc;
use crate::store::{AgentRecord, Store};
use anyhow::Result;
use std::collections::HashSet;
use tui_textarea::TextArea;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Normal,
    NewAgent,
    Help,
    ConfirmStop,
    ConfirmQuit,
    /// Esc pressed in the new-agent form with unsaved input — confirm discard.
    ConfirmDiscard,
    Filter,
    /// Focused into the selected agent's embedded terminal — keys go to the PTY.
    Terminal,
}

pub const HARNESSES: [&str; 3] = ["claude", "opencode", "cursor"];

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
pub const FIELD_HARNESS: usize = 1;
pub const FIELD_MODEL: usize = 2;
pub const FIELD_WORKTREE: usize = 3;
pub const FIELD_IMAGES: usize = 4;
pub const FIELD_COUNT: usize = 5;

pub struct NewAgentForm {
    pub task: TextArea<'static>,
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
    pub fn cycle_harness(&mut self, d: i64) {
        let n = HARNESSES.len() as i64;
        self.harness_idx = (((self.harness_idx as i64 + d) % n + n) % n) as usize;
        self.model_idx = 0;
    }
    pub fn task_text(&self) -> String {
        self.task.lines().join("\n")
    }
    /// Full prompt sent to the agent: task plus any attached image paths.
    pub fn full_prompt(&self) -> String {
        let mut s = self.task_text();
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
    /// Size (cols, rows) the terminal pane was last rendered at — used to resize.
    pub term_size: (u16, u16),
}

impl App {
    pub fn new(store: Store) -> App {
        let registry = store.load_registry();
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
            show_observed: true,
            all_rows: Vec::new(),
            filter_text: String::new(),
            term_size: (80, 24),
        }
    }

    pub fn apply_filter(&mut self) {
        let q = self.filter_text.to_lowercase();
        self.rows = if q.is_empty() {
            self.all_rows.clone()
        } else {
            self.all_rows
                .iter()
                .filter(|r| {
                    r.task.to_lowercase().contains(&q)
                        || r.harness.to_lowercase().contains(&q)
                        || r.id.to_lowercase().contains(&q)
                        || r.status.to_lowercase().contains(&q)
                })
                .cloned()
                .collect()
        };
        if self.selected >= self.rows.len() && !self.rows.is_empty() {
            self.selected = self.rows.len() - 1;
        }
    }

    pub fn refresh(&mut self) -> Result<()> {
        // live interactive agents first (from the owned PTY procs)
        let mut rows: Vec<Row> = self
            .agents
            .iter_mut()
            .enumerate()
            .map(|(i, p)| Row::from_proc(i, p))
            .collect();
        let live_ids: HashSet<String> = rows.iter().map(|r| r.id.clone()).collect();
        // persisted agents that aren't currently live → resumable
        for rec in &self.registry {
            if !live_ids.contains(&rec.id) {
                rows.push(Row::from_record(rec));
            }
        }
        // observed plugin sessions (read-only, from the DB)
        if self.show_observed {
            let sessions = self.store.sessions().unwrap_or_default();
            rows.extend(sessions.iter().map(Row::from_session));
        }
        rows = merge_keep(rows);
        self.all_rows = rows;
        self.apply_filter();
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
    }

    pub fn open_new_agent(&mut self) {
        self.new_form = NewAgentForm::default();
        self.mode = Mode::NewAgent;
    }

    /// Launch an interactive PTY agent from the form. Task is required.
    pub fn spawn_from_form(&mut self) {
        if self.new_form.task_text().trim().is_empty() {
            self.new_form.error = Some("task is required".into());
            self.new_form.field = FIELD_TASK;
            return;
        }
        let harness = self.new_form.harness().to_string();
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
                    id: id.clone(), harness, model, worktree, task: task_text,
                });
                self.store.save_registry(&self.registry);
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
        if let Some(idx) = self.selected_row().and_then(|r| r.proc_idx) {
            if let Some(p) = self.agents.get_mut(idx) {
                p.kill();
            }
            self.status_msg = "stopped".into();
            let _ = self.refresh();
        } else {
            self.status_msg = "only interactive agents can be stopped here".into();
        }
    }

    /// Remove the selected agent: kill any live process AND forget its persisted
    /// record (so it no longer shows as resumable). The worktree is left on disk.
    pub fn remove_selected(&mut self) {
        let Some(r) = self.selected_row().cloned() else { return };
        if let Some(idx) = r.proc_idx {
            if let Some(p) = self.agents.get_mut(idx) {
                p.kill();
            }
            self.agents.remove(idx);
        }
        let before = self.registry.len();
        self.registry.retain(|rec| rec.id != r.id);
        if self.registry.len() != before {
            self.store.save_registry(&self.registry);
        }
        let _ = self.refresh();
        self.status_msg = "removed".into();
    }

    pub fn show_worktree(&mut self) {
        if let Some(r) = self.selected_row() {
            self.status_msg = r.worktree.clone().unwrap_or_else(|| "no worktree".into());
        }
    }

    pub fn running_count(&self) -> usize {
        self.rows.iter().filter(|r| r.status == "running").count()
    }
}

/// Sort rows (running first, then exited, then observed), newest kept order.
fn merge_keep(mut rows: Vec<Row>) -> Vec<Row> {
    rows.sort_by_key(|r| match r.status.as_str() {
        "running" => 0,
        "resumable" => 1,
        "exited" => 2,
        _ => 3,
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

fn slugify(task: &str) -> String {
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
    fn full_prompt_appends_images() {
        let mut f = NewAgentForm::default();
        f.task.insert_str("fix the header");
        f.images.push("/tmp/shot.png".into());
        let p = f.full_prompt();
        assert!(p.contains("fix the header"));
        assert!(p.contains("[image] /tmp/shot.png"));
    }
}
