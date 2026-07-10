import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "child_process";
import path from "path";
import crypto from "crypto";
import fs from "fs";

// nexum repo root, resolved from: <repo>/.pi/extensions/nexum-pi.ts
const NEXUM_ROOT = path.resolve(import.meta.dirname, "../..");
const SCRIPTS_DIR = path.join(NEXUM_ROOT, "scripts");
const DATA_DIR = path.join(NEXUM_ROOT, ".nexum-data");

let currentSessionId = crypto.randomUUID();

/**
 * Run a Python script via JSON-over-stdin/stdout (hook-compatible protocol).
 * Writes `input` as JSON to stdin, reads JSON from stdout, fail-opens to {}.
 */
function runScript(
  name: string,
  input: Record<string, any>,
  timeout = 10000,
): Promise<Record<string, any>> {
  return new Promise((resolve) => {
    const child = spawn("python3", [path.join(SCRIPTS_DIR, name)], {
      env: { ...process.env, CLAUDE_PLUGIN_ROOT: NEXUM_ROOT },
      stdio: ["pipe", "pipe", "pipe"],
    });

    const timer = setTimeout(() => {
      child.kill();
      resolve({});
    }, timeout);

    try {
      child.stdin.write(JSON.stringify(input));
      child.stdin.end();
    } catch {
      child.kill();
      clearTimeout(timer);
      resolve({});
      return;
    }

    let stdout = "";
    child.stdout.on("data", (d: Buffer) => {
      stdout += d.toString();
    });
    child.on("close", () => {
      clearTimeout(timer);
      try {
        resolve(JSON.parse(stdout));
      } catch {
        resolve({});
      }
    });
    child.on("error", () => {
      clearTimeout(timer);
      resolve({});
    });
  });
}

export default function (pi: ExtensionAPI) {
  // ---------------------------------------------------------------------------
  // Session lifecycle
  // ---------------------------------------------------------------------------
  pi.on("session_start", async (event: any, ctx: any) => {
    try {
      const sid = event?.session_id ?? event?.sessionId ?? currentSessionId;
      currentSessionId = sid;
      const cwd = event?.cwd ?? NEXUM_ROOT;
      await runScript("session_reset.py", { session_id: sid });
      await runScript("resume_nudge.py", { session_id: sid, source: event?.reason ?? "startup", cwd });
      await runScript("audit_nudge.py", { session_id: sid, source: event?.reason ?? "startup", cwd });
    } catch {
      // fail-open
    }
  });

  pi.on("session_before_compact", async (event: any, ctx: any) => {
    try {
      const sid = currentSessionId;
      const cwd = event?.cwd ?? NEXUM_ROOT;
      await runScript("precompact.py", {
        session_id: sid,
        cwd,
        transcript_path: event?.transcript_path ?? "",
        trigger: event?.reason ?? "compacted",
      });
    } catch {
      // fail-open
    }
  });

  // ---------------------------------------------------------------------------
  // Tool call/result interceptors
  // ---------------------------------------------------------------------------
  pi.on("tool_call", async (event: any, ctx: any) => {
    try {
      if (event.toolName === "read" || event.toolName === "bash" || event.toolName === "grep" || event.toolName === "glob") {
        const scanResult = await runScript("scan_guard.py", {
          session_id: currentSessionId,
          tool_name: event.toolName,
          tool_input: event.input ?? {},
        });
        if (scanResult?.hookSpecificOutput?.permissionDecision) {
          ctx.ui.notify?.({ title: "nexum", message: `Blocked ${event.toolName} call` });
          return { block: true, reason: scanResult.hookSpecificOutput.permissionDecisionReason ?? "Blocked by scan_guard" };
        }
        if (scanResult?.hookSpecificOutput?.updatedInput) {
          Object.assign(event.input, scanResult.hookSpecificOutput.updatedInput);
        }
      }

      const predupResult = await runScript("predup.py", {
        session_id: currentSessionId,
        tool_name: event.toolName,
        tool_input: event.input ?? {},
      });
      if (predupResult?.hookSpecificOutput?.permissionDecision) {
        return { block: true, reason: predupResult.hookSpecificOutput.permissionDecisionReason ?? "Blocked by predup" };
      }
    } catch {
      // fail-open
    }
  });

  pi.on("tool_result", async (event: any, ctx: any) => {
    try {
      const dedupResult = await runScript("dedup.py", {
        session_id: currentSessionId,
        tool_name: event.toolName,
        tool_input: event.input ?? {},
        tool_use_id: event.toolCallId ?? "",
        transcript_path: "",
        tool_response: event.content ?? "",
      });
      if (dedupResult?.hookSpecificOutput?.updatedToolOutput) {
        return { content: dedupResult.hookSpecificOutput.updatedToolOutput };
      }
    } catch {
      // fail-open
    }
  });

  // ---------------------------------------------------------------------------
  // nx-plan — interactive plan creation
  // ---------------------------------------------------------------------------
  pi.registerCommand("nx-plan", {
    description: "Decompose current task into tiered, self-contained steps and write plan file",
    handler: async (args: any, ctx: any) => {
      if (!ctx.hasUI) return;

      const planDir = path.join(DATA_DIR, "plan");
      fs.mkdirSync(planDir, { recursive: true });

      const sid = currentSessionId;
      const planPath = path.join(planDir, `${sid}.md`);

      // Gather repo context
      const branch = await pi.exec("git", ["rev-parse", "--abbrev-ref", "HEAD"], { cwd: NEXUM_ROOT });
      const gitStatus = await pi.exec("git", ["status", "--short"], { cwd: NEXUM_ROOT });

      // Ask user clarifying questions
      const goal = await ctx.ui.input("What task should nexum plan? Describe the goal.");
      if (!goal) { ctx.ui.notify("nx-plan cancelled.", "warning"); return; }

      // Determine number of steps
      const stepCountStr = await ctx.ui.input("How many steps? (2-8, or press Enter for auto-estimate)");
      const stepCount = stepCountStr ? Math.min(Math.max(parseInt(stepCountStr, 10) || 4, 2), 8) : 4;

      // Ask for scope and constraints
      const constraints = await ctx.ui.input("Any constraints, out-of-bounds areas, or acceptance criteria?");

      // Ask model selection per tier
      const mechanicalModel = await ctx.ui.input("Model for mechanical steps? (default: haiku)") || "haiku";
      const standardModel = await ctx.ui.input("Model for standard steps? (default: sonnet)") || "sonnet";
      const strongModel = await ctx.ui.input("Model for needs-strong steps? (default: opus)") || "opus";

      // Read last few script files to understand project structure
      const files = (await pi.exec("ls", ["-la"], { cwd: NEXUM_ROOT })) || "";

      // Write plan template
      const plan = [
        `# Plan: ${goal}`,
        "",
        `**Session:** ${sid}`,
        `**Generated:** ${new Date().toISOString().split("T")[0]}`,
        `**Task summary:** ${goal}`,
        "",
        "**Models:**",
        `- mechanical: ${mechanicalModel}`,
        `- standard: ${standardModel}`,
        `- needs-strong: ${strongModel}`,
        "",
        "---",
        "",
      ];
      for (let i = 1; i <= stepCount; i++) {
        plan.push(
          `### Step ${i}: `,
          `- route: `,
          `- files: `,
          `- objective: `,
          `- contract: `,
          `- scope: `,
          `- acceptance: `,
          "",
        );
      }
      fs.writeFileSync(planPath, plan.join("\n"));

      ctx.ui.notify(`Plan template written: ${planPath}`);
      ctx.ui.setWidget("nexum-plan", [
        `Goal: ${goal}`,
        `Steps: ${stepCount}`,
        `Models: mech=${mechanicalModel} std=${standardModel} strong=${strongModel}`,
        `File: ${planPath}`,
      ]);
    },
  });

  // ---------------------------------------------------------------------------
  // nx-build — execute a nexum plan
  // ---------------------------------------------------------------------------
  pi.registerCommand("nx-build", {
    description: "Execute a nexum plan: dispatch steps, verify acceptance, report cost",
    handler: async (args: any, ctx: any) => {
      const sid = currentSessionId;
      const planDir = path.join(DATA_DIR, "plan");
      const planPath = path.join(planDir, `${sid}.md`);

      if (!fs.existsSync(planPath)) {
        if (ctx.hasUI) ctx.ui.notify("No plan found. Run /nx-plan first.", "error");
        return;
      }

      // Parse plan file for models and steps
      const planContent = fs.readFileSync(planPath, "utf-8");
      const lines = planContent.split("\n");

      // Extract models
      const models: Record<string, string> = {};
      let inModels = false;
      for (const line of lines) {
        if (line.trim() === "---") break;
        if (line.trim() === "**Models:**") { inModels = true; continue; }
        if (inModels && line.startsWith("- ")) {
          const m = line.match(/-\s*(\w+):\s*(.+)/);
          if (m) models[m[1]] = m[2].trim();
        }
      }

      // Extract steps
      interface Step {
        index: number;
        route: string;
        files: string;
        objective: string;
        contract: string;
        scope: string;
        acceptance: string;
      }
      const steps: Step[] = [];
      let currentStep: Partial<Step> | null = null;
      for (const line of lines) {
        const stepMatch = line.match(/### Step (\d+):/);
        if (stepMatch) {
          if (currentStep && currentStep.index) steps.push(currentStep as Step);
          currentStep = { index: parseInt(stepMatch[1], 10) };
          continue;
        }
        if (!currentStep) continue;
        const routeMatch = line.match(/- route:\s*(.+)/);
        if (routeMatch) { currentStep.route = routeMatch[1].trim(); continue; }
        const filesMatch = line.match(/- files:\s*(.+)/);
        if (filesMatch) { currentStep.files = filesMatch[1]; continue; }
        const objMatch = line.match(/- objective:\s*(.+)/);
        if (objMatch) { currentStep.objective = objMatch[1]; continue; }
        const contractMatch = line.match(/- contract:\s*(.+)/);
        if (contractMatch) { currentStep.contract = contractMatch[1]; continue; }
        const scopeMatch = line.match(/- scope:\s*(.+)/);
        if (scopeMatch) { currentStep.scope = scopeMatch[1]; continue; }
        const accMatch = line.match(/- acceptance:\s*(.+)/);
        if (accMatch) { currentStep.acceptance = accMatch[1]; continue; }
      }
      if (currentStep && currentStep.index) steps.push(currentStep as Step);

      if (steps.length === 0) {
        if (ctx.hasUI) ctx.ui.notify("No steps found in plan.", "error");
        return;
      }

      // Group by route (tier): mechanical → standard → needs-strong
      const routeOrder = ["mechanical", "standard", "needs-strong"];
      const grouped: Record<string, Step[]> = { mechanical: [], standard: [], "needs-strong": [] };
      for (const s of steps) {
        const r = s.route || "standard";
        if (!grouped[r]) grouped[r] = [];
        grouped[r].push(s);
      }

      // Resolve actual model from ctx.model (pi.dev) or fallback
      const actualModelId = (ctx as any)?.model?.id || "";

      interface StepResult { index: number; status: "pass" | "fail"; }
      const results: StepResult[] = [];

      for (const tier of routeOrder) {
        const tierSteps = grouped[tier];
        if (tierSteps.length === 0) continue;

        const planModel = models[tier];
        const resolvedModel = actualModelId || planModel || tier;
        const isInherit = planModel === "current session model (inherit)";

        if (!isInherit && planModel) {
          try { await pi.setModel(planModel); } catch { /* best-effort */ }
        }

        for (const step of tierSteps) {
          // Build step message with full context
          const stepMsg = [
            `## Execute step ${step.index}: ${step.objective}`,
            "",
            `**Files:** ${step.files}`,
            `**Objective:** ${step.objective}`,
            `**Contract:** ${step.contract || "see acceptance"}`,
            `**Scope:** ${step.scope || "none"}`,
            "",
            `**Acceptance:** ${step.acceptance || "none"}`,
            "",
            `Implement this step. After implementation, verify acceptance.`,
            `Do not modify files outside the declared scope.`,
          ].join("\n");

          // Send to agent and wait
          await pi.sendUserMessage(stepMsg);

          // Wait for agent to become idle
          try { await ctx.waitForIdle(); } catch { /* timeout or abort */ }

          // Run acceptance via guardrail
          if (step.acceptance) {
            const { exitCode } = await pi.exec("python3", [
              path.join(SCRIPTS_DIR, "guardrail.py"),
              "--acceptance", step.acceptance,
              "--scope-root", NEXUM_ROOT,
              ...(step.files ? ["--changed", step.files] : []),
            ], { cwd: NEXUM_ROOT });

            const status = exitCode === 0 ? "pass" : "fail";
            results.push({ index: step.index, status });

            // Record in ledger with actual model ID
            await runScript("store.py", {
              cmd: "step-set",
              session: sid,
              planHash: "",
              index: step.index,
              status: status === "pass" ? "done" : "failed",
              title: step.objective,
              route: tier,
              tier: resolvedModel,
            });

            if (status === "fail") {
              if (ctx.hasUI) ctx.ui.notify(`Step ${step.index} failed. Retrying...`, "warning");
              // Retry once
              try { await pi.setModel(resolvedModel); } catch { /* best-effort */ }
              await pi.sendUserMessage(`Retry step ${step.index}.\nPrevious attempt failed.\n\n${stepMsg}`);
              try { await ctx.waitForIdle(); } catch { /* ignore */ }

              const retryExit = (await pi.exec("python3", [
                path.join(SCRIPTS_DIR, "guardrail.py"),
                "--acceptance", step.acceptance,
                "--scope-root", NEXUM_ROOT,
                ...(step.files ? ["--changed", step.files] : []),
              ], { cwd: NEXUM_ROOT })).exitCode;

              if (retryExit !== 0 && ctx.hasUI) {
                ctx.ui.notify(`Step ${step.index} failed after retry. Escalate manually.`, "error");
              }
            }
          } else {
            results.push({ index: step.index, status: "pass" });
          }
        }
      }

      // Final cost report
      const costChild = spawn("python3", [
        path.join(SCRIPTS_DIR, "cost_report.py"),
        "--session", sid,
      ], { env: { ...process.env, CLAUDE_PLUGIN_ROOT: NEXUM_ROOT } });

      let costOut = "";
      costChild.stdout.on("data", (d: Buffer) => { costOut += d.toString(); });
      costChild.on("close", () => {
        if (!ctx.hasUI) return;
        ctx.ui.setWidget("nexum-cost", costOut || "Cost report unavailable.");
      });
    },
  });

  // ---------------------------------------------------------------------------
  // nx-save — write session handoff to .nexum-data/handoff/<session_id>.md
  // ---------------------------------------------------------------------------
  pi.registerCommand("nx-save", {
    description: "Write a session handoff to .nexum-data/handoff/<session_id>.md",
    handler: async (args: any, ctx: any) => {
      const sessionId = args.session ?? currentSessionId;
      const cwd = args.cwd ?? NEXUM_ROOT;

      const child = spawn("python3", [
        path.join(SCRIPTS_DIR, "handoff.py"),
        "write",
        "--session",
        sessionId,
        "--cwd",
        cwd,
        ...(args.tokens ? ["--tokens", String(args.tokens)] : []),
      ], { env: { ...process.env, CLAUDE_PLUGIN_ROOT: NEXUM_ROOT } });

      let stdout = "";
      child.stdout.on("data", (d: Buffer) => { stdout += d.toString(); });

      return new Promise<void>((resolve) => {
        child.on("close", () => {
          try {
            const result = JSON.parse(stdout.trim() || "{}");
            if (!ctx.hasUI) { resolve(); return; }
            if (result.ok) {
              ctx.ui.notify?.({ title: "nx-save", message: "Handoff written to .nexum-data/handoff/" });
            } else {
              ctx.ui.notify?.({ title: "nx-save", message: "Handoff failed", type: "error" });
            }
          } catch {
            if (ctx.hasUI) ctx.ui.notify?.({ title: "nx-save", message: "Handoff failed", type: "error" });
          }
          resolve();
        });
        child.on("error", () => {
          if (ctx.hasUI) ctx.ui.notify?.({ title: "nx-save", message: "Handoff failed", type: "error" });
          resolve();
        });
      });
    },
  });

  // ---------------------------------------------------------------------------
  // nx-load — read latest handoff, display summary
  // ---------------------------------------------------------------------------
  pi.registerCommand("nx-load", {
    description: "Read the latest handoff and display a summary",
    handler: async (_args: any, ctx: any) => {
      const latestPath = path.join(DATA_DIR, "handoff", "latest.md");
      let content: string;
      try {
        content = fs.readFileSync(latestPath, "utf-8");
      } catch {
        if (ctx.hasUI) ctx.ui.notify?.({ title: "nx-load", message: "No handoff found", type: "warning" });
        return;
      }

      if (!ctx.hasUI) return;

      // Show first 8 lines as a summary notification
      const summary = content.split("\n").slice(0, 8).join("\n");
      ctx.ui.notify?.({ title: "nx-load", message: summary });
      ctx.ui.setWidget?.({ id: "nexum-handoff", content });
    },
  });

  // ---------------------------------------------------------------------------
  // nx-audit — run audit.py, show findings, offer --write fix
  // ---------------------------------------------------------------------------
  pi.registerCommand("nx-audit", {
    description: "Run audit.py, show findings, offer --write fix",
    handler: async (args: any, ctx: any) => {
      const root = args.root ?? NEXUM_ROOT;

      // First pass: read-only audit
      const child = spawn("python3", [
        path.join(SCRIPTS_DIR, "audit.py"),
        "--root", root,
      ], { env: { ...process.env, CLAUDE_PLUGIN_ROOT: NEXUM_ROOT } });

      let stdout = "";
      child.stdout.on("data", (d: Buffer) => { stdout += d.toString(); });

      return new Promise<void>((resolve) => {
        child.on("close", async () => {
          if (!ctx.hasUI) { resolve(); return; }

          // Show findings
          ctx.ui.setWidget?.({ id: "nexum-audit", content: stdout || "Audit returned no output." });

          // If issues found and user didn't already pass --write, offer to fix
          if (!args.write && stdout.includes("issues found")) {
            const confirmed = await ctx.ui.confirm?.("Apply suggested ignore-file patterns?");
            if (confirmed) {
              const writer = spawn("python3", [
                path.join(SCRIPTS_DIR, "audit.py"),
                "--root", root, "--write",
              ], { env: { ...process.env, CLAUDE_PLUGIN_ROOT: NEXUM_ROOT } });
              let wOut = "";
              writer.stdout.on("data", (d: Buffer) => { wOut += d.toString(); });
              writer.on("close", () => {
                ctx.ui.setWidget?.({ id: "nexum-audit-result", content: wOut || "Patterns applied." });
                ctx.ui.notify?.({ title: "nx-audit", message: "Ignore-file patterns applied." });
                resolve();
              });
              writer.on("error", () => { resolve(); });
              return;
            }
            ctx.ui.notify?.({ title: "nx-audit", message: "Audit complete (no changes)." });
          } else {
            ctx.ui.notify?.({ title: "nx-audit", message: "Audit complete." });
          }
          resolve();
        });
        child.on("error", () => {
          if (ctx.hasUI) ctx.ui.notify?.({ title: "nx-audit", message: "Audit failed", type: "error" });
          resolve();
        });
      });
    },
  });

  // ---------------------------------------------------------------------------
  // nx-report — run report.py, display session digest
  // ---------------------------------------------------------------------------
  pi.registerCommand("nx-report", {
    description: "Run report.py and display session digest",
    handler: async (args: any, ctx: any) => {
      const sessionArg = args.session ? ["--session", args.session] : [];

      const child = spawn("python3", [
        path.join(SCRIPTS_DIR, "report.py"),
        ...sessionArg,
      ], { env: { ...process.env, CLAUDE_PLUGIN_ROOT: NEXUM_ROOT } });

      let stdout = "";
      child.stdout.on("data", (d: Buffer) => { stdout += d.toString(); });

      return new Promise<void>((resolve) => {
        child.on("close", () => {
          if (!ctx.hasUI) { resolve(); return; }
          ctx.ui.setWidget?.({ id: "nexum-report", content: stdout || "No report data." });
          ctx.ui.notify?.({ title: "nx-report", message: "Report generated." });
          resolve();
        });
        child.on("error", () => {
          if (ctx.hasUI) ctx.ui.notify?.({ title: "nx-report", message: "Report failed", type: "error" });
          resolve();
        });
      });
    },
  });

  // ---------------------------------------------------------------------------
  // nx-status — run report.py --session, display compact stats
  // ---------------------------------------------------------------------------
  pi.registerCommand("nx-status", {
    description: "Run report.py --session and display compact stats",
    handler: async (args: any, ctx: any) => {
      const sessionId = args.session ?? currentSessionId;

      const child = spawn("python3", [
        path.join(SCRIPTS_DIR, "report.py"),
        "--session", sessionId,
      ], { env: { ...process.env, CLAUDE_PLUGIN_ROOT: NEXUM_ROOT } });

      let stdout = "";
      child.stdout.on("data", (d: Buffer) => { stdout += d.toString(); });

      return new Promise<void>((resolve) => {
        child.on("close", () => {
          if (!ctx.hasUI) { resolve(); return; }
          const lines = (stdout || "").split("\n").filter((l) => l.trim());
          const compact = lines.slice(0, 10).join("\n");
          ctx.ui.setWidget?.({ id: "nexum-status", content: compact || "No data." });
          resolve();
        });
        child.on("error", () => {
          if (ctx.hasUI) ctx.ui.notify?.({ title: "nx-status", message: "Status failed", type: "error" });
          resolve();
        });
      });
    },
  });
}
