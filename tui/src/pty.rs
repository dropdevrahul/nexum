//! In-TUI interactive agents: each agent runs its harness REPL inside a PTY we
//! own, and its output is parsed by a vt100 emulator so it can be rendered in a
//! pane *inside* the dashboard (via tui-term). Keystrokes are written straight
//! back to the PTY. No tmux, no leaving the TUI.
//!
//! Agents live for the lifetime of the TUI process (they're owned here). That's
//! the trade-off for "everything happens inside the TUI".

use anyhow::{Context, Result};
use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};
use std::io::{Read, Write};
use std::sync::{Arc, Mutex};
use std::thread;

/// A single interactive agent: PTY + vt100 screen + child process handle.
pub struct AgentProc {
    pub id: String,
    pub harness: String,
    pub model: String,
    pub task: String,
    pub worktree: String,
    /// Unix seconds when this agent was launched (for "running Nm" in the list).
    pub started: f64,
    /// vt100 screen, fed by a background reader thread; read by the renderer.
    pub parser: Arc<Mutex<vt100::Parser>>,
    /// PTY master writer. Shared (Arc) so the first-message seeder thread and
    /// interactive `send()` write through the same handle — the PTY only hands
    /// out one writer, so a second `take_writer()` would fail.
    writer: Arc<Mutex<Box<dyn Write + Send>>>,
    master: Box<dyn MasterPty + Send>,
    child: Box<dyn Child + Send + Sync>,
}

impl AgentProc {
    /// Spawn `argv` in a PTY at `cwd`, seeded with `task` as the first message.
    pub fn spawn(
        id: String,
        harness: String,
        model: String,
        task: String,
        worktree: String,
        argv: &[String],
        rows: u16,
        cols: u16,
    ) -> Result<AgentProc> {
        let pty = native_pty_system();
        let pair = pty
            .openpty(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 })
            .context("openpty")?;

        let mut cmd = CommandBuilder::new(&argv[0]);
        for a in &argv[1..] {
            cmd.arg(a);
        }
        cmd.cwd(&worktree);
        cmd.env("TERM", "xterm-256color");

        let child = pair.slave.spawn_command(cmd).context("spawn agent command")?;
        // slave no longer needed in this process; dropping it lets EOF propagate.
        drop(pair.slave);

        let mut reader = pair.master.try_clone_reader().context("clone reader")?;
        let writer = Arc::new(Mutex::new(pair.master.take_writer().context("take writer")?));
        let parser = Arc::new(Mutex::new(vt100::Parser::new(rows, cols, 2000)));

        {
            let parser = parser.clone();
            thread::spawn(move || {
                let mut buf = [0u8; 8192];
                loop {
                    match reader.read(&mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => {
                            if let Ok(mut p) = parser.lock() {
                                p.process(&buf[..n]);
                            }
                        }
                    }
                }
            });
        }

        let started = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let proc = AgentProc {
            id,
            harness,
            model,
            task: task.clone(),
            worktree,
            started,
            parser,
            writer,
            master: pair.master,
            child,
        };
        // Seed the first message. Two failure modes to avoid:
        //   1. Timing — a fixed sleep races the harness's startup; typing before
        //      the input box exists silently drops the keystrokes.
        //   2. Multi-line — workflow prompts contain newlines. Typed raw, each
        //      newline submits a fragment early, so the agent gets a partial task.
        // So: wait until the REPL has painted, paste the whole prompt as one
        // bracketed-paste block (newlines land as input, not submits), then send
        // Enter to submit it. Finally verify it reached the agent and retry once
        // if the input box wasn't ready in time.
        if !task.trim().is_empty() {
            let task = task.clone();
            let parser = proc.parser.clone();
            let writer = proc.writer.clone();
            thread::spawn(move || {
                // Non-empty screen ≈ REPL has drawn its input box. Give slow
                // harnesses (claude loads MCP servers first) up to 12s.
                let start = std::time::Instant::now();
                loop {
                    let ready = parser
                        .lock()
                        .map(|p| !p.screen().contents().trim().is_empty())
                        .unwrap_or(false);
                    if ready || start.elapsed() > std::time::Duration::from_secs(12) {
                        break;
                    }
                    thread::sleep(std::time::Duration::from_millis(50));
                }
                thread::sleep(std::time::Duration::from_millis(600));

                let payload = task.replace('\r', "");
                let write = |bytes: &[u8]| {
                    if let Ok(mut w) = writer.lock() {
                        let _ = w.write_all(bytes);
                        let _ = w.flush();
                    }
                };
                // Bracketed paste: the whole prompt (newlines and all) enters the
                // input box as one block instead of submitting each line early.
                let paste = || {
                    write(b"\x1b[200~");
                    write(payload.as_bytes());
                    write(b"\x1b[201~");
                };
                paste();
                // Give the harness time to fully ingest the paste before Enter —
                // too short and Enter races the paste, landing as a newline inside
                // the box so the message is typed but never submitted.
                thread::sleep(std::time::Duration::from_millis(400));
                write(b"\r"); // submit

                // Verify against two failure modes:
                //   dropped  — nothing on screen: input wasn't ready → re-paste.
                //   unsent   — text on screen but still sitting in the input box
                //              (Enter raced the paste) → nudge Enter again. A
                //              redundant Enter on an already-sent prompt is a
                //              harmless no-op (claude ignores empty input).
                let probe: String = payload.chars().take(24).collect();
                let probe = probe.trim().to_string();
                if !probe.is_empty() {
                    thread::sleep(std::time::Duration::from_millis(800));
                    let landed = parser
                        .lock()
                        .map(|p| p.screen().contents().contains(&probe))
                        .unwrap_or(true);
                    if landed {
                        write(b"\r"); // nudge submit in case it's unsent
                    } else {
                        paste();
                        thread::sleep(std::time::Duration::from_millis(400));
                        write(b"\r");
                    }
                }
            });
        }
        Ok(proc)
    }

    /// Forward raw bytes (a keypress) to the agent.
    pub fn send(&mut self, bytes: &[u8]) {
        if let Ok(mut w) = self.writer.lock() {
            let _ = w.write_all(bytes);
            let _ = w.flush();
        }
    }

    /// Resize the PTY + emulator to fit the render area. Takes `&self` (both
    /// `MasterPty::resize` and `Mutex::lock` do) so the renderer can call it.
    pub fn resize(&self, rows: u16, cols: u16) {
        if rows == 0 || cols == 0 {
            return;
        }
        if let Ok(mut p) = self.parser.lock() {
            if p.screen().size() == (rows, cols) {
                return;
            }
            let _ = self.master.resize(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 });
            p.set_size(rows, cols);
        }
    }

    /// True while the child process is still running.
    pub fn is_alive(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    /// Terminate the agent.
    pub fn kill(&mut self) {
        let _ = self.child.kill();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Manual probe against the REAL claude CLI: launch it, let the seeder run,
    /// then dump the screen so we can SEE whether the seeded prompt landed in the
    /// input box and got submitted. Run:
    ///   cargo test real_claude_seed_probe -- --ignored --nocapture
    #[test]
    #[ignore]
    fn real_claude_seed_probe() {
        // Mirror the REAL app path: interactive_argv (--model + strict MCP flags)
        // and run in the actual repo so .mcp.json is discovered exactly as the
        // TUI does. NEXUM_PROBE_CWD overrides the working dir.
        let dir = std::env::var("NEXUM_PROBE_CWD")
            .unwrap_or_else(|_| "/Users/rahultyagi/work/nexum".into());
        let dir = std::path::PathBuf::from(dir);
        let mcp = dir.join(".mcp.json");
        let mcp = if mcp.is_file() { Some(mcp.to_string_lossy().into_owned()) } else { None };
        let argv = crate::app::interactive_argv("claude", "sonnet", mcp.as_deref());
        eprintln!("ARGV: {:?}\nCWD: {}", argv, dir.display());
        // Multi-line workflow-shaped prompt: the reported failing case.
        // Override with NEXUM_PROBE_TASK (empty = no seed, isolates launch).
        let default_task = "Line ALPHA: do not answer yet.\n\nLine BRAVO: now reply with \
                    exactly the two words ALPHA BRAVO and nothing else.";
        let task = std::env::var("NEXUM_PROBE_TASK").unwrap_or_else(|_| default_task.into());
        let mut a = AgentProc::spawn(
            "probe".into(), "claude".into(), "sonnet".into(),
            task.into(),
            dir.to_string_lossy().to_string(), &argv, 40, 120,
        )
        .unwrap();
        // Watch the screen evolve for a while.
        for i in 0..16 {
            std::thread::sleep(std::time::Duration::from_millis(1000));
            let screen = a.parser.lock().unwrap().screen().contents();
            eprintln!("=== t+{}s ===\n{}\n", i + 1, screen);
        }
        a.kill();
    }

    #[test]
    fn pty_captures_command_output() {
        // spawn a trivial command in a PTY and confirm vt100 captures its output.
        let dir = std::env::temp_dir();
        let argv = vec!["printf".to_string(), "HELLO_PTY".to_string()];
        let mut a = AgentProc::spawn(
            "t1".into(), "stub".into(), "m".into(), String::new(),
            dir.to_string_lossy().to_string(), &argv, 24, 80,
        )
        .unwrap();
        // let the reader thread drain the output
        std::thread::sleep(std::time::Duration::from_millis(400));
        let screen = a.parser.lock().unwrap().screen().contents();
        assert!(screen.contains("HELLO_PTY"), "vt100 did not capture output: {:?}", screen);
        a.kill();
    }

    #[test]
    fn pty_seeds_multiline_first_message() {
        // A banner makes the screen non-empty (so the readiness wait fires fast),
        // then `cat` echoes the seeded bytes back. Proves a multi-line prompt —
        // the workflow case — reaches the agent whole, not truncated at the first
        // newline.
        let dir = std::env::temp_dir();
        let argv = vec![
            "sh".to_string(), "-c".to_string(), "printf 'READY\\n'; cat".to_string(),
        ];
        let mut a = AgentProc::spawn(
            "t3".into(), "stub".into(), "m".into(),
            "first line of task\nsecond line of task".into(),
            dir.to_string_lossy().to_string(), &argv, 24, 80,
        )
        .unwrap();
        // readiness wait (banner) + settle + paste; give it room.
        std::thread::sleep(std::time::Duration::from_millis(1200));
        let screen = a.parser.lock().unwrap().screen().contents();
        assert!(screen.contains("first line of task"), "line 1 missing: {:?}", screen);
        assert!(screen.contains("second line of task"), "line 2 missing (truncated at newline?): {:?}", screen);
        a.kill();
    }

    #[test]
    fn pty_forwards_input() {
        // `cat` echoes stdin back to stdout — prove send() reaches the process
        // and the echoed bytes land on the vt100 screen.
        let dir = std::env::temp_dir();
        let argv = vec!["cat".to_string()];
        let mut a = AgentProc::spawn(
            "t2".into(), "stub".into(), "m".into(), String::new(),
            dir.to_string_lossy().to_string(), &argv, 24, 80,
        )
        .unwrap();
        std::thread::sleep(std::time::Duration::from_millis(200));
        a.send(b"PING_INPUT\r");
        std::thread::sleep(std::time::Duration::from_millis(300));
        let screen = a.parser.lock().unwrap().screen().contents();
        assert!(screen.contains("PING_INPUT"), "input not echoed to screen: {:?}", screen);
        a.kill();
    }
}
