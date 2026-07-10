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
    writer: Box<dyn Write + Send>,
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
        let writer = pair.master.take_writer().context("take writer")?;
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
        // Seed the first message. A fixed sleep races the harness's startup —
        // if we type before its input box exists the keystrokes are dropped and
        // the task never reaches the agent. Instead wait until the REPL has
        // drawn something (screen goes non-empty), then a short settle, then type.
        if !task.trim().is_empty() {
            let task = task.clone();
            let parser = proc.parser.clone();
            let mut w = proc.try_clone_writer();
            thread::spawn(move || {
                let start = std::time::Instant::now();
                loop {
                    let ready = parser
                        .lock()
                        .map(|p| !p.screen().contents().trim().is_empty())
                        .unwrap_or(false);
                    if ready || start.elapsed() > std::time::Duration::from_secs(8) {
                        break;
                    }
                    thread::sleep(std::time::Duration::from_millis(50));
                }
                // let the input box finish painting before typing into it
                thread::sleep(std::time::Duration::from_millis(400));
                if let Some(w) = w.as_mut() {
                    let _ = w.write_all(task.as_bytes());
                    let _ = w.write_all(b"\r");
                    let _ = w.flush();
                }
            });
        }
        Ok(proc)
    }

    /// A second writer handle (PTY master writers are independent clones).
    fn try_clone_writer(&self) -> Option<Box<dyn Write + Send>> {
        self.master.take_writer().ok()
    }

    /// Forward raw bytes (a keypress) to the agent.
    pub fn send(&mut self, bytes: &[u8]) {
        let _ = self.writer.write_all(bytes);
        let _ = self.writer.flush();
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
