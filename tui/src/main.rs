//! nexum-tui — a repo-scoped dashboard for running and chatting with coding
//! agents. Each agent runs its harness REPL in a PTY rendered *inside* the TUI.
//!
//!   nexum-tui                 # interactive dashboard
//!   nexum-tui --dump          # print the agent list once and exit (no TTY)
//!   nexum-tui --snapshot      # render one real frame to text (no TTY)
//!   nexum-tui --scripts DIR   # override the scripts dir (default <repo>/scripts)

mod agent;
mod app;
mod pty;
mod store;
mod ui;

use anyhow::Result;
use app::{App, Mode};
use crossterm::{
    event::{
        self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEvent, KeyEventKind,
        KeyModifiers, MouseEventKind,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{backend::CrosstermBackend, Terminal};
use std::io;
use std::path::PathBuf;
use std::time::{Duration, Instant};

type Term = Terminal<CrosstermBackend<io::Stdout>>;

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let scripts_override = arg_value(&args, "--scripts");

    let cwd = std::env::current_dir()?;
    let repo_root = store::git_toplevel(&cwd);
    let scripts_dir = scripts_override
        .map(PathBuf::from)
        .or_else(|| std::env::var("NEXUM_SCRIPTS").ok().map(PathBuf::from))
        .unwrap_or_else(|| repo_root.join("scripts"));

    let st = store::Store::new(repo_root, scripts_dir);
    let mut app = App::new(st);
    app.refresh()?;

    if args.iter().any(|a| a == "--dump") {
        dump_table(&app);
        return Ok(());
    }
    if args.iter().any(|a| a == "--snapshot") {
        match std::env::var("NEXUM_SNAPSHOT_MODE").as_deref() {
            Ok("new") => {
                app.open_new_agent();
                app.new_form.task.insert_str("add auth middleware");
            }
            Ok("help") => app.mode = app::Mode::Help,
            Ok("term") | Ok("quit") => {
                // spawn a stub interactive agent and drop into its terminal
                app.term_size = (80, 24);
                app.open_new_agent();
                app.new_form.task.insert_str("say hello");
                app.new_form.worktree_new = false; // run in cwd, skip worktree.py
                app.spawn_from_form();
                std::thread::sleep(Duration::from_millis(600));
                let _ = app.refresh();
                if std::env::var("NEXUM_SNAPSHOT_MODE").as_deref() == Ok("quit") {
                    app.mode = app::Mode::ConfirmQuit;
                }
            }
            _ => {}
        }
        snapshot(&app);
        return Ok(());
    }

    run_tui(&mut app)
}

fn dump_table(app: &App) {
    println!("nexum agents for {} — {} rows", app.store.repo_root.display(), app.rows.len());
    for r in &app.rows {
        let kind = if r.interactive { "agent" } else { "session" };
        println!("{:<7} {:<9} {:<8} {}", kind, r.harness, r.status, r.task);
    }
}

fn snapshot(app: &App) {
    use ratatui::{backend::TestBackend, Terminal};
    let mut term = Terminal::new(TestBackend::new(100, 30)).unwrap();
    term.draw(|f| ui::draw(f, app)).unwrap();
    let buf = term.backend().buffer().clone();
    for y in 0..buf.area.height {
        let mut line = String::new();
        for x in 0..buf.area.width {
            line.push_str(buf[(x, y)].symbol());
        }
        println!("{}", line.trim_end());
    }
}

fn run_tui(app: &mut App) -> Result<()> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let res = event_loop(app, &mut terminal);

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen, DisableMouseCapture)?;
    terminal.show_cursor()?;
    res
}

fn event_loop(app: &mut App, terminal: &mut Term) -> Result<()> {
    // fast tick so the embedded terminal feels live; refresh list less often.
    let refresh_every = Duration::from_millis(800);
    let mut last_refresh = Instant::now();

    loop {
        terminal.draw(|f| ui::draw(f, app))?;

        if event::poll(Duration::from_millis(60))? {
            match event::read()? {
                Event::Key(key) if key.kind == KeyEventKind::Press => {
                    if app.mode == Mode::Terminal {
                        handle_terminal_key(app, key);
                    } else {
                        if !app.status_msg.is_empty() {
                            app.status_msg.clear();
                        }
                        handle_key(app, key);
                    }
                }
                Event::Mouse(m) if app.mode == Mode::Normal => match m.kind {
                    MouseEventKind::ScrollDown => app.move_sel(1),
                    MouseEventKind::ScrollUp => app.move_sel(-1),
                    _ => {}
                },
                _ => {}
            }
        }

        if last_refresh.elapsed() >= refresh_every {
            let _ = app.refresh();
            last_refresh = Instant::now();
        }

        if app.should_quit {
            return Ok(());
        }
    }
}

/// In Terminal mode every key is forwarded to the agent's PTY, except Ctrl-o
/// which leaves the embedded terminal and returns to the dashboard.
fn handle_terminal_key(app: &mut App, key: KeyEvent) {
    if key.code == KeyCode::Char('o') && key.modifiers.contains(KeyModifiers::CONTROL) {
        app.mode = Mode::Normal;
        return;
    }
    let bytes = key_to_bytes(key);
    if !bytes.is_empty() {
        app.send_to_terminal(&bytes);
    }
}

fn handle_key(app: &mut App, key: KeyEvent) {
    let code = key.code;
    match app.mode {
        Mode::Normal => match code {
            KeyCode::Char('q') => {
                if app.agents.iter_mut().any(|a| a.is_alive()) {
                    app.mode = Mode::ConfirmQuit;
                } else {
                    app.should_quit = true;
                }
            }
            KeyCode::Char('j') | KeyCode::Down => app.move_sel(1),
            KeyCode::Char('k') | KeyCode::Up => app.move_sel(-1),
            KeyCode::Char('r') => { let _ = app.refresh(); }
            KeyCode::Char('n') => app.open_new_agent(),
            KeyCode::Char('s') => {
                if app.selected_row().map(|r| r.interactive).unwrap_or(false) {
                    app.mode = Mode::ConfirmStop;
                } else {
                    app.status_msg = "only interactive agents can be stopped".into();
                }
            }
            KeyCode::Char('x') => app.remove_selected(),
            KeyCode::Char('o') => app.show_worktree(),
            KeyCode::Char('h') => { app.show_observed = !app.show_observed; let _ = app.refresh(); }
            KeyCode::Char('/') => app.mode = Mode::Filter,
            KeyCode::Char('?') => app.mode = Mode::Help,
            KeyCode::Enter => app.open_selected(),
            _ => {}
        },
        Mode::Help => app.mode = Mode::Normal,
        Mode::ConfirmQuit => match code {
            KeyCode::Char('y') | KeyCode::Enter => app.should_quit = true,
            KeyCode::Char('n') | KeyCode::Esc => app.mode = Mode::Normal,
            _ => {}
        },
        Mode::ConfirmStop => match code {
            KeyCode::Char('y') => { app.stop_selected(); app.mode = Mode::Normal; }
            KeyCode::Char('n') | KeyCode::Esc => app.mode = Mode::Normal,
            _ => {}
        },
        Mode::ConfirmDiscard => match code {
            KeyCode::Char('y') => { app.new_form = app::NewAgentForm::default(); app.mode = Mode::Normal; }
            KeyCode::Char('n') | KeyCode::Esc => app.mode = Mode::NewAgent,
            _ => {}
        },
        Mode::Filter => match code {
            KeyCode::Esc => { app.filter_text.clear(); app.apply_filter(); app.mode = Mode::Normal; }
            KeyCode::Enter => app.mode = Mode::Normal,
            KeyCode::Backspace => { app.filter_text.pop(); app.apply_filter(); }
            KeyCode::Char(c) => { app.filter_text.push(c); app.apply_filter(); }
            _ => {}
        },
        Mode::NewAgent => handle_new_agent_key(app, key),
        Mode::Terminal => {}
    }
}

fn handle_new_agent_key(app: &mut App, key: KeyEvent) {
    use app::{FIELD_HARNESS, FIELD_IMAGES, FIELD_MODEL, FIELD_TASK, FIELD_WORKTREE, FIELD_COUNT};
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);

    // Ctrl-S launches from any field (task uses Enter for newlines).
    if ctrl && key.code == KeyCode::Char('s') {
        app.spawn_from_form();
        return;
    }
    match key.code {
        KeyCode::Esc => {
            // cancel an in-progress image path first; otherwise confirm before
            // throwing away a form the user has typed into.
            if app.new_form.image_input.is_some() {
                app.new_form.image_input = None;
            } else if app.new_form.task_text().trim().is_empty() && app.new_form.images.is_empty() {
                app.mode = Mode::Normal;
            } else {
                app.mode = Mode::ConfirmDiscard;
            }
            return;
        }
        KeyCode::Tab => { app.new_form.field = (app.new_form.field + 1) % FIELD_COUNT; return; }
        KeyCode::BackTab => { app.new_form.field = (app.new_form.field + FIELD_COUNT - 1) % FIELD_COUNT; return; }
        _ => {}
    }

    let f = &mut app.new_form;
    // image-path capture sub-mode
    if let Some(buf) = f.image_input.as_mut() {
        match key.code {
            KeyCode::Enter => {
                let path = buf.trim().to_string();
                if !path.is_empty() { f.images.push(path); }
                f.image_input = None;
            }
            KeyCode::Esc => f.image_input = None,
            KeyCode::Backspace => { buf.pop(); }
            KeyCode::Char(c) => buf.push(c),
            _ => {}
        }
        return;
    }

    match f.field {
        FIELD_TASK => { f.task.input(key); }
        FIELD_HARNESS => match key.code {
            KeyCode::Left | KeyCode::Char('h') => f.cycle_harness(-1),
            KeyCode::Right | KeyCode::Char('l') => f.cycle_harness(1),
            _ => {}
        },
        FIELD_MODEL => match key.code {
            KeyCode::Left | KeyCode::Char('h') => f.cycle_model(-1),
            KeyCode::Right | KeyCode::Char('l') => f.cycle_model(1),
            _ => {}
        },
        FIELD_WORKTREE => match key.code {
            KeyCode::Left | KeyCode::Right | KeyCode::Char(' ') | KeyCode::Char('h') | KeyCode::Char('l') => {
                f.worktree_new = !f.worktree_new;
            }
            _ => {}
        },
        FIELD_IMAGES => match key.code {
            KeyCode::Char('a') | KeyCode::Enter => f.image_input = Some(String::new()),
            KeyCode::Char('d') | KeyCode::Backspace => { f.images.pop(); }
            _ => {}
        },
        _ => {}
    }
}

/// Convert a keypress into the bytes a terminal would send to the PTY.
fn key_to_bytes(key: KeyEvent) -> Vec<u8> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        KeyCode::Char(c) => {
            if ctrl {
                let b = (c.to_ascii_uppercase() as u8).wrapping_sub(0x40) & 0x7f;
                vec![b]
            } else {
                c.to_string().into_bytes()
            }
        }
        KeyCode::Enter => vec![b'\r'],
        KeyCode::Backspace => vec![0x7f],
        KeyCode::Tab => vec![b'\t'],
        KeyCode::Esc => vec![0x1b],
        KeyCode::Left => b"\x1b[D".to_vec(),
        KeyCode::Right => b"\x1b[C".to_vec(),
        KeyCode::Up => b"\x1b[A".to_vec(),
        KeyCode::Down => b"\x1b[B".to_vec(),
        KeyCode::Home => b"\x1b[H".to_vec(),
        KeyCode::End => b"\x1b[F".to_vec(),
        KeyCode::PageUp => b"\x1b[5~".to_vec(),
        KeyCode::PageDown => b"\x1b[6~".to_vec(),
        KeyCode::Delete => b"\x1b[3~".to_vec(),
        _ => vec![],
    }
}

fn arg_value(args: &[String], flag: &str) -> Option<String> {
    args.iter().position(|a| a == flag).and_then(|i| args.get(i + 1).cloned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use app::App;
    use std::path::PathBuf;

    fn app() -> App {
        App::new(store::Store::new(PathBuf::from("/tmp/r"), PathBuf::from("/tmp/r/scripts")))
    }
    fn esc() -> KeyEvent {
        KeyEvent::new(KeyCode::Esc, KeyModifiers::empty())
    }

    #[test]
    fn esc_on_empty_form_closes_without_confirm() {
        let mut a = app();
        a.open_new_agent();
        handle_new_agent_key(&mut a, esc());
        assert_eq!(a.mode, Mode::Normal);
    }

    #[test]
    fn esc_with_typed_task_asks_to_confirm() {
        let mut a = app();
        a.open_new_agent();
        a.new_form.task.insert_str("do the thing");
        handle_new_agent_key(&mut a, esc());
        assert_eq!(a.mode, Mode::ConfirmDiscard);
        // [n]/esc keeps editing, task preserved
        handle_key(&mut a, esc());
        assert_eq!(a.mode, Mode::NewAgent);
        assert_eq!(a.new_form.task_text(), "do the thing");
        // reopen confirm, [y] discards and resets the form
        handle_new_agent_key(&mut a, esc());
        handle_key(&mut a, KeyEvent::new(KeyCode::Char('y'), KeyModifiers::empty()));
        assert_eq!(a.mode, Mode::Normal);
        assert!(a.new_form.task_text().is_empty());
    }
}
