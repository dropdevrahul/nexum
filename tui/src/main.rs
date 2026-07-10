//! crew — a repo-scoped dashboard for running, chatting with, and managing
//! coding agents across harnesses. Each agent runs its harness REPL in a PTY
//! rendered *inside* the TUI. Standalone: creates worktrees natively and keeps
//! its own registry; an optional nexum engine adds observed sessions + delegation.
//!
//!   crew                 # interactive dashboard
//!   crew --dump          # print the agent list once and exit (no TTY)
//!   crew --snapshot      # render one real frame to text (no TTY)
//!   crew --scripts DIR   # override the optional engine scripts dir

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
        KeyModifiers, MouseButton, MouseEventKind,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{backend::CrosstermBackend, Terminal};
use std::io;
use std::path::PathBuf;
use std::time::{Duration, Instant};

type Term = Terminal<CrosstermBackend<io::Stdout>>;

const HELP: &str = "\
crew — a terminal dashboard for coding agents across harnesses

USAGE:
  crew                         interactive dashboard (from inside a git repo)
  crew --new \"task\"            create a worktree + run the harness headless in it
       [--harness claude|opencode|cursor] [--model NAME] [--here]
       (--here runs in the current repo instead of a new worktree)
  crew --dump                  print the agent list once and exit (no TTY)
  crew --snapshot              render one frame to text (no TTY)
  crew --scripts DIR           optional nexum engine scripts dir
  crew --doctor                check git / harness CLIs / gh / engine
  crew --help | --version

KEYS: press ? inside the dashboard for the full keymap.";

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--help" || a == "-h") {
        println!("{HELP}");
        return Ok(());
    }
    if args.iter().any(|a| a == "--version" || a == "-V") {
        println!("crew {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args.iter().any(|a| a == "--doctor") {
        return doctor(&args);
    }
    let scripts_override = arg_value(&args, "--scripts");

    let cwd = std::env::current_dir()?;
    // crew is repo-scoped: worktrees, branches, status all need a git repo.
    let interactive = !args.iter().any(|a| a == "--dump" || a == "--snapshot");
    if interactive && !store::is_git_repo(&cwd) {
        eprintln!("crew: not inside a git repository (cd into one, or `git init`)");
        std::process::exit(1);
    }
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
    if let Some(task) = arg_value(&args, "--new") {
        return run_new(&app, &task, &args);
    }
    if args.iter().any(|a| a == "--snapshot") {
        match std::env::var("NEXUM_SNAPSHOT_MODE").as_deref() {
            Ok("new") => {
                app.open_new_agent();
                app.new_form.task.insert_str("add auth middleware");
            }
            Ok("help") => app.mode = app::Mode::Help,
            Ok("demo") => {
                // inject a rich sample fleet for docs/marketing screenshots
                use agent::{ManagedAgent, Row};
                let mk = |id: &str, h: &str, st: &str, task: &str, cost: f64, ago: f64| {
                    Row::from_managed(&ManagedAgent {
                        agent_id: id.into(), harness: Some(h.into()), model: Some("sonnet".into()),
                        worktree: Some(format!(".crew/worktrees/{}", id)), branch: Some(format!("crew/{}", id)),
                        status: Some(st.into()), cost_usd: Some(cost), task: Some(task.into()),
                        step_index: None, updated_ts: Some(app_now() - ago), pid: None, log_path: None,
                    })
                };
                app.all_rows = vec![
                    mk("billing-api", "cursor", "running", "add billing dashboard endpoint", 0.42, 12.0),
                    mk("auth-mw", "claude", "running", "add auth middleware + tests", 0.31, 40.0),
                    mk("flaky-test", "opencode", "failed", "fix flaky timezone test", 0.08, 120.0),
                    mk("readme", "claude", "done", "rewrite the README intro", 0.05, 600.0),
                    mk("dark-mode", "cursor", "done", "add dark-mode toggle to settings", 0.19, 3600.0),
                ];
                app.apply_filter();
                app.selected = 0;
                app.marked.insert("auth-mw".into());
                app.marked.insert("flaky-test".into());
            }
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

/// Headless launch from the shell: create a worktree natively and run the
/// harness once in it (inheriting the terminal), then print the worktree/branch.
/// Fully standalone — no engine required.
fn run_new(app: &App, task: &str, args: &[String]) -> Result<()> {
    let harness = arg_value(args, "--harness").unwrap_or_else(|| "claude".into());
    let model = arg_value(args, "--model").unwrap_or_else(|| default_model(&harness).into());
    let slug = app::slugify(task);
    // --here runs in the current repo; default is a fresh isolated worktree.
    let here = args.iter().any(|a| a == "--here");
    let (wt, where_) = if here {
        let repo = app.store.repo_root.to_string_lossy().to_string();
        (repo.clone(), format!("repo {repo}"))
    } else {
        match app.store.create_worktree(&slug) {
            Ok(p) => (p.clone(), format!("branch crew/{slug} · worktree {p}")),
            Err(e) => {
                eprintln!("crew: worktree failed: {e}");
                std::process::exit(1);
            }
        }
    };
    let argv = headless_argv(&harness, &model, task);
    println!("crew: launching {harness} ({model}) in {wt}");
    let status = std::process::Command::new(&argv[0])
        .args(&argv[1..])
        .current_dir(&wt)
        .status();
    match status {
        Ok(s) => println!("crew: done ({}) · {where_}",
            s.code().map(|c| c.to_string()).unwrap_or_else(|| "signal".into())),
        Err(e) => {
            eprintln!("crew: could not run {}: {e}", argv[0]);
            std::process::exit(1);
        }
    }
    Ok(())
}

/// Print an environment checklist so setup problems are obvious.
fn doctor(args: &[String]) -> Result<()> {
    let cwd = std::env::current_dir()?;
    let scripts_dir = arg_value(args, "--scripts")
        .map(PathBuf::from)
        .or_else(|| std::env::var("NEXUM_SCRIPTS").ok().map(PathBuf::from))
        .unwrap_or_else(|| store::git_toplevel(&cwd).join("scripts"));
    let mark = |ok: bool| if ok { "✓" } else { "✗" };

    println!("crew {} — environment check\n", env!("CARGO_PKG_VERSION"));
    println!("  {} git installed", mark(app::on_path("git")));
    println!("  {} inside a git repository", mark(store::is_git_repo(&cwd)));
    println!("  {} gh (GitHub CLI, for push→PR / open)", mark(app::gh_available()));
    println!("\n  harness CLIs:");
    for h in app::HARNESSES {
        println!("    {} {:<9} ({})", mark(app::harness_installed(h)), h, app::harness_bin(h));
    }
    let engine = scripts_dir.join("store.py").is_file();
    println!("\n  {} optional nexum engine ({})",
        mark(engine), scripts_dir.display());
    if !engine {
        println!("      (standalone mode — observed sessions & delegation disabled)");
    }
    Ok(())
}

fn default_model(harness: &str) -> &'static str {
    match harness {
        "opencode" => "anthropic/claude-sonnet-4-6",
        "cursor" => "auto",
        _ => "sonnet",
    }
}

/// One-shot headless argv per harness (mirrors the interactive presets minus the
/// REPL). Overridable via `NEXUM_HEADLESS_CMD_<HARNESS>` (prompt appended).
fn headless_argv(harness: &str, model: &str, task: &str) -> Vec<String> {
    if let Ok(over) = std::env::var(format!("NEXUM_HEADLESS_CMD_{}", harness.to_uppercase())) {
        if !over.trim().is_empty() {
            let mut v: Vec<String> = over.split_whitespace().map(String::from).collect();
            v.push(task.into());
            return v;
        }
    }
    match harness {
        "opencode" => vec!["opencode".into(), "run".into(), task.into(), "--model".into(), model.into()],
        "cursor" => vec!["cursor-agent".into(), "-p".into(), task.into(), "--model".into(), model.into()],
        _ => vec!["claude".into(), "-p".into(), task.into(), "--model".into(), model.into()],
    }
}

fn dump_table(app: &App) {
    println!("crew agents for {} — {} rows", app.store.repo_root.display(), app.rows.len());
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
    // gh is a network call — poll PRs on a slow timer, first pass ~immediately.
    let pr_every = Duration::from_secs(20);
    let mut last_pr = Instant::now()
        .checked_sub(pr_every)
        .unwrap_or_else(Instant::now);
    // per-row git status is heavier than the list refresh — run it less often.
    let dirty_every = Duration::from_millis(2500);
    let mut last_dirty = Instant::now()
        .checked_sub(dirty_every)
        .unwrap_or_else(Instant::now);

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
                    MouseEventKind::Down(MouseButton::Left) => {
                        // list occupies the left ~34%; items start at y=2 (header
                        // row + top border). Click selects that row.
                        let w = terminal.size().map(|s| s.width).unwrap_or(80);
                        let left_w = w * 34 / 100;
                        if m.column < left_w && m.row >= 2 {
                            let idx = (m.row - 2) as usize;
                            if idx < app.rows.len() {
                                app.selected = idx;
                            }
                        }
                    }
                    _ => {}
                },
                _ => {}
            }
        }

        // an editor/shell was requested: drop out of the TUI, run it, come back
        if let Some((cwd, argv)) = app.pending_shell.take() {
            run_suspended(terminal, &cwd, &argv)?;
        }

        if last_refresh.elapsed() >= refresh_every {
            if app.mode == Mode::Log {
                app.tick_log(); // live-follow the open log (tail -f)
            } else {
                let _ = app.refresh();
            }
            last_refresh = Instant::now();
        }

        if last_pr.elapsed() >= pr_every && app.mode != Mode::Terminal {
            app.poll_prs();
            last_pr = Instant::now();
        }

        if last_dirty.elapsed() >= dirty_every && app.mode != Mode::Terminal {
            app.compute_dirty();
            last_dirty = Instant::now();
        }

        if app.should_quit {
            app.save_ui_prefs(); // remember view toggles for next run
            return Ok(());
        }
    }
}

/// Suspend the TUI (leave raw mode + alternate screen), run `argv` in `cwd`
/// attached to the real terminal, then restore the dashboard. Used for `e`
/// (editor) and `!` (shell) — the child fully owns the terminal while it runs.
fn run_suspended(terminal: &mut Term, cwd: &str, argv: &[String]) -> Result<()> {
    if argv.is_empty() {
        return Ok(());
    }
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen, DisableMouseCapture)?;
    let _ = std::process::Command::new(&argv[0])
        .args(&argv[1..])
        .current_dir(cwd)
        .status();
    enable_raw_mode()?;
    execute!(terminal.backend_mut(), EnterAlternateScreen, EnableMouseCapture)?;
    terminal.clear()?;
    Ok(())
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
    // Ctrl-d / Ctrl-u half-page the list (checked before plain-letter binds).
    if app.mode == Mode::Normal && key.modifiers.contains(KeyModifiers::CONTROL) {
        match code {
            KeyCode::Char('d') => { app.move_sel(8); return; }
            KeyCode::Char('u') => { app.move_sel(-8); return; }
            _ => {}
        }
    }
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
            KeyCode::Char('g') | KeyCode::Home => app.sel_top(),
            KeyCode::Char('G') | KeyCode::End => app.sel_bottom(),
            KeyCode::PageDown => app.move_sel(10),
            KeyCode::PageUp => app.move_sel(-10),
            KeyCode::Char(']') => app.jump_failed(1),
            KeyCode::Char('[') => app.jump_failed(-1),
            KeyCode::Char('y') => app.yank_worktree(),
            KeyCode::Char('r') => { let _ = app.refresh(); }
            KeyCode::Char('n') => app.open_new_agent(),
            KeyCode::Char('s') => {
                let has_live = !app.marked.is_empty()
                    || app.selected_row().map(|r| r.interactive).unwrap_or(false);
                if has_live {
                    app.mode = Mode::ConfirmStop;
                } else {
                    app.status_msg = "only interactive agents can be stopped".into();
                }
            }
            KeyCode::Char(' ') => app.toggle_mark(),
            KeyCode::Char('A') => app.mark_all_visible(),
            KeyCode::Esc => {
                if !app.marked.is_empty() {
                    app.clear_marks();
                } else if app.category != app::Category::All {
                    app.set_category(app::Category::All);
                }
            }
            KeyCode::Char('x') => {
                if app.marked.is_empty() { app.remove_selected(); }
                else { app.mode = Mode::ConfirmRemove; }
            }
            KeyCode::Char('L') => app.open_rename(),
            KeyCode::Char('D') => app.duplicate_selected(),
            KeyCode::Char('S') => {
                if app.agents.iter_mut().any(|a| a.is_alive()) {
                    app.mode = Mode::ConfirmStopAll;
                } else {
                    app.status_msg = "no running agents".into();
                }
            }
            KeyCode::Char('c') => app.clear_finished(),
            KeyCode::Char('R') => app.open_retry(),
            KeyCode::Char('P') => app.open_push(),
            KeyCode::Char('o') => app.show_worktree(),
            KeyCode::Char('Y') => app.yank_branch(),
            KeyCode::Char('O') => app.open_branch_web(),
            KeyCode::Char('f') => app.fetch_selected(),
            KeyCode::Char('e') => app.open_editor(),
            KeyCode::Char('!') => app.open_shell(),
            KeyCode::Char('d') => app.show_diff(),
            KeyCode::Char('l') => app.show_log(),
            KeyCode::Char('h') => { app.show_observed = !app.show_observed; let _ = app.refresh(); }
            KeyCode::Char('1') => app.set_category(app::Category::All),
            KeyCode::Char('2') => app.set_category(app::Category::Running),
            KeyCode::Char('3') => app.set_category(app::Category::Agents),
            KeyCode::Char('4') => app.set_category(app::Category::Sessions),
            KeyCode::Char('t') => app.cycle_sort(),
            KeyCode::Char(',') => app.open_settings(),
            KeyCode::Char('/') => app.mode = Mode::Filter,
            KeyCode::Char('?') => { app.help_scroll = 0; app.mode = Mode::Help; }
            KeyCode::Enter => app.open_selected(),
            _ => {}
        },
        Mode::Help => match code {
            KeyCode::Char('j') | KeyCode::Down => app.help_scroll = app.help_scroll.saturating_add(1),
            KeyCode::Char('k') | KeyCode::Up => app.help_scroll = app.help_scroll.saturating_sub(1),
            _ => app.mode = Mode::Normal,
        },
        Mode::Diff | Mode::Log => handle_viewer_key(app, key),
        Mode::Retry => match code {
            KeyCode::Left | KeyCode::Char('h') => app.cycle_retry_harness(-1),
            KeyCode::Right | KeyCode::Char('l') => app.cycle_retry_harness(1),
            KeyCode::Enter => app.confirm_retry(),
            KeyCode::Esc | KeyCode::Char('q') => app.mode = Mode::Normal,
            _ => {}
        },
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
        Mode::ConfirmStopAll => match code {
            KeyCode::Char('y') => { app.stop_all(); app.mode = Mode::Normal; }
            KeyCode::Char('n') | KeyCode::Esc => app.mode = Mode::Normal,
            _ => {}
        },
        Mode::ConfirmPush => match code {
            KeyCode::Char('y') | KeyCode::Enter => app.confirm_push(),
            KeyCode::Char('n') | KeyCode::Esc => app.mode = Mode::Normal,
            _ => {}
        },
        Mode::Settings => match code {
            KeyCode::Left | KeyCode::Char('h') => app.settings_change(-1),
            KeyCode::Right | KeyCode::Char('l') | KeyCode::Char(' ') => app.settings_change(1),
            KeyCode::Tab | KeyCode::Down | KeyCode::Char('j') => app.settings_move(1),
            KeyCode::BackTab | KeyCode::Up | KeyCode::Char('k') => app.settings_move(-1),
            KeyCode::Enter | KeyCode::Char('s') => app.save_settings(),
            KeyCode::Esc | KeyCode::Char('q') => app.cancel_settings(),
            _ => {}
        },
        Mode::ConfirmRemove => match code {
            KeyCode::Char('y') => { app.remove_selected(); app.mode = Mode::Normal; }
            KeyCode::Char('n') | KeyCode::Esc => app.mode = Mode::Normal,
            _ => {}
        },
        Mode::Rename => match code {
            KeyCode::Enter => app.commit_rename(),
            KeyCode::Esc => app.mode = Mode::Normal,
            KeyCode::Backspace => { app.rename_buf.pop(); }
            KeyCode::Char(c) => app.rename_buf.push(c),
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

/// Shared key handling for the full-screen diff and log viewers: scrolling,
/// vim jumps, and an in-viewer `/` search (Enter/n/N to cycle matches).
fn handle_viewer_key(app: &mut App, key: KeyEvent) {
    let code = key.code;
    if app.find_input {
        match code {
            KeyCode::Enter => { app.find_input = false; app.find_next(1); }
            KeyCode::Esc => { app.find_input = false; app.find_query.clear(); app.status_msg.clear(); }
            KeyCode::Backspace => { app.find_query.pop(); }
            KeyCode::Char(c) => app.find_query.push(c),
            _ => {}
        }
        return;
    }
    match code {
        KeyCode::Char('j') | KeyCode::Down => app.scroll_viewer(1),
        KeyCode::Char('k') | KeyCode::Up => app.scroll_viewer(-1),
        KeyCode::PageDown | KeyCode::Char(' ') => app.scroll_viewer(20),
        KeyCode::PageUp => app.scroll_viewer(-20),
        KeyCode::Char('g') | KeyCode::Home => app.scroll_viewer(i64::MIN),
        KeyCode::Char('G') | KeyCode::End => app.scroll_viewer(i64::MAX),
        KeyCode::Char('/') => { app.find_input = true; app.find_query.clear(); }
        KeyCode::Char('n') => app.find_next(1),
        KeyCode::Char('N') => app.find_next(-1),
        KeyCode::Char('r') if app.mode == Mode::Log => app.show_log(),
        KeyCode::Char('q') | KeyCode::Esc => {
            if app.mode == Mode::Log { app.log_path_open = None; }
            app.mode = Mode::Normal;
            app.find_query.clear();
            app.find_input = false;
        }
        _ => {}
    }
}

fn handle_new_agent_key(app: &mut App, key: KeyEvent) {
    use app::{FIELD_HARNESS, FIELD_IMAGES, FIELD_MODEL, FIELD_TASK, FIELD_WORKFLOW, FIELD_WORKTREE, FIELD_COUNT};
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
        FIELD_WORKFLOW => match key.code {
            KeyCode::Left | KeyCode::Char('h') => f.cycle_workflow(-1),
            KeyCode::Right | KeyCode::Char('l') | KeyCode::Char(' ') => f.cycle_workflow(1),
            _ => {}
        },
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
        KeyCode::BackTab => b"\x1b[Z".to_vec(), // shift-tab: lets Claude Code cycle modes

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

fn app_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
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

    fn ch(c: char) -> KeyEvent {
        KeyEvent::new(KeyCode::Char(c), KeyModifiers::empty())
    }

    #[test]
    fn backtab_forwards_shift_tab_sequence() {
        // shift-tab must reach the PTY as CSI Z so Claude Code can cycle modes.
        let bytes = key_to_bytes(KeyEvent::new(KeyCode::BackTab, KeyModifiers::empty()));
        assert_eq!(bytes, b"\x1b[Z");
    }

    #[test]
    fn form_tab_reaches_workflow_field_and_cycles() {
        let mut a = app();
        a.open_new_agent();
        // Tab off the task editor lands on the workflow picker.
        handle_new_agent_key(&mut a, KeyEvent::new(KeyCode::Tab, KeyModifiers::empty()));
        assert_eq!(a.new_form.field, app::FIELD_WORKFLOW);
        assert_eq!(a.new_form.workflow_idx, 0);
        handle_new_agent_key(&mut a, KeyEvent::new(KeyCode::Right, KeyModifiers::empty()));
        assert_eq!(a.new_form.workflow_idx, 1);
    }

    #[test]
    fn diff_mode_opens_scrolls_and_closes() {
        let mut a = app();
        // no worktree selected → show_diff reports, stays Normal
        a.show_diff();
        assert_eq!(a.mode, Mode::Normal);
        // simulate an open diff view and drive scroll/close keys
        a.diff_text = (0..50).map(|i| format!("line {i}")).collect::<Vec<_>>().join("\n");
        a.mode = Mode::Diff;
        handle_key(&mut a, ch('j'));
        assert_eq!(a.diff_scroll, 1);
        handle_key(&mut a, KeyEvent::new(KeyCode::PageDown, KeyModifiers::empty()));
        assert_eq!(a.diff_scroll, 21);
        handle_key(&mut a, ch('k'));
        assert_eq!(a.diff_scroll, 20);
        handle_key(&mut a, ch('q'));
        assert_eq!(a.mode, Mode::Normal);
    }

    #[test]
    fn log_view_reads_file_scrolls_and_closes() {
        use agent::{ManagedAgent, Row};
        let dir = std::env::temp_dir().join(format!("nexum-logv-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let logp = dir.join("a.log");
        std::fs::write(&logp, "line1\nERROR boom\nline3\n").unwrap();
        let ma = ManagedAgent {
            agent_id: "m1".into(), harness: Some("cursor".into()), model: None,
            worktree: None, branch: None, status: Some("running".into()),
            cost_usd: None, task: Some("t".into()), step_index: None,
            updated_ts: None, pid: None,
            log_path: Some(logp.to_string_lossy().into_owned()),
        };
        let mut a = app();
        a.rows = vec![Row::from_managed(&ma)];
        a.selected = 0;
        a.show_log();
        assert_eq!(a.mode, Mode::Log);
        assert!(a.log_text.contains("ERROR boom"), "{}", a.log_text);
        handle_key(&mut a, ch('k')); // scroll
        handle_key(&mut a, ch('q'));
        assert_eq!(a.mode, Mode::Normal);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn log_follow_tracks_bottom_but_respects_scrollback() {
        use agent::{ManagedAgent, Row};
        let dir = std::env::temp_dir().join(format!("nexum-logf-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let logp = dir.join("f.log");
        std::fs::write(&logp, "l1\nl2\nl3\n").unwrap();
        let ma = ManagedAgent {
            agent_id: "f".into(), harness: Some("cursor".into()), model: None,
            worktree: None, branch: None, status: Some("running".into()),
            cost_usd: None, task: None, step_index: None, updated_ts: None, pid: None,
            log_path: Some(logp.to_string_lossy().into_owned()),
        };
        let mut a = app();
        a.rows = vec![Row::from_managed(&ma)];
        a.selected = 0;
        a.show_log();
        assert_eq!(a.log_scroll, 2); // parked at bottom (3 lines → idx 2)

        // new output arrives; following → scroll advances to the new bottom
        std::fs::write(&logp, "l1\nl2\nl3\nl4\nl5\n").unwrap();
        a.tick_log();
        assert_eq!(a.log_scroll, 4);

        // reader scrolls up; new output must NOT yank them back down
        a.scroll_log(-3); // now at 1
        assert_eq!(a.log_scroll, 1);
        std::fs::write(&logp, "l1\nl2\nl3\nl4\nl5\nl6\n").unwrap();
        a.tick_log();
        assert_eq!(a.log_scroll, 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Full drive of a live agent: launch a stub PTY agent, focus its terminal,
    /// forward a keystroke, leave, bulk-stop, then clear it.
    #[test]
    fn e2e_spawn_chat_stopall_clear() {
        let dir = std::env::temp_dir().join(format!("nexum-e2e-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::env::set_var("NEXUM_INTERACTIVE_CMD_CLAUDE", "cat");
        let mut a = App::new(store::Store::new(dir.clone(), dir.join("scripts")));
        a.term_size = (80, 24);

        // launch via the form, running in the dir itself (no worktree.py)
        a.open_new_agent();
        a.new_form.task.insert_str("hello agent");
        a.new_form.worktree_new = false;
        a.spawn_from_form();
        assert_eq!(a.mode, Mode::Terminal, "should drop into chat");
        assert_eq!(a.agents.len(), 1);
        assert!(a.agents[0].is_alive());

        // forward a keystroke to the PTY, then leave the terminal (Ctrl-o)
        handle_terminal_key(&mut a, ch('x'));
        handle_terminal_key(&mut a, KeyEvent::new(KeyCode::Char('o'), KeyModifiers::CONTROL));
        assert_eq!(a.mode, Mode::Normal);

        // review its worktree diff (dir isn't a git repo → friendly message)
        handle_key(&mut a, ch('d'));
        assert_eq!(a.mode, Mode::Diff);
        assert!(!a.diff_text.is_empty());
        handle_key(&mut a, esc());

        // bulk stop-all: S → confirm → y
        handle_key(&mut a, ch('S'));
        assert_eq!(a.mode, Mode::ConfirmStopAll);
        handle_key(&mut a, ch('y'));
        assert_eq!(a.mode, Mode::Normal);
        // give the kill a moment to land
        std::thread::sleep(std::time::Duration::from_millis(200));
        assert!(!a.agents[0].is_alive(), "stop_all should have killed the agent");

        // clear finished drops the dead proc
        handle_key(&mut a, ch('c'));
        assert!(a.agents.is_empty(), "clear_finished should remove the dead agent");

        std::env::remove_var("NEXUM_INTERACTIVE_CMD_CLAUDE");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// e2e: `crew --new` creates a worktree natively and runs the harness
    /// headless in it (stub harness writes a marker) — no engine needed.
    #[test]
    fn e2e_new_launches_headless_in_worktree() {
        use std::process::Command;
        let manifest = env!("CARGO_MANIFEST_DIR");
        let bin = PathBuf::from(manifest).join("target/debug/crew");
        let fake = PathBuf::from(manifest).parent().unwrap().join("tests/fixtures/fake_harness.py");
        if !bin.exists() || !fake.exists() { return; }
        let py = std::env::var("NEXUM_PYTHON").unwrap_or_else(|_| "python3".into());

        let repo = std::env::temp_dir().join(format!("crew-new-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&repo);
        std::fs::create_dir_all(&repo).unwrap();
        let g = |a: &[&str]| { Command::new("git").arg("-C").arg(&repo).args(a).output().unwrap(); };
        g(&["init", "-q"]); g(&["config", "user.email", "t@t"]); g(&["config", "user.name", "t"]);
        std::fs::write(repo.join("seed.txt"), "x").unwrap();
        g(&["add", "seed.txt"]); g(&["commit", "-qm", "init"]);

        let out = Command::new(&bin)
            .args(["--new", "hello task", "--harness", "claude"])
            .current_dir(&repo)
            .env("NEXUM_HEADLESS_CMD_CLAUDE", format!("{} {}", py, fake.display()))
            .output().unwrap();
        assert!(out.status.success(), "stderr: {}", String::from_utf8_lossy(&out.stderr));
        let stdout = String::from_utf8_lossy(&out.stdout);
        assert!(stdout.contains("branch crew/"), "stdout: {stdout}");

        // the stub harness ran inside the created worktree
        let slug = stdout.split("branch crew/").nth(1).unwrap()
            .split_whitespace().next().unwrap().to_string();
        let marker = repo.join(".crew/worktrees").join(&slug).join("fake_out.txt");
        assert!(marker.exists(), "marker missing at {}", marker.display());
        let _ = std::fs::remove_dir_all(&repo);
    }

    /// e2e: push commits the worktree and pushes its branch to origin (a local
    /// bare remote — no network). Proves the push path works standalone.
    #[test]
    fn e2e_push_pushes_branch_to_origin() {
        use std::process::Command;
        let base = std::env::temp_dir().join(format!("crew-push-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        let origin = base.join("origin.git");
        let work = base.join("work");
        Command::new("git").args(["init", "--bare", "-q"]).arg(&origin).output().unwrap();
        let g = |a: &[&str]| { Command::new("git").arg("-C").arg(&work).args(a).output().unwrap(); };
        std::fs::create_dir_all(&work).unwrap();
        g(&["init", "-q"]); g(&["config", "user.email", "t@t"]); g(&["config", "user.name", "t"]);
        std::fs::write(work.join("seed.txt"), "x").unwrap();
        g(&["add", "seed.txt"]); g(&["commit", "-qm", "init"]);
        g(&["remote", "add", "origin", origin.to_str().unwrap()]);

        let work = std::fs::canonicalize(&work).unwrap();
        let mut app = App::new(store::Store::new(work.clone(), work.join("no-scripts")));
        let wt = app.store.create_worktree("push-me").unwrap();
        std::fs::write(std::path::Path::new(&wt).join("new.txt"), "hi").unwrap();
        app.push_queue = vec![(wt, "crew/push-me".into(), "add new.txt".into())];
        app.confirm_push();

        // origin now has the branch
        let out = Command::new("git").arg("-C").arg(&origin)
            .args(["branch", "--list", "crew/push-me"]).output().unwrap();
        assert!(String::from_utf8_lossy(&out.stdout).contains("crew/push-me"),
            "branch not on origin; status: {}", app.status_msg);
        let _ = std::fs::remove_dir_all(&base);
    }

    /// e2e: retry/re-delegate from the TUI dispatches the task on the chosen
    /// harness and records a new agent row (polled from the store).
    #[test]
    fn e2e_retry_redelegates_and_records_agent() {
        use agent::{ManagedAgent, Row};
        use std::process::Command;
        let manifest = env!("CARGO_MANIFEST_DIR");
        let root = PathBuf::from(manifest).parent().unwrap().to_path_buf();
        let scripts = root.join("scripts");
        let fake = root.join("tests/fixtures/fake_harness.py");
        if !fake.exists() { return; }
        let py = std::env::var("NEXUM_PYTHON").unwrap_or_else(|_| "python3".into());

        let repo = std::env::temp_dir().join(format!("nexum-retry-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&repo);
        std::fs::create_dir_all(&repo).unwrap();
        let git = |a: &[&str]| { Command::new("git").arg("-C").arg(&repo).args(a).output().unwrap(); };
        git(&["init", "-q"]); git(&["config", "user.email", "t@t"]); git(&["config", "user.name", "t"]);
        std::fs::write(repo.join("seed.txt"), "seed").unwrap();
        git(&["add", "seed.txt"]); git(&["commit", "-qm", "init"]);

        let data = repo.join(".nexum-data");
        std::env::set_var("CLAUDE_PLUGIN_DATA", &data);
        std::env::set_var("NEXUM_HARNESS_CMD_CURSOR", format!("{} {}", py, fake.display()));

        let mut app = App::new(store::Store::new(std::fs::canonicalize(&repo).unwrap(), scripts.clone()));
        let ma = ManagedAgent {
            agent_id: "orig".into(), harness: Some("cursor".into()), model: None,
            worktree: None, branch: None, status: Some("failed".into()), cost_usd: None,
            task: Some("retry writes a file".into()), step_index: None, updated_ts: None,
            pid: None, log_path: None,
        };
        app.rows = vec![Row::from_managed(&ma)];
        app.selected = 0;
        app.open_retry();
        assert_eq!(app.mode, Mode::Retry);
        assert_eq!(app.retry_harness_idx, 2); // cursor
        app.confirm_retry();
        assert_eq!(app.mode, Mode::Normal);
        let id = app.status_msg.rsplit("→ ").next().unwrap().trim().to_string();
        assert!(id.starts_with("retry_"), "status: {}", app.status_msg);

        // poll the store until the detached dispatch finishes
        let deadline = std::time::Instant::now() + Duration::from_secs(20);
        let mut done = false;
        while std::time::Instant::now() < deadline {
            let out = Command::new(&py).arg(scripts.join("store.py"))
                .args(["agent-get", "--id", &id])
                .env("CLAUDE_PLUGIN_DATA", &data).output().unwrap();
            let s = String::from_utf8_lossy(&out.stdout);
            if s.contains("\"status\": \"done\"") { done = true; break; }
            std::thread::sleep(Duration::from_millis(200));
        }
        assert!(done, "retry agent never reached done");

        std::env::remove_var("CLAUDE_PLUGIN_DATA");
        std::env::remove_var("NEXUM_HARNESS_CMD_CURSOR");
        let _ = std::fs::remove_dir_all(&repo);
    }

    /// Cross-boundary e2e: a delegated sub-agent recorded by dispatch.py (via the
    /// same path the MCP `delegate` tool uses) must appear in the TUI's list.
    #[test]
    fn e2e_delegated_agent_shows_in_tui() {
        use std::process::Command;
        let manifest = env!("CARGO_MANIFEST_DIR"); // .../nexum/tui
        let root = PathBuf::from(manifest).parent().unwrap().to_path_buf(); // .../nexum
        let scripts = root.join("scripts");
        let fake = root.join("tests/fixtures/fake_harness.py");
        if !fake.exists() {
            return; // fixtures not present (partial checkout) — skip
        }
        let py = std::env::var("NEXUM_PYTHON").unwrap_or_else(|_| "python3".into());

        // throwaway git repo
        let repo = std::env::temp_dir().join(format!("nexum-deleg-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&repo);
        std::fs::create_dir_all(&repo).unwrap();
        let git = |args: &[&str]| {
            Command::new("git").arg("-C").arg(&repo).args(args).output().unwrap();
        };
        git(&["init", "-q"]);
        git(&["config", "user.email", "t@t"]);
        git(&["config", "user.name", "t"]);
        std::fs::write(repo.join("seed.txt"), "seed").unwrap();
        git(&["add", "seed.txt"]);
        git(&["commit", "-qm", "init"]);

        let data = repo.join(".nexum-data");
        let step = repo.join("step.json");
        std::fs::write(&step,
            r#"{"title":"delegated bit","objective":"write file","acceptance":"test -f fake_out.txt","files":["fake_out.txt"],"scope_deny":[]}"#).unwrap();

        // run dispatch exactly like the delegation MCP does, with the fake harness
        let out = Command::new(&py)
            .arg(scripts.join("dispatch.py"))
            .args(["--harness", "cursor", "--model", "auto",
                   "--repo", repo.to_str().unwrap(),
                   "--new-worktree", "--slug", "deleg",
                   "--step-file", step.to_str().unwrap(),
                   "--agent-id", "tui_deleg_e2e"])
            .env("CLAUDE_PLUGIN_DATA", &data)
            .env("NEXUM_HARNESS_CMD_CURSOR", format!("{} {}", py, fake.display()))
            .env("PYTHONPATH", &scripts)
            .output()
            .unwrap();
        assert!(out.status.success(), "dispatch failed: {}",
                String::from_utf8_lossy(&out.stderr));

        // now the TUI store must read that agent back and refresh must list it
        std::env::set_var("CLAUDE_PLUGIN_DATA", &data);
        let mut app = App::new(store::Store::new(
            std::fs::canonicalize(&repo).unwrap(), scripts.clone()));
        app.refresh().unwrap();
        let found = app.rows.iter().find(|r| r.id == "tui_deleg_e2e")
            .expect("delegated agent should appear in the TUI list");
        assert_eq!(found.harness, "cursor");
        assert_eq!(found.status, "done");
        assert!(found.worktree.is_some());

        std::env::remove_var("CLAUDE_PLUGIN_DATA");
        let _ = std::fs::remove_dir_all(&repo);
    }
}
