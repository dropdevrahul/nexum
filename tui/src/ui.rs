//! Rendering. Master-detail: an agent list on the left; on the right either the
//! selected agent's *embedded terminal* (interactive) or a preview (observed).

use crate::agent::{rel_time, Kind};
use crate::app::{App, Mode};
use ratatui::{
    layout::{Constraint, Direction, Layout, Margin, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{
        Block, BorderType, Borders, Clear, List, ListItem, ListState, Paragraph, Scrollbar,
        ScrollbarOrientation, ScrollbarState, Wrap,
    },
    Frame,
};
use tui_term::widget::PseudoTerminal;

// ── theme ────────────────────────────────────────────────────────────────
// One accent so the UI reads as a system (lazygit/k9s style). Borders are
// rounded and dim until a panel is focused, then they light up in the accent.
const ACCENT: Color = Color::Cyan;
const ACCENT_DIM: Color = Color::DarkGray;

/// A rounded panel with a dim border and a subtle title.
fn panel(title: &str) -> Block<'static> {
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(ACCENT_DIM))
        .title(Span::styled(title.to_string(), Style::default().fg(Color::Gray)))
}

/// A rounded panel whose border + title are lit in the accent (focused).
fn panel_focused(title: &str, color: Color) -> Block<'static> {
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(color))
        .title(Span::styled(title.to_string(), Style::default().fg(color).add_modifier(Modifier::BOLD)))
}

/// A rounded modal dialog block with a caller-styled title span.
fn dialog(title: Span<'static>) -> Block<'static> {
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(ACCENT_DIM))
        .title(title)
}

/// Braille spinner frame for "running", cycled from wall-clock time.
fn spinner() -> &'static str {
    const F: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
    let i = (now_millis() / 100 % 10) as usize;
    F[i]
}

/// Render a vertical scrollbar on the right edge of `area` for a scrolled view.
fn scrollbar(f: &mut Frame, area: Rect, pos: u16, total: usize) {
    if total == 0 {
        return;
    }
    let mut state = ScrollbarState::new(total).position(pos as usize);
    f.render_stateful_widget(
        Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .begin_symbol(None).end_symbol(None)
            .thumb_style(Style::default().fg(ACCENT)),
        area.inner(Margin { vertical: 1, horizontal: 0 }),
        &mut state,
    );
}

fn status_color(status: &str) -> Color {
    match status {
        "running" => Color::Green,
        "done" => Color::Green,
        "failed" => Color::Red,
        "resumable" => Color::Yellow,
        "exited" => Color::DarkGray,
        _ => Color::Cyan, // observed
    }
}

/// Per-harness accent so you can scan the list by tool at a glance (k9s-style).
fn harness_color(h: &str) -> Color {
    match h {
        "claude" => Color::Magenta,
        "cursor" => Color::Blue,
        "opencode" => Color::Green,
        "session" => Color::DarkGray,
        _ => Color::Gray,
    }
}

/// A compact unicode meter like `███░░░` for a 0–100 percentage.
fn meter(pct: f64, width: usize) -> String {
    let p = (pct / 100.0).clamp(0.0, 1.0);
    let filled = (p * width as f64).round() as usize;
    let mut s = String::new();
    for i in 0..width {
        s.push(if i < filled { '█' } else { '░' });
    }
    s
}

fn status_icon(status: &str) -> &'static str {
    match status {
        "running" => "●",
        "done" => "✓",
        "failed" => "✗",
        "resumable" => "▷",
        "exited" => "□",
        _ => "·",
    }
}

pub fn draw(f: &mut Frame, app: &App) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Min(5), Constraint::Length(1)])
        .split(f.area());

    draw_header(f, app, chunks[0]);

    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(34), Constraint::Percentage(66)])
        .split(chunks[1]);
    draw_list(f, app, cols[0]);
    draw_right(f, app, cols[1]);

    draw_footer(f, app, chunks[2]);

    match app.mode {
        Mode::NewAgent => draw_form(f, app),
        Mode::Help => draw_help(f, app),
        Mode::Diff => draw_diff(f, app),
        Mode::Log => draw_log(f, app),
        Mode::ConfirmStop => draw_confirm(f, app),
        Mode::ConfirmStopAll => draw_confirm_stop_all(f, app),
        Mode::ConfirmQuit => draw_confirm_quit(f, app),
        Mode::ConfirmDiscard => draw_confirm_discard(f),
        Mode::Retry => draw_retry(f, app),
        Mode::ConfirmPush => draw_confirm_push(f, app),
        Mode::Settings => draw_settings(f, app),
        Mode::ConfirmRemove => draw_confirm_remove(f, app),
        Mode::Rename => draw_rename(f, app),
        Mode::Palette => draw_palette(f, app),
        Mode::Broadcast => draw_broadcast(f, app),
        Mode::About => draw_about(f, app),
        _ => {}
    }
}

/// Fuzzy command palette (`:`) — a filterable list of actions.
fn draw_palette(f: &mut Frame, app: &App) {
    let area = centered(60, 70, f.area());
    f.render_widget(Clear, area);
    let matches = app.palette_matches();
    let items: Vec<ListItem> = matches
        .iter()
        .map(|&i| {
            let (label, _) = App::PALETTE[i];
            ListItem::new(Line::from(Span::raw(format!("  {}", label))))
        })
        .collect();
    let title = format!(" : {}_ ", app.palette_query);
    let list = List::new(items)
        .block(panel_focused(&title, ACCENT))
        .highlight_style(Style::default().bg(ACCENT).fg(Color::Black).add_modifier(Modifier::BOLD))
        .highlight_symbol("▶ ");
    let mut state = ListState::default();
    if !matches.is_empty() {
        state.select(Some(app.palette_sel.min(matches.len() - 1)));
    }
    f.render_stateful_widget(list, area, &mut state);
}

/// Broadcast composer (`b`) — one message sent to every marked live agent.
fn draw_broadcast(f: &mut Frame, app: &App) {
    let area = centered(64, 26, f.area());
    f.render_widget(Clear, area);
    let n = app.target_live_count();
    let lines = vec![
        Line::from(""),
        Line::from(Span::styled(format!("  Send to {} live agent(s):", n),
            Style::default().fg(Color::Gray))),
        Line::from(vec![Span::raw("  "),
            Span::styled(format!("{}_", app.broadcast_buf),
                Style::default().fg(Color::Green).add_modifier(Modifier::BOLD))]),
        Line::from(""),
        Line::from(Span::styled("  [enter] send   [esc] cancel", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false })
        .block(panel_focused(" broadcast ", Color::Green)), area);
}

/// About / version splash (`=`).
fn draw_about(f: &mut Frame, app: &App) {
    let area = centered(56, 46, f.area());
    f.render_widget(Clear, area);
    let engine = if app.store.has_engine() { "connected" } else { "standalone" };
    let lines = vec![
        Line::from(""),
        Line::from(Span::styled(format!("  crew {}", env!("CARGO_PKG_VERSION")),
            Style::default().fg(Color::White).add_modifier(Modifier::BOLD))),
        Line::from(Span::styled("  a terminal dashboard for coding agents", Style::default().fg(Color::Gray))),
        Line::from(""),
        Line::from(vec![Span::styled("  repo    ", Style::default().fg(Color::DarkGray)),
            Span::raw(app.store.repo_root.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default())]),
        Line::from(vec![Span::styled("  engine  ", Style::default().fg(Color::DarkGray)),
            Span::raw(engine.to_string())]),
        Line::from(vec![Span::styled("  agents  ", Style::default().fg(Color::DarkGray)),
            Span::raw(app.all_rows.len().to_string())]),
        Line::from(""),
        Line::from(Span::styled("  [:] command palette   [?] full keymap", Style::default().fg(Color::Gray))),
        Line::from(Span::styled("  any key closes", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines).block(panel_focused(" about ", ACCENT)), area);
}

fn draw_confirm_remove(f: &mut Frame, app: &App) {
    let area = centered(52, 20, f.area());
    f.render_widget(Clear, area);
    let lines = vec![
        Line::from(""),
        Line::from(Span::raw(format!("  Remove {} marked agent(s)?", app.marked.len()))),
        Line::from(Span::styled("  (worktrees stay on disk)", Style::default().fg(Color::DarkGray))),
        Line::from(""),
        Line::from(Span::styled("  [y] remove   [n/esc] cancel", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines)
        .block(dialog(Span::styled(" confirm remove ", Style::default().fg(Color::Red)))), area);
}

fn draw_rename(f: &mut Frame, app: &App) {
    let area = centered(56, 20, f.area());
    f.render_widget(Clear, area);
    let lines = vec![
        Line::from(""),
        Line::from(Span::styled("  Label this agent (blank clears):", Style::default().fg(Color::Gray))),
        Line::from(vec![Span::raw("  "),
            Span::styled(format!("{}_", app.rename_buf),
                Style::default().fg(Color::Green).add_modifier(Modifier::BOLD))]),
        Line::from(""),
        Line::from(Span::styled("  [enter] save   [esc] cancel", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines).block(panel_focused(" rename ", ACCENT)), area);
}

fn draw_settings(f: &mut Frame, app: &App) {
    let Some(p) = &app.settings else { return };
    let area = centered(62, 44, f.area());
    f.render_widget(Clear, area);
    use crate::app::{model_presets, HARNESSES, WORKFLOWS};
    let harness = HARNESSES[p.harness_idx.min(2)];
    let presets = model_presets(p.harness_idx);
    let model = presets[p.model_idx.min(presets.len() - 1)];
    let workflow = WORKFLOWS[p.workflow_idx.min(WORKFLOWS.len() - 1)];
    let wt = if p.worktree_new { "new git worktree (isolated)" } else { "current repo (shared)" };

    let row = |i: usize, label: &str, val: &str| {
        let active = app.settings_field == i;
        let arrow = if active { ACCENT } else { ACCENT_DIM };
        Line::from(vec![
            Span::styled(format!("{}{:<10}", if active { "▶ " } else { "  " }, label),
                if active { Style::default().fg(Color::White).add_modifier(Modifier::BOLD) }
                else { Style::default().fg(Color::Gray) }),
            Span::styled("‹ ", Style::default().fg(arrow)),
            Span::styled(val.to_string(), Style::default().fg(Color::Green).add_modifier(Modifier::BOLD)),
            Span::styled(" ›", Style::default().fg(arrow)),
        ])
    };
    let lines = vec![
        Line::from(Span::styled("  Default launch settings", Style::default().add_modifier(Modifier::BOLD))),
        Line::from(Span::styled("  used to pre-fill the new-agent form", Style::default().fg(Color::DarkGray))),
        Line::from(""),
        row(0, "harness", harness),
        row(1, "model", model),
        row(2, "workflow", workflow),
        row(3, "worktree", wt),
        row(4, "budget", &if p.budget_usd > 0.0 { format!("${:.2}", p.budget_usd) } else { "off".into() }),
        Line::from(""),
        Line::from(Span::styled("  [tab] field   [←→] change   [enter] save   [esc] cancel",
            Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines).block(panel_focused(" settings ", ACCENT)), area);
}

fn draw_confirm_push(f: &mut Frame, app: &App) {
    let area = centered(64, 34, f.area());
    f.render_widget(Clear, area);
    let has_gh = crate::app::gh_available();
    let n = app.push_queue.len();
    let target = if n == 1 {
        Line::from(vec![Span::styled("  push branch  ", Style::default().fg(Color::DarkGray)),
            Span::styled(app.push_queue.first().map(|q| q.1.clone()).unwrap_or_default(),
                Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD))])
    } else {
        Line::from(Span::styled(format!("  push {} marked agent(s)", n),
            Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)))
    };
    let lines = vec![
        Line::from(""),
        target,
        Line::from(""),
        Line::from(Span::styled(
            if has_gh { "  → commits all changes, pushes to origin, opens a PR (gh)" }
            else { "  → commits all changes, pushes to origin (install gh for a PR)" },
            Style::default().fg(Color::Gray))),
        Line::from(""),
        Line::from(Span::styled("  [y] push   [n/esc] cancel", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false })
        .block(panel_focused(" push + PR ", Color::Green)), area);
}

fn draw_retry(f: &mut Frame, app: &App) {
    let area = centered(60, 34, f.area());
    f.render_widget(Clear, area);
    let harness = crate::app::HARNESSES[app.retry_harness_idx.min(2)];
    let lines = vec![
        Line::from(""),
        Line::from(vec![Span::raw("  re-delegate: "),
            Span::styled(truncate(&app.retry_task, area.width.saturating_sub(18) as usize),
                Style::default().fg(Color::White).add_modifier(Modifier::BOLD))]),
        Line::from(""),
        Line::from(vec![
            Span::styled("  harness  ", Style::default().fg(Color::DarkGray)),
            Span::styled("‹ ", Style::default().fg(ACCENT)),
            Span::styled(harness, Style::default().fg(harness_color(harness)).add_modifier(Modifier::BOLD)),
            Span::styled(" ›", Style::default().fg(ACCENT)),
            Span::styled("   (←/→ change)", Style::default().fg(Color::DarkGray)),
        ]),
        Line::from(""),
        Line::from(Span::styled("  runs headless in a fresh worktree — appears as a new agent",
            Style::default().fg(Color::DarkGray))),
        Line::from(Span::styled("  [enter] dispatch   [esc] cancel", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false })
        .block(panel_focused(" retry / re-delegate ", ACCENT)), area);
}

fn draw_header(f: &mut Frame, app: &App, area: Rect) {
    let repo = app
        .store
        .repo_root
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "?".into());
    let mode = match app.mode {
        Mode::Terminal => " CHAT ",
        _ => "",
    };
    // per-status tallies + total metered cost across every visible row
    let count = |s: &str| app.rows.iter().filter(|r| r.status == s).count();
    let (running, done, failed) = (count("running"), count("done"), count("failed"));
    let total_cost: f64 = app.rows.iter().map(|r| r.cost_usd).sum();

    let mut spans = vec![
        Span::styled(" crew ", Style::default().bg(Color::Blue).fg(Color::White).add_modifier(Modifier::BOLD)),
        Span::raw(format!(" {} ", repo)),
    ];
    if !app.repo_branch.is_empty() {
        spans.push(Span::styled(format!(" {} ", app.repo_branch), Style::default().fg(Color::Yellow)));
    }
    // standalone vs. nexum-engine-connected
    if app.store.has_engine() {
        spans.push(Span::styled("⚙ engine ", Style::default().fg(Color::Green)));
    }
    spans.push(Span::styled(format!("· {} agents  ", app.rows.len()), Style::default().fg(Color::Gray)));
    // colored status legend, only showing non-zero buckets
    let legend = [(running, "●", Color::Green), (done, "✓", Color::Green), (failed, "✗", Color::Red)];
    for (n, glyph, color) in legend {
        if n > 0 {
            spans.push(Span::styled(format!("{}{} ", glyph, n), Style::default().fg(color)));
        }
    }
    if app.budget > 0.0 {
        // spend vs. ceiling — greens → reds as it fills, red chip once over
        let pct = total_cost / app.budget * 100.0;
        let color = if total_cost > app.budget { Color::Red }
                    else if pct >= 80.0 { Color::Yellow } else { Color::Green };
        spans.push(Span::styled(format!(" ${:.2}/${:.2} ", total_cost, app.budget),
            Style::default().fg(color).add_modifier(Modifier::BOLD)));
        if total_cost > app.budget {
            spans.push(Span::styled(" ⚠ over budget ",
                Style::default().bg(Color::Red).fg(Color::White).add_modifier(Modifier::BOLD)));
        }
    } else if total_cost > 0.0 {
        spans.push(Span::styled(format!(" ${:.3} ", total_cost),
            Style::default().fg(Color::Magenta).add_modifier(Modifier::BOLD)));
    }
    let dirty_n = app.dirty.values().filter(|d| **d).count();
    if dirty_n > 0 {
        spans.push(Span::styled(format!(" ±{} ", dirty_n),
            Style::default().fg(Color::Yellow)));
    }
    let pinned_n = app.pins.len();
    if pinned_n > 0 {
        spans.push(Span::styled(format!(" ◆{} ", pinned_n), Style::default().fg(Color::Yellow)));
    }
    if app.paused {
        spans.push(Span::styled(" ⏸ paused ",
            Style::default().bg(Color::Yellow).fg(Color::Black).add_modifier(Modifier::BOLD)));
    }
    if !app.marked.is_empty() {
        spans.push(Span::styled(format!(" ◉ {} marked ", app.marked.len()),
            Style::default().bg(ACCENT).fg(Color::Black).add_modifier(Modifier::BOLD)));
    }
    // active category / non-default sort chips (k9s-style)
    if app.category != crate::app::Category::All {
        spans.push(Span::styled(format!(" [{}] ", app.category.label()),
            Style::default().bg(ACCENT).fg(Color::Black).add_modifier(Modifier::BOLD)));
    }
    if app.sort != crate::app::SortKey::Status {
        spans.push(Span::styled(format!(" ↓{} ", app.sort.label()), Style::default().fg(ACCENT)));
    }
    if app.mode == Mode::Filter || !app.filter_text.is_empty() {
        let caret = if app.mode == Mode::Filter { "_" } else { "" };
        spans.push(Span::styled(format!(" /{}{} ", app.filter_text, caret), Style::default().fg(ACCENT)));
    }
    if !mode.is_empty() {
        spans.push(Span::styled(mode, Style::default().bg(Color::Green).fg(Color::Black).add_modifier(Modifier::BOLD)));
    }
    spans.push(Span::styled(format!(" {}", app.status_msg), Style::default().fg(Color::Yellow)));
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

fn draw_list(f: &mut Frame, app: &App, area: Rect) {
    let title = format!(" agents ({}) ", app.rows.len());
    if app.rows.is_empty() {
        // distinguish "nothing at all" from "your filter hid everything"
        let msg = if app.all_rows.is_empty() {
            "\n  no agents yet.\n\n  press [n] to launch one.".to_string()
        } else {
            format!("\n  nothing matches the [{}] filter.\n\n  press [1] for all, or [/] to search.",
                app.category.label())
        };
        let hint = Paragraph::new(msg)
            .style(Style::default().fg(Color::DarkGray))
            .block(panel(&title));
        f.render_widget(hint, area);
        return;
    }
    let inner_w = area.width.saturating_sub(4) as usize;
    let now = now_secs();
    let items: Vec<ListItem> = app
        .rows
        .iter()
        .map(|r| {
            let is_marked = app.marked.contains(&r.id);
            let mark = if is_marked {
                Span::styled("◉ ", Style::default().fg(ACCENT).add_modifier(Modifier::BOLD))
            } else if app.pins.contains(&r.id) {
                Span::styled("◆ ", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD))
            } else {
                Span::raw("  ")
            };
            // running rows get an animated spinner; everything else a static glyph
            let glyph = if r.status == "running" { spinner() } else { status_icon(&r.status) };
            let icon = Span::styled(format!("{} ", glyph),
                Style::default().fg(status_color(&r.status)));
            let who = Span::styled(format!("{:<8} ", truncate(&r.harness, 8)),
                Style::default().fg(harness_color(&r.harness)));
            // uncommitted-changes flag (2-col slot keeps rows aligned)
            let dirt = if app.dirty.get(&r.id).copied().unwrap_or(false) {
                Span::styled("± ", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD))
            } else {
                Span::raw("  ")
            };
            let meta = rel_time(r.updated_ts, now);
            let cost = if r.cost_usd > 0.0 { format!("${:.2} ", r.cost_usd) } else { String::new() };
            // tail: [cost]+time. Left cols: mark(2)+icon(2)+who(9)+dirt(2).
            let tail_len = cost.len() + meta.len();
            let task_w = inner_w.saturating_sub(2 + 2 + 9 + 2 + tail_len + 1).max(6);
            let shown = r.display();
            let task = truncate(if shown.is_empty() { "(no task)" } else { shown }, task_w);
            let pad = inner_w.saturating_sub(2 + 2 + 9 + 2 + task.chars().count() + tail_len);
            ListItem::new(Line::from(vec![
                mark, icon, who, dirt, Span::raw(task), Span::raw(" ".repeat(pad)),
                Span::styled(cost, Style::default().fg(Color::Magenta)),
                Span::styled(meta, Style::default().fg(Color::DarkGray)),
            ]))
        })
        .collect();

    let focused = app.mode == Mode::Normal || app.mode == Mode::Filter;
    let block = if focused { panel_focused(&title, ACCENT) } else { panel(&title) };
    let list = List::new(items)
        .block(block)
        .highlight_style(Style::default().bg(ACCENT).fg(Color::Black).add_modifier(Modifier::BOLD))
        .highlight_symbol("▶ ");
    let mut state = ListState::default();
    state.select(Some(app.selected.min(app.rows.len() - 1)));
    f.render_stateful_widget(list, area, &mut state);
}

fn draw_right(f: &mut Frame, app: &App, area: Rect) {
    match app.selected_row() {
        Some(r) if r.interactive => {
            if let Some(idx) = r.proc_idx {
                draw_terminal(f, app, idx, area);
                return;
            }
        }
        _ => {}
    }
    draw_preview(f, app, area);
}

/// The embedded terminal — the agent's live REPL, rendered inside the TUI.
fn draw_terminal(f: &mut Frame, app: &App, idx: usize, area: Rect) {
    let focused = app.mode == Mode::Terminal;
    let title = if focused {
        " chat · typing goes to the agent · [Ctrl-o] leave ".to_string()
    } else {
        " terminal · [enter] to chat ".to_string()
    };
    let block = if focused {
        panel_focused(&title, Color::Green)
    } else {
        panel(&title)
    };
    let inner = block.inner(area);
    f.render_widget(block, area);

    if let Some(proc) = app.agents.get(idx) {
        // keep the PTY sized to the visible pane
        proc.resize(inner.height.max(1), inner.width.max(1));
        if let Ok(parser) = proc.parser.lock() {
            let term = PseudoTerminal::new(parser.screen());
            f.render_widget(term, inner);
        }
    }
}

fn draw_preview(f: &mut Frame, app: &App, area: Rect) {
    let block = panel(" preview ");
    let Some(r) = app.selected_row() else {
        let p = Paragraph::new("\n  select an agent, or press [n] to launch one.")
            .style(Style::default().fg(Color::DarkGray)).block(block);
        f.render_widget(p, area);
        return;
    };
    let inner_w = block.inner(area).width as usize;
    let field = |k: &str| Span::styled(format!("{:<9}", k), Style::default().fg(Color::DarkGray));
    let kind = match r.kind { Kind::Managed => "agent", Kind::Observed => "observed session" };
    let glyph = if r.status == "running" { spinner() } else { status_icon(&r.status) };
    let mut lines = vec![
        Line::from(Span::styled(
            truncate(if r.display().is_empty() { "(no task)" } else { r.display() }, inner_w.saturating_sub(1)),
            Style::default().fg(Color::White).add_modifier(Modifier::BOLD))),
        Line::from(""),
        Line::from(vec![field("status"), Span::styled(
            format!("{} {}", glyph, r.status), Style::default().fg(status_color(&r.status)))]),
        Line::from(vec![field("kind"), Span::raw(kind.to_string())]),
        Line::from(vec![field("harness"),
            Span::styled(r.harness.clone(), Style::default().fg(harness_color(&r.harness))),
            Span::styled(format!(" · {}", r.model), Style::default().fg(Color::Gray))]),
    ];
    if let Some((k, n)) = r.steps {
        lines.push(Line::from(vec![field("steps"),
            Span::styled(format!("{}/{}", k, n), Style::default().fg(ACCENT))]));
    } else if let Some(i) = r.step_index {
        lines.push(Line::from(vec![field("step"), Span::raw(format!("#{}", i))]));
    }
    if !r.branch.is_empty() && r.branch != "-" {
        lines.push(Line::from(vec![field("branch"),
            Span::styled(r.branch.clone(), Style::default().fg(Color::Yellow))]));
    }
    if let Some(rem) = &app.sel_remote {
        if !rem.is_empty() && r.worktree.is_some() {
            let color = match rem.as_str() {
                "up to date" => Color::Green,
                "not pushed" => Color::DarkGray,
                _ => Color::Yellow,
            };
            lines.push(Line::from(vec![field("remote"), Span::styled(rem.clone(), Style::default().fg(color))]));
        }
    }
    if let Some((n, _)) = app.selected_pr() {
        lines.push(Line::from(vec![field("PR"),
            Span::styled(format!("#{} open  [O] open", n),
                Style::default().fg(Color::Magenta).add_modifier(Modifier::BOLD))]));
    }
    lines.push(Line::from(vec![field("cost"),
        Span::styled(format!("${:.3}", r.cost_usd), Style::default().fg(Color::Magenta))]));
    if let Some(p) = r.ctx_pct {
        // btop-style meter; reddens as context fills up
        let color = if p >= 85.0 { Color::Red } else if p >= 60.0 { Color::Yellow } else { Color::Green };
        lines.push(Line::from(vec![field("context"),
            Span::styled(meter(p, 12), Style::default().fg(color)),
            Span::styled(format!(" {:.0}%", p), Style::default().fg(color))]));
    }
    // live PTY agents show a precise uptime (updated_ts holds the launch time)
    if r.interactive && r.status == "running" && r.updated_ts > 0.0 {
        lines.push(Line::from(vec![field("uptime"),
            Span::styled(fmt_uptime(now_secs() - r.updated_ts), Style::default().fg(Color::Green))]));
    } else if r.updated_ts > 0.0 {
        let label = if r.status == "running" { "running" } else { "updated" };
        lines.push(Line::from(vec![field(label),
            Span::styled(rel_time(r.updated_ts, now_secs()), Style::default().fg(Color::DarkGray))]));
    }
    // git status of the selected worktree (cached)
    if let Some((path, sum, clean)) = &app.worktree_status {
        if Some(path) == r.worktree.as_ref() {
            let color = if *clean { Color::Green } else { Color::Yellow };
            lines.push(Line::from(vec![field("changes"),
                Span::styled(sum.clone(), Style::default().fg(color))]));
        }
    }
    if let Some(wt) = &r.worktree {
        lines.push(Line::from(vec![field("worktree"),
            Span::styled(truncate(wt, inner_w.saturating_sub(10)), Style::default().fg(Color::DarkGray))]));
    }
    if r.kind == Kind::Managed && !r.interactive {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "[d]iff [l]og [e]dit [!]shell [P]ush [R]etry [o]wt [x]rm",
            Style::default().fg(ACCENT_DIM))));
    }
    f.render_widget(Paragraph::new(lines).block(block).wrap(Wrap { trim: false }), area);
}

fn draw_footer(f: &mut Frame, app: &App, area: Rect) {
    let keys = match app.mode {
        Mode::Terminal => "CHAT — type to talk   ·   [shift-tab] cycle agent modes   ·   [Ctrl-o] back",
        Mode::Diff => "DIFF — [j/k]scroll [space/pgdn]page   ·   [q/esc] close",
        Mode::Log => "LOG — [j/k]scroll [space/pgdn]page [r]eload   ·   [q/esc] close",
        _ => "[q]uit [jk]move [n]ew [enter]chat [b]roadcast [d]iff [l]og [P]ush [p]in [:]palette [1-4]filter [?]help",
    };
    f.render_widget(
        Paragraph::new(Span::styled(keys, Style::default().fg(Color::DarkGray))),
        area,
    );
}

fn draw_form(f: &mut Frame, app: &App) {
    let area = centered(76, 66, f.area());
    f.render_widget(Clear, area);
    let form = &app.new_form;
    use crate::app::{FIELD_HARNESS, FIELD_IMAGES, FIELD_MODEL, FIELD_TASK, FIELD_WORKFLOW, FIELD_WORKTREE};

    let block = panel_focused(" new agent ", ACCENT);
    let inner = block.inner(area);
    f.render_widget(block, area);

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1), // title
            Constraint::Length(1), // label: task
            Constraint::Length(5), // task editor
            Constraint::Length(1), // workflow
            Constraint::Length(1), // harness
            Constraint::Length(1), // model
            Constraint::Length(1), // worktree
            Constraint::Length(1), // images
            Constraint::Min(1),    // hints / error
        ])
        .split(inner);

    let lbl = |i: usize, t: &str| {
        let active = form.field == i;
        Span::styled(
            format!("{}{}", if active { "▶ " } else { "  " }, t),
            if active { Style::default().fg(Color::White).add_modifier(Modifier::BOLD) }
            else { Style::default().fg(Color::Gray) },
        )
    };
    let picker = |active: bool, value: &str, idx: usize, total: usize| {
        let arrow = if active { Color::Cyan } else { Color::DarkGray };
        Line::from(vec![
            Span::styled("   ‹ ", Style::default().fg(arrow)),
            Span::styled(value.to_string(), Style::default().fg(Color::Green).add_modifier(Modifier::BOLD)),
            Span::styled(" ›", Style::default().fg(arrow)),
            Span::styled(format!("  {}/{}", idx + 1, total), Style::default().fg(Color::DarkGray)),
        ])
    };

    f.render_widget(Paragraph::new(Span::styled("  Launch an interactive agent",
        Style::default().add_modifier(Modifier::BOLD))), rows[0]);
    f.render_widget(Paragraph::new(Line::from(lbl(FIELD_TASK, "task *  (Enter = newline)"))), rows[1]);

    // the multiline rich task editor
    let mut task = form.task.clone();
    let te_border = if form.field == FIELD_TASK { Color::Cyan } else { Color::DarkGray };
    task.set_block(Block::default().borders(Borders::ALL).border_style(Style::default().fg(te_border)));
    f.render_widget(&task, rows[2]);

    let mut fline = vec![lbl(FIELD_WORKFLOW, "workflow")];
    fline.extend(picker(form.field == FIELD_WORKFLOW, form.workflow(),
        form.workflow_idx, crate::app::WORKFLOWS.len()).spans);
    f.render_widget(Paragraph::new(Line::from(fline)), rows[3]);

    let presets = crate::app::model_presets(form.harness_idx);
    let mut hline = vec![lbl(FIELD_HARNESS, "harness")];
    hline.extend(picker(form.field == FIELD_HARNESS, form.harness(), form.harness_idx, 3).spans);
    // is this harness's CLI installed?
    if crate::app::harness_installed(form.harness()) {
        hline.push(Span::styled("  ✓ installed", Style::default().fg(Color::Green)));
    } else {
        hline.push(Span::styled(format!("  ✗ '{}' not on PATH", crate::app::harness_bin(form.harness())),
            Style::default().fg(Color::Red)));
    }
    f.render_widget(Paragraph::new(Line::from(hline)), rows[4]);

    let mut mline = vec![lbl(FIELD_MODEL, "model")];
    mline.extend(picker(form.field == FIELD_MODEL, form.model(), form.model_idx, presets.len()).spans);
    f.render_widget(Paragraph::new(Line::from(mline)), rows[5]);

    let wt = if form.worktree_new { "new git worktree (isolated)" } else { "current repo (shared)" };
    let mut wline = vec![lbl(FIELD_WORKTREE, "worktree")];
    wline.extend(picker(form.field == FIELD_WORKTREE, wt, if form.worktree_new {0} else {1}, 2).spans);
    f.render_widget(Paragraph::new(Line::from(wline)), rows[6]);

    let img = if let Some(buf) = &form.image_input {
        format!("   typing path: {}_", buf)
    } else if form.images.is_empty() {
        "   (none — [a] add, [d] remove)".to_string()
    } else {
        format!("   {}  ([a] add, [d] remove)", form.images.join(", "))
    };
    let mut iline = vec![lbl(FIELD_IMAGES, "images")];
    iline.push(Span::styled(img, Style::default().fg(Color::Gray)));
    f.render_widget(Paragraph::new(Line::from(iline)), rows[7]);

    let mut hints = Vec::new();
    if let Some(err) = &form.error {
        hints.push(Line::from(Span::styled(format!("  ⚠ {}", err), Style::default().fg(Color::Red))));
    }
    hints.push(Line::from(Span::styled(
        "  [tab] next field   [←→] change   [ Ctrl-S ] launch & chat   [esc] cancel",
        Style::default().fg(Color::DarkGray))));
    f.render_widget(Paragraph::new(hints).wrap(Wrap { trim: false }), rows[8]);
}

fn draw_help(f: &mut Frame, app: &App) {
    let area = centered(64, 92, f.area());
    f.render_widget(Clear, area);
    // grouped cheatsheet (lazygit-style): (section, [(keys, desc)])
    let groups: [(&str, &[(&str, &str)]); 6] = [
        ("Navigate", &[
            ("j / k · ↑ / ↓", "move selection"),
            ("g / G", "jump to top / bottom"),
            ("PgUp/Dn · ^u/^d", "page / half-page the list"),
            ("] / [", "jump to next / prev failed agent"),
            ("space / A", "mark row / mark all · esc clears marks"),
            ("click / wheel", "select a row · scroll"),
        ]),
        ("Launch & drive", &[
            ("n", "new agent (workflow: chat / plan→build / plan-only)"),
            ("enter", "chat with a live agent, or resume a ▷ one"),
            ("b", "broadcast one message to all marked live agents"),
            ("shift-tab", "(in chat) cycle the agent's modes, e.g. plan"),
            ("D", "duplicate into a prefilled new-agent form"),
            (":", "command palette — fuzzy-run any action"),
        ]),
        ("Inspect", &[
            ("d", "review the worktree diff (colored)"),
            ("l", "tail the log — live-follows at the bottom"),
            ("o / y", "worktree path · yank it to the clipboard"),
            ("Y", "yank the branch name"),
            ("e / !", "open worktree in $EDITOR · drop to a $SHELL"),
        ]),
        ("Ship", &[
            ("P", "commit + push (selected or all marked), open PR (gh)"),
            ("O", "open the branch on the remote in a browser (gh)"),
            ("f", "git fetch origin (refreshes ahead/behind)"),
            ("R", "retry: re-delegate the task (needs engine)"),
        ]),
        ("Manage", &[
            ("s / S", "stop selected/marked · stop ALL running"),
            ("x / c", "remove selected/marked · clear finished"),
            ("p / F", "pin selected (floats to top) · mark all failed"),
            ("z / E", "pause auto-refresh · export fleet report (md)"),
            ("T / i / L", "yank task · yank id · label/rename"),
        ]),
        ("Filter & sort", &[
            ("1 2 3 4", "all / running / agents / sessions"),
            ("] [ · } {", "jump failed · jump running"),
            ("t · 0", "cycle sort · reset view (clear filter/sort/marks)"),
            ("/", "search — try status:failed or harness:claude"),
            (", · = · h", "settings (+budget) · about · toggle observed"),
        ]),
    ];
    let mut lines: Vec<Line> = Vec::new();
    for (title, rows) in groups {
        lines.push(Line::from(Span::styled(format!(" {}", title),
            Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD))));
        for (k, d) in rows {
            lines.push(Line::from(vec![
                Span::styled(format!("  {:<15}", k), Style::default().fg(ACCENT).add_modifier(Modifier::BOLD)),
                Span::styled(d.to_string(), Style::default().fg(Color::Gray)),
            ]));
        }
        lines.push(Line::from(""));
    }
    lines.push(Line::from(vec![
        Span::styled(" legend  ", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
        Span::styled("● running  ", Style::default().fg(Color::Green)),
        Span::styled("✓ done  ", Style::default().fg(Color::Green)),
        Span::styled("✗ failed  ", Style::default().fg(Color::Red)),
        Span::styled("▷ resumable  ", Style::default().fg(Color::Yellow)),
        Span::styled("± uncommitted  ", Style::default().fg(Color::Yellow)),
        Span::styled("◉ marked", Style::default().fg(ACCENT)),
    ]));
    f.render_widget(Paragraph::new(lines)
        .scroll((app.help_scroll, 0))
        .block(panel_focused(" help — [j/k] scroll · any other key closes ", ACCENT)), area);
}

/// Full-screen colored diff of the selected agent's worktree.
fn draw_diff(f: &mut Frame, app: &App) {
    let area = centered(90, 88, f.area());
    f.render_widget(Clear, area);
    let q = app.find_query.to_lowercase();
    let lines: Vec<Line> = app.diff_text.lines().map(|l| {
        if !q.is_empty() && l.to_lowercase().contains(&q) {
            return Line::from(Span::styled(l.to_string(),
                Style::default().bg(Color::Yellow).fg(Color::Black).add_modifier(Modifier::BOLD)));
        }
        let style = if l.starts_with('+') && !l.starts_with("+++") {
            Style::default().fg(Color::Green)
        } else if l.starts_with('-') && !l.starts_with("---") {
            Style::default().fg(Color::Red)
        } else if l.starts_with("@@") {
            Style::default().fg(Color::Cyan)
        } else if l.starts_with("diff ") || l.starts_with("── ") {
            Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(Color::Gray)
        };
        Line::from(Span::styled(l.to_string(), style))
    }).collect();
    let total = lines.len();
    let title = format!(" diff · {} lines ·{} [/]search [q]close ", total, find_hint(app));
    f.render_widget(
        Paragraph::new(lines)
            .scroll((app.diff_scroll, 0))
            .block(panel_focused(&title, ACCENT)),
        area,
    );
    scrollbar(f, area, app.diff_scroll, total);
}

/// Title fragment showing the in-viewer search state ("" when idle).
fn find_hint(app: &App) -> String {
    if app.find_input {
        format!(" /{}_ ·", app.find_query)
    } else if !app.find_query.is_empty() {
        format!(" /{} [n/N] ·", app.find_query)
    } else {
        String::new()
    }
}

/// Scrollable tail of a headless agent's log file.
fn draw_log(f: &mut Frame, app: &App) {
    let area = centered(90, 88, f.area());
    f.render_widget(Clear, area);
    let total = app.log_text.lines().count();
    let q = app.find_query.to_lowercase();
    let lines: Vec<Line> = app.log_text.lines().map(|l| {
        let low = l.to_ascii_lowercase();
        if !q.is_empty() && low.contains(&q) {
            return Line::from(Span::styled(l.to_string(),
                Style::default().bg(Color::Yellow).fg(Color::Black).add_modifier(Modifier::BOLD)));
        }
        // highlight anything that reads like an error
        let style = if low.contains("error") || low.contains("\"is_error\":true") || low.contains("traceback") {
            Style::default().fg(Color::Red)
        } else {
            Style::default().fg(Color::Gray)
        };
        Line::from(Span::styled(l.to_string(), style))
    }).collect();
    let at_bottom = app.log_scroll as usize >= total.saturating_sub(1);
    let follow = if at_bottom { "⏵following" } else { "⏸scrollback" };
    let title = format!(" log · {} lines · {} ·{} [/]search [q]close ", total, follow, find_hint(app));
    f.render_widget(
        Paragraph::new(lines)
            .scroll((app.log_scroll, 0))
            .block(panel_focused(&title, ACCENT)),
        area,
    );
    scrollbar(f, area, app.log_scroll, total);
}

fn draw_confirm(f: &mut Frame, app: &App) {
    let area = centered(50, 20, f.area());
    f.render_widget(Clear, area);
    let what = if !app.marked.is_empty() {
        format!("{} marked agent(s)", app.marked.len())
    } else {
        format!("agent {}", app.selected_row().map(|r| r.id.clone()).unwrap_or_default())
    };
    let lines = vec![
        Line::from(""),
        Line::from(Span::raw(format!("  Stop {} ?", what))),
        Line::from(""),
        Line::from(Span::styled("  [y] confirm   [n/esc] cancel", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines)
        .block(dialog(Span::styled(" confirm stop ", Style::default().fg(Color::Red)))), area);
}

fn draw_confirm_stop_all(f: &mut Frame, app: &App) {
    let area = centered(52, 20, f.area());
    f.render_widget(Clear, area);
    let n = app.running_count();
    let lines = vec![
        Line::from(""),
        Line::from(Span::raw(format!("  Stop ALL {} running agent(s)?", n))),
        Line::from(""),
        Line::from(Span::styled("  [y] confirm   [n/esc] cancel", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines)
        .block(dialog(Span::styled(" confirm stop all ", Style::default().fg(Color::Red)))), area);
}

fn draw_confirm_quit(f: &mut Frame, app: &App) {
    let area = centered(58, 26, f.area());
    f.render_widget(Clear, area);
    let n = app.agents.len();
    let dirty = app.dirty.values().filter(|d| **d).count();
    let mut lines = vec![
        Line::from(""),
        Line::from(Span::styled(format!("  Quit? {} agent(s) will stop.", n),
            Style::default().add_modifier(Modifier::BOLD))),
    ];
    if dirty > 0 {
        lines.push(Line::from(Span::styled(
            format!("  ⚠ {} worktree(s) have uncommitted changes (± in the list).", dirty),
            Style::default().fg(Color::Yellow))));
    }
    lines.extend([
        Line::from(""),
        Line::from(Span::styled(
            "  Their worktrees are kept — reopen the TUI and press",
            Style::default().fg(Color::Gray))),
        Line::from(Span::styled(
            "  [enter] on a ▷ resumable agent to continue where it left off.",
            Style::default().fg(Color::Gray))),
        Line::from(""),
        Line::from(Span::styled("  [y] quit   [n/esc] cancel", Style::default().fg(Color::DarkGray))),
    ]);
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false })
        .block(dialog(Span::styled(" confirm quit ", Style::default().fg(Color::Yellow)))), area);
}

fn draw_confirm_discard(f: &mut Frame) {
    let area = centered(50, 20, f.area());
    f.render_widget(Clear, area);
    let lines = vec![
        Line::from(""),
        Line::from(Span::raw("  Discard this new agent?")),
        Line::from(""),
        Line::from(Span::styled("  [y] discard   [n/esc] keep editing", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines)
        .block(dialog(Span::styled(" confirm discard ", Style::default().fg(Color::Yellow)))), area);
}

fn centered(pw: u16, ph: u16, area: Rect) -> Rect {
    let w = area.width * pw / 100;
    let h = area.height * ph / 100;
    Rect {
        x: area.x + (area.width.saturating_sub(w)) / 2,
        y: area.y + (area.height.saturating_sub(h)) / 2,
        width: w.max(24),
        height: h.max(10),
    }
}

fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        let mut out: String = s.chars().take(n.saturating_sub(1)).collect();
        out.push('…');
        out
    }
}

/// Compact uptime like "3m 12s" / "1h 04m" / "45s".
fn fmt_uptime(secs: f64) -> String {
    let s = secs.max(0.0) as u64;
    if s < 60 { format!("{}s", s) }
    else if s < 3600 { format!("{}m {:02}s", s / 60, s % 60) }
    else { format!("{}h {:02}m", s / 3600, (s % 3600) / 60) }
}

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn now_millis() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::App;
    use crate::store::Store;
    use ratatui::{backend::TestBackend, Terminal};
    use std::path::PathBuf;

    fn buffer_text(term: &Terminal<TestBackend>) -> String {
        term.backend().buffer().content().iter().map(|c| c.symbol()).collect()
    }

    #[test]
    fn meter_fills_proportionally() {
        assert_eq!(meter(0.0, 10), "░░░░░░░░░░");
        assert_eq!(meter(100.0, 10), "██████████");
        assert_eq!(meter(50.0, 10), "█████░░░░░");
        assert_eq!(meter(200.0, 4), "████"); // clamps over 100
    }

    #[test]
    fn renders_empty_dashboard_chrome() {
        let store = Store::new(PathBuf::from("/tmp/myrepo"), PathBuf::from("/tmp/myrepo/scripts"));
        let app = App::new(store);
        let mut term = Terminal::new(TestBackend::new(120, 24)).unwrap();
        term.draw(|f| draw(f, &app)).unwrap();
        let text = buffer_text(&term);
        assert!(text.contains("crew"));
        assert!(text.contains("myrepo"));
        assert!(text.contains("agents ("));
        assert!(text.contains("[n]ew"));
    }

    #[test]
    fn renders_populated_list_with_markers() {
        use crate::agent::{ManagedAgent, Row};
        let mk = |id: &str, h: &str, st: &str, cost: f64| Row::from_managed(&ManagedAgent {
            agent_id: id.into(), harness: Some(h.into()), model: Some("sonnet".into()),
            worktree: Some(format!("/tmp/{id}")), branch: Some(format!("crew/{id}")),
            status: Some(st.into()), cost_usd: Some(cost), task: Some(format!("task {id}")),
            step_index: None, updated_ts: Some(1.0), pid: None, log_path: None,
        });
        let store = Store::new(PathBuf::from("/tmp/myrepo"), PathBuf::from("/tmp/myrepo/scripts"));
        let mut app = App::new(store);
        app.rows = vec![mk("a", "cursor", "running", 0.42), mk("b", "claude", "failed", 0.08)];
        app.marked.insert("a".into());
        app.dirty.insert("b".into(), true);
        let mut term = Terminal::new(TestBackend::new(120, 24)).unwrap();
        term.draw(|f| draw(f, &app)).unwrap();
        let text = buffer_text(&term);
        assert!(text.contains("cursor") && text.contains("claude"));
        assert!(text.contains("$0.42"), "cost missing");
        assert!(text.contains('◉'), "mark missing");
        assert!(text.contains('±'), "dirty marker missing");
        assert!(text.contains("✗1") || text.contains('✗'), "failed legend missing");
    }

    #[test]
    fn renders_palette_broadcast_about() {
        let store = Store::new(PathBuf::from("/tmp/myrepo"), PathBuf::from("/tmp/myrepo/scripts"));
        let mut app = App::new(store);
        let render = |app: &App| {
            let mut term = Terminal::new(TestBackend::new(120, 30)).unwrap();
            term.draw(|f| draw(f, app)).unwrap();
            buffer_text(&term)
        };
        app.mode = crate::app::Mode::Palette;
        app.palette_query = "push".into();
        assert!(render(&app).contains("push"), "palette should list matching actions");

        app.mode = crate::app::Mode::Broadcast;
        app.broadcast_buf = "run the tests".into();
        assert!(render(&app).contains("run the tests"), "broadcast echoes the buffer");

        app.mode = crate::app::Mode::About;
        assert!(render(&app).contains("crew"), "about shows the name/version");
    }

    #[test]
    fn budget_over_shows_warning_in_header() {
        use crate::agent::{ManagedAgent, Row};
        let store = Store::new(PathBuf::from("/tmp/myrepo"), PathBuf::from("/tmp/myrepo/scripts"));
        let mut app = App::new(store);
        app.budget = 1.0;
        app.rows = vec![Row::from_managed(&ManagedAgent {
            agent_id: "a".into(), harness: Some("claude".into()), model: None,
            worktree: None, branch: None, status: Some("running".into()),
            cost_usd: Some(2.5), task: Some("t".into()), step_index: None,
            updated_ts: Some(1.0), pid: None, log_path: None,
        })];
        let mut term = Terminal::new(TestBackend::new(120, 24)).unwrap();
        term.draw(|f| draw(f, &app)).unwrap();
        let text = buffer_text(&term);
        assert!(text.contains("over budget"), "over-budget warning missing: {}", text);
    }

    #[test]
    fn renders_new_agent_form() {
        let store = Store::new(PathBuf::from("/tmp/myrepo"), PathBuf::from("/tmp/myrepo/scripts"));
        let mut app = App::new(store);
        app.open_new_agent();
        app.new_form.task.insert_str("add billing endpoint");
        let mut term = Terminal::new(TestBackend::new(120, 30)).unwrap();
        term.draw(|f| draw(f, &app)).unwrap();
        let text = buffer_text(&term);
        assert!(text.contains("new agent"));
        assert!(text.contains("task *"));
        assert!(text.contains("harness"));
        assert!(text.contains("worktree"));
        assert!(text.contains("Ctrl-S"));
    }
}
