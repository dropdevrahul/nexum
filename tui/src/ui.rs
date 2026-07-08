//! Rendering. Master-detail: an agent list on the left; on the right either the
//! selected agent's *embedded terminal* (interactive) or a preview (observed).

use crate::agent::{rel_time, Kind};
use crate::app::{App, Mode};
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, List, ListItem, ListState, Paragraph, Wrap},
    Frame,
};
use tui_term::widget::PseudoTerminal;

fn status_color(status: &str) -> Color {
    match status {
        "running" => Color::Green,
        "resumable" => Color::Yellow,
        "exited" => Color::DarkGray,
        _ => Color::Cyan, // observed
    }
}

fn status_icon(status: &str) -> &'static str {
    match status {
        "running" => "●",
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
        Mode::Help => draw_help(f),
        Mode::ConfirmStop => draw_confirm(f, app),
        Mode::ConfirmQuit => draw_confirm_quit(f, app),
        Mode::ConfirmDiscard => draw_confirm_discard(f),
        _ => {}
    }
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
    let mut spans = vec![
        Span::styled(" nexum ", Style::default().bg(Color::Blue).fg(Color::White).add_modifier(Modifier::BOLD)),
        Span::raw(format!(" {} ", repo)),
        Span::styled(
            format!("· {} agents ({} running) ", app.agents.len(), app.running_count()),
            Style::default().fg(Color::Gray),
        ),
    ];
    if app.mode == Mode::Filter || !app.filter_text.is_empty() {
        let caret = if app.mode == Mode::Filter { "_" } else { "" };
        spans.push(Span::styled(format!("/{}{} ", app.filter_text, caret), Style::default().fg(Color::Cyan)));
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
        let hint = Paragraph::new("\n  no agents yet.\n\n  press [n] to launch one.")
            .style(Style::default().fg(Color::DarkGray))
            .block(Block::default().borders(Borders::ALL).title(title));
        f.render_widget(hint, area);
        return;
    }
    let inner_w = area.width.saturating_sub(4) as usize;
    let now = now_secs();
    let items: Vec<ListItem> = app
        .rows
        .iter()
        .map(|r| {
            let icon = Span::styled(format!("{} ", status_icon(&r.status)),
                Style::default().fg(status_color(&r.status)));
            let who = Span::styled(format!("{:<8} ", truncate(&r.harness, 8)), Style::default().fg(Color::Gray));
            let meta = rel_time(r.updated_ts, now);
            let fixed = 2 + 9 + meta.len() + 1;
            let task_w = inner_w.saturating_sub(fixed).max(6);
            let task = truncate(if r.task.is_empty() { "(no task)" } else { &r.task }, task_w);
            let pad = inner_w.saturating_sub(2 + 9 + task.chars().count() + meta.len());
            ListItem::new(Line::from(vec![
                icon, who, Span::raw(task), Span::raw(" ".repeat(pad)),
                Span::styled(meta, Style::default().fg(Color::DarkGray)),
            ]))
        })
        .collect();

    let list = List::new(items)
        .block(Block::default().borders(Borders::ALL).title(title))
        .highlight_style(Style::default().bg(Color::Blue).fg(Color::White).add_modifier(Modifier::BOLD))
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
    let border = if focused { Color::Green } else { Color::DarkGray };
    let block = Block::default().borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(title);
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
    let block = Block::default().borders(Borders::ALL).title(" preview ");
    let Some(r) = app.selected_row() else {
        let p = Paragraph::new("\n  select an agent, or press [n] to launch one.")
            .style(Style::default().fg(Color::DarkGray)).block(block);
        f.render_widget(p, area);
        return;
    };
    let field = |k: &str| Span::styled(format!("{:<9}", k), Style::default().fg(Color::DarkGray));
    let kind = match r.kind { Kind::Managed => "agent", Kind::Observed => "observed session" };
    let lines = vec![
        Line::from(Span::styled(truncate(if r.task.is_empty() { "(no task)" } else { &r.task }, 60),
            Style::default().add_modifier(Modifier::BOLD))),
        Line::from(vec![field("status"), Span::styled(
            format!("{} {}", status_icon(&r.status), r.status), Style::default().fg(status_color(&r.status)))]),
        Line::from(vec![field("kind"), Span::raw(kind.to_string())]),
        Line::from(vec![field("harness"), Span::raw(format!("{} · {}", r.harness, r.model))]),
        Line::from(vec![field("cost"), Span::raw(format!("${:.3}", r.cost_usd))]),
        Line::from(vec![field("context"), Span::raw(r.ctx_pct.map(|p| format!("{:.0}%", p)).unwrap_or_else(|| "-".into()))]),
    ];
    f.render_widget(Paragraph::new(lines).block(block).wrap(Wrap { trim: false }), area);
}

fn draw_footer(f: &mut Frame, app: &App, area: Rect) {
    let keys = match app.mode {
        Mode::Terminal => "CHAT — type to talk to the agent   ·   [Ctrl-o] back to dashboard",
        _ => "[q]uit [j/k]move [n]ew [enter]chat [s]top [x]rm [o]wt [/]find [h]ide-obs [?]help",
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
    use crate::app::{FIELD_HARNESS, FIELD_IMAGES, FIELD_MODEL, FIELD_TASK, FIELD_WORKTREE};

    let block = Block::default().borders(Borders::ALL).title(" new agent ");
    let inner = block.inner(area);
    f.render_widget(block, area);

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1), // title
            Constraint::Length(1), // label: task
            Constraint::Length(5), // task editor
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

    let presets = crate::app::model_presets(form.harness_idx);
    let mut hline = vec![lbl(FIELD_HARNESS, "harness")];
    hline.extend(picker(form.field == FIELD_HARNESS, form.harness(), form.harness_idx, 3).spans);
    f.render_widget(Paragraph::new(Line::from(hline)), rows[3]);

    let mut mline = vec![lbl(FIELD_MODEL, "model")];
    mline.extend(picker(form.field == FIELD_MODEL, form.model(), form.model_idx, presets.len()).spans);
    f.render_widget(Paragraph::new(Line::from(mline)), rows[4]);

    let wt = if form.worktree_new { "new git worktree (isolated)" } else { "current repo (shared)" };
    let mut wline = vec![lbl(FIELD_WORKTREE, "worktree")];
    wline.extend(picker(form.field == FIELD_WORKTREE, wt, if form.worktree_new {0} else {1}, 2).spans);
    f.render_widget(Paragraph::new(Line::from(wline)), rows[5]);

    let img = if let Some(buf) = &form.image_input {
        format!("   typing path: {}_", buf)
    } else if form.images.is_empty() {
        "   (none — [a] add, [d] remove)".to_string()
    } else {
        format!("   {}  ([a] add, [d] remove)", form.images.join(", "))
    };
    let mut iline = vec![lbl(FIELD_IMAGES, "images")];
    iline.push(Span::styled(img, Style::default().fg(Color::Gray)));
    f.render_widget(Paragraph::new(Line::from(iline)), rows[6]);

    let mut hints = Vec::new();
    if let Some(err) = &form.error {
        hints.push(Line::from(Span::styled(format!("  ⚠ {}", err), Style::default().fg(Color::Red))));
    }
    hints.push(Line::from(Span::styled(
        "  [tab] next field   [←→] change   [ Ctrl-S ] launch & chat   [esc] cancel",
        Style::default().fg(Color::DarkGray))));
    f.render_widget(Paragraph::new(hints).wrap(Wrap { trim: false }), rows[7]);
}

fn draw_help(f: &mut Frame) {
    let area = centered(62, 72, f.area());
    f.render_widget(Clear, area);
    let rows = [
        ("j / k, ↑ / ↓", "move selection"),
        ("n", "new interactive agent (chat) in a worktree"),
        ("enter", "chat with a live agent, or resume a ▷ one"),
        ("Ctrl-o", "leave the terminal, back to the dashboard"),
        ("s", "stop the selected agent"),
        ("x", "remove the selected agent"),
        ("o", "show worktree path"),
        ("/", "search    ·    h  toggle observed sessions"),
        ("q", "quit"),
    ];
    let lines: Vec<Line> = rows.iter().map(|(k, d)| Line::from(vec![
        Span::styled(format!("  {:<14}", k), Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        Span::raw(*d),
    ])).collect();
    f.render_widget(Paragraph::new(lines)
        .block(Block::default().borders(Borders::ALL).title(" help — any key to close ")), area);
}

fn draw_confirm(f: &mut Frame, app: &App) {
    let area = centered(50, 20, f.area());
    f.render_widget(Clear, area);
    let id = app.selected_row().map(|r| r.id.clone()).unwrap_or_default();
    let lines = vec![
        Line::from(""),
        Line::from(Span::raw(format!("  Stop agent {} ?", id))),
        Line::from(""),
        Line::from(Span::styled("  [y] confirm   [n/esc] cancel", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines)
        .block(Block::default().borders(Borders::ALL).title(Span::styled(" confirm stop ", Style::default().fg(Color::Red)))), area);
}

fn draw_confirm_quit(f: &mut Frame, app: &App) {
    let area = centered(58, 26, f.area());
    f.render_widget(Clear, area);
    let n = app.agents.len();
    let lines = vec![
        Line::from(""),
        Line::from(Span::styled(format!("  Quit? {} agent(s) will stop.", n),
            Style::default().add_modifier(Modifier::BOLD))),
        Line::from(""),
        Line::from(Span::styled(
            "  Their worktrees are kept — reopen the TUI and press",
            Style::default().fg(Color::Gray))),
        Line::from(Span::styled(
            "  [enter] on a ▷ resumable agent to continue where it left off.",
            Style::default().fg(Color::Gray))),
        Line::from(""),
        Line::from(Span::styled("  [y] quit   [n/esc] cancel", Style::default().fg(Color::DarkGray))),
    ];
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false })
        .block(Block::default().borders(Borders::ALL)
            .title(Span::styled(" confirm quit ", Style::default().fg(Color::Yellow)))), area);
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
        .block(Block::default().borders(Borders::ALL).title(Span::styled(" confirm discard ", Style::default().fg(Color::Yellow)))), area);
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

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
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
    fn renders_empty_dashboard_chrome() {
        let store = Store::new(PathBuf::from("/tmp/myrepo"), PathBuf::from("/tmp/myrepo/scripts"));
        let app = App::new(store);
        let mut term = Terminal::new(TestBackend::new(120, 24)).unwrap();
        term.draw(|f| draw(f, &app)).unwrap();
        let text = buffer_text(&term);
        assert!(text.contains("nexum"));
        assert!(text.contains("myrepo"));
        assert!(text.contains("agents ("));
        assert!(text.contains("[n]ew"));
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
