import type { Plugin } from "@opencode-ai/plugin"
import path from "path"
import { spawn } from "child_process"

const NEXUM_ROOT = path.resolve(import.meta.dirname, "../..")
const SCRIPTS_DIR = path.join(NEXUM_ROOT, "scripts")

let currentSessionId = "_nosession"

function runScript(scriptName: string, input: object, timeout = 10000): Promise<Record<string, any>> {
  return new Promise((resolve) => {
    const child = spawn("python3", [path.join(SCRIPTS_DIR, scriptName)], {
      env: { ...process.env, CLAUDE_PLUGIN_ROOT: NEXUM_ROOT },
      stdio: ["pipe", "pipe", "pipe"],
    })

    const timer = setTimeout(() => {
      child.kill()
      resolve({})
    }, timeout)

    child.stdin.write(JSON.stringify(input))
    child.stdin.end()

    let stdout = ""
    child.stdout.on("data", (d: Buffer) => { stdout += d.toString() })
    child.on("close", () => {
      clearTimeout(timer)
      try { resolve(JSON.parse(stdout)) }
      catch { resolve({}) }
    })
    child.on("error", () => {
      clearTimeout(timer)
      resolve({})
    })
  })
}

export const NexumHooks: Plugin = async (ctx) => {
  return {
    "shell.env": async (_input, output) => {
      output.env.CLAUDE_PLUGIN_ROOT = NEXUM_ROOT
      output.env.CLAUDE_SESSION_ID = currentSessionId
    },

    "session.created": async (input: any) => {
      currentSessionId = input?.session_id || input?.sessionId || "_nosession"
      await runScript("session_reset.py", { session_id: currentSessionId })
      await runScript("resume_nudge.py", {
        session_id: currentSessionId,
        source: input?.source || "startup",
        cwd: input?.cwd || NEXUM_ROOT,
      })
      await runScript("audit_nudge.py", {
        session_id: currentSessionId,
        source: input?.source || "startup",
        cwd: input?.cwd || NEXUM_ROOT,
      })
    },

    "tool.execute.before": async (input: any, output: any) => {
      const sharedInput = {
        session_id: currentSessionId,
        tool_name: input.tool,
        tool_input: input.args || {},
      }

      const scanResult = await runScript("scan_guard.py", {
        ...sharedInput,
        tool_input: { ...sharedInput.tool_input },
      })

      if (scanResult?.hookSpecificOutput?.permissionDecision) {
        output.permissionDecision = scanResult.hookSpecificOutput.permissionDecision
        output.permissionDecisionReason = scanResult.hookSpecificOutput.permissionDecisionReason
        if (scanResult.hookSpecificOutput.updatedInput) {
          Object.assign(output.args, scanResult.hookSpecificOutput.updatedInput)
        }
        return
      }

      const predupResult = await runScript("predup.py", sharedInput)
      if (predupResult?.hookSpecificOutput?.permissionDecision) {
        output.permissionDecision = predupResult.hookSpecificOutput.permissionDecision
        output.permissionDecisionReason = predupResult.hookSpecificOutput.permissionDecisionReason
      }
    },

    "tool.execute.after": async (input: any, output: any) => {
      const dedupResult = await runScript("dedup.py", {
        session_id: currentSessionId,
        tool_name: input.tool,
        tool_input: input.args || {},
        tool_use_id: "",
        transcript_path: "",
        tool_response: input.result ?? "",
      })

      if (dedupResult?.hookSpecificOutput?.updatedToolOutput) {
        output.result = dedupResult.hookSpecificOutput.updatedToolOutput
      }
    },

    "session.compacted": async (input: any) => {
      await runScript("precompact.py", {
        session_id: currentSessionId,
        cwd: input?.cwd || NEXUM_ROOT,
        transcript_path: input?.transcript_path || "",
        trigger: input?.trigger || "compacted",
      })
    },
  }
}
