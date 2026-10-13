/**
 * g03-conductor pi extension: thin TS shell over `python3 -I tools/g03_conductor <cmd>` (design §8–§9).
 *
 * No business logic lives here. Python decides stages, statistics, gates and termination; this file only
 *   - registers g03_status / g03_propose / g03_run / g03_note (no stage-transition tool exists),
 *   - blocks disallowed conductor tools and write/edit on conductor-owned or locked paths,
 *   - on agent_settled calls `next` and follows its action (instruction, forced review, auto run, stop),
 *   - shows the read-only widget and the /g03 view; /g03 pause|resume|stop write control.json.
 *
 * Launch (interactive mode, ideally inside tmux):
 *   G03_ROOT=<private-root> pi --no-extensions -e ~/.pi/agent/extensions/subagent -e extensions/g03-conductor
 */

import { spawn } from "node:child_process";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { matchesKey, truncateToWidth } from "@earendil-works/pi-tui";
import { Type } from "typebox";
import { CONDUCTOR, isUnder, loadGuardPaths, mutatingBash } from "./guard/index.ts";

const PY = process.env.G03_PYTHON ?? "python3";
const CONDUCTOR_TOOLS = new Set(["g03_status", "g03_propose", "g03_run", "g03_note"]);

type Json = Record<string, any>;

export function runCli(args: string[], opts: { stdin?: string; signal?: AbortSignal } = {}): Promise<Json> {
	return new Promise((resolve) => {
		const proc = spawn(PY, ["-I", "-B", CONDUCTOR, ...args], { stdio: ["pipe", "pipe", "pipe"], signal: opts.signal });
		let out = "";
		let err = "";
		proc.stdout.on("data", (d) => (out += d));
		proc.stderr.on("data", (d) => (err += d));
		proc.on("error", (e) => resolve({ ok: false, code: "SPAWN", error: String(e) }));
		proc.on("close", () => {
			try {
				resolve(JSON.parse(out));
			} catch {
				resolve({ ok: false, code: "BAD_OUTPUT", error: (out + err).slice(-2000) });
			}
		});
		proc.stdin.end(opts.stdin ?? "");
	});
}

function text(obj: unknown) {
	return { content: [{ type: "text" as const, text: typeof obj === "string" ? obj : JSON.stringify(obj, null, 1) }], details: obj };
}

export default function (pi: ExtensionAPI) {
	const root = process.env.G03_ROOT;
	if (!root) {
		pi.registerCommand("g03", {
			description: "g03-conductor (inactive: set G03_ROOT)",
			handler: async (_a, ctx) => ctx.ui.notify("g03-conductor inactive: start pi with G03_ROOT=<private root>", "warning"),
		});
		return;
	}
	const R = ["--root", root];
	let driving = false;
	let finished = false;

	async function refreshWidget(ctx: ExtensionContext) {
		if (!ctx.hasUI) return;
		const s = await runCli(["status", ...R]);
		ctx.ui.setWidget("g03", s.ok ? s.widget : [`G03 状态读取失败: ${s.error ?? s.code}`]);
	}

	// --- tools -------------------------------------------------------------------------------------
	pi.registerTool({
		name: "g03_status",
		label: "G03 status",
		description: "Read the G03 conductor status: stage, side state, feasible window, budget, allowed tools, locked paths.",
		promptSnippet: "g03_status: current G03 stage, window, budget and what is allowed now",
		parameters: Type.Object({ detail: Type.Optional(Type.Boolean({ description: "include timeline/candidate table" })) }),
		async execute(_id, params) {
			return text(await runCli(["status", ...R, ...(params.detail ? ["--detail"] : [])]));
		},
	});

	pi.registerTool({
		name: "g03_propose",
		label: "G03 propose",
		description:
			"Submit a protocol.yaml draft (kind=protocol, content=full YAML), register a new candidate directory " +
			"(kind=candidate, description=method idea; returns g03/candidates/vN for g03-worker to implement), or file " +
			"a claim that the protocol is infeasible (kind=infeasible) or that scorer/Baseline/data has a defect (kind=defect). " +
			"Claims are reviewed at the next gate; you cannot change stages yourself.",
		parameters: Type.Object({
			kind: Type.Union([Type.Literal("protocol"), Type.Literal("candidate"), Type.Literal("infeasible"), Type.Literal("defect")]),
			content: Type.Optional(Type.String({ description: "protocol YAML (kind=protocol)" })),
			description: Type.Optional(Type.String()),
			source: Type.Optional(Type.String({ description: "existing directory to copy as the candidate start (optional)" })),
			evidence: Type.Optional(Type.Array(Type.String())),
		}),
		async execute(_id, p, signal) {
			const args = ["propose", ...R, "--kind", p.kind];
			if (p.kind === "protocol") args.push("--file", "-");
			if (p.description) args.push("--description", p.description);
			if (p.source) args.push("--source", p.source);
			for (const e of p.evidence ?? []) args.push("--evidence", e);
			return text(await runCli(args, { stdin: p.content ?? "", signal }));
		},
	});

	pi.registerTool({
		name: "g03_run",
		label: "G03 run",
		description:
			"The only experiment entry. Runs train+score for kind=diag|dev|screen (method=base or vN) on the stage's " +
			"declared seeds; formal kinds are run automatically by the conductor. Blocks until done (GPU via gpu_scheduler).",
		parameters: Type.Object({
			kind: Type.Union([Type.Literal("diag"), Type.Literal("dev"), Type.Literal("screen")]),
			method: Type.Optional(Type.String({ description: "base or candidate version vN" })),
			seeds: Type.Optional(Type.Array(Type.Integer(), { description: "subset of the stage seeds (default: all)" })),
			mult: Type.Optional(Type.Number({ description: "diag only: budget multiplier from diag_budget_multipliers" })),
		}),
		executionMode: "sequential",
		async execute(_id, p, signal, _u, ctx) {
			const args = ["run", ...R, "--kind", p.kind, "--method", p.method ?? "base"];
			for (const s of p.seeds ?? []) args.push("--seed", String(s));
			if (p.mult !== undefined) args.push("--mult", String(p.mult));
			const out = await runCli(args, { signal });
			await refreshWidget(ctx);
			return text(out);
		},
	});

	pi.registerTool({
		name: "g03_note",
		label: "G03 note",
		description: "Append an ASI / negative-result / diagnosis note to the ledger (tag=diagnosis is required in S1).",
		parameters: Type.Object({ tag: Type.String(), text: Type.String() }),
		async execute(_id, p) {
			return text(await runCli(["note", ...R, "--tag", p.tag, "--text", p.text]));
		},
	});

	// --- gating ------------------------------------------------------------------------------------
	pi.on("tool_call", async (event, ctx) => {
		const name = event.toolName;
		const input = event.input as Json;
		if (CONDUCTOR_TOOLS.has(name)) {
			if (name === "g03_status") return undefined;
			const kind = name === "g03_note" ? undefined : input.kind;
			const res = await runCli(["allowed", ...R, "--tool", name, ...(kind ? ["--kind", String(kind)] : [])]);
			if (!res.ok || !res.allowed) return { block: true, reason: `g03: ${res.reason ?? res.error ?? "not allowed"}` };
			return undefined;
		}
		if (name === "write" || name === "edit") {
			const target = typeof input.path === "string" ? input.path : undefined;
			if (!target) return undefined;
			const guard = loadGuardPaths(root, PY);
			if (!guard) return { block: true, reason: "g03 guard unavailable; refusing write (fail closed)" };
			const hit = isUnder(target, guard.protected, ctx.cwd) ?? isUnder(target, guard.locked, ctx.cwd);
			if (hit && !isUnder(target, guard.writable, ctx.cwd)) {
				return { block: true, reason: `g03: ${hit} is conductor-owned or locked; use g03_propose/candidates instead` };
			}
		}
		if (name === "bash" && typeof input.command === "string" && /g03\/(ledger\.jsonl|lock\.json|control\.json)/.test(input.command)
			&& mutatingBash(input.command)) {
			return { block: true, reason: "g03: the ledger/lock/control files are conductor-owned" };
		}
		return undefined;
	});

	// --- driver ------------------------------------------------------------------------------------
	async function drive(ctx: ExtensionContext) {
		if (driving || finished) return;
		driving = true;
		try {
			for (let i = 0; i < 50; i++) {
				const res = await runCli(["next", ...R]);
				await refreshWidget(ctx);
				if (!res.ok) {
					ctx.ui.notify(`g03 next failed: ${res.error ?? res.code}`, "error");
					return;
				}
				switch (res.action) {
					case "review": {
						if (ctx.hasUI) ctx.ui.setStatus("g03", `reviewer: ${res.gate}`);
						const rv = await runCli(["review-run", ...R, "--gate", res.gate, "--subject", res.subject]);
						if (ctx.hasUI) ctx.ui.setStatus("g03", undefined);
						if (!rv.ok) ctx.ui.notify(`reviewer ${res.gate}: ${rv.error ?? rv.code}`, "warning");
						continue;
					}
					case "auto_run": {
						const ar = res.auto_run;
						if (ctx.hasUI) ctx.ui.setStatus("g03", `auto ${ar.kind} ${ar.method} ${ar.seeds.length} seeds`);
						const args = ["run", ...R, "--auto", "--kind", ar.kind, "--method", ar.method];
						for (const s of ar.seeds) args.push("--seed", String(s));
						const out = await runCli(args);
						if (ctx.hasUI) ctx.ui.setStatus("g03", undefined);
						if (!out.ok && out.code !== "BLOCKED_INFRA") ctx.ui.notify(`auto run: ${out.error ?? out.code}`, "warning");
						continue;
					}
					case "continue":
						await pi.sendUserMessage(`[g03-conductor ${res.stage}] ${res.message}`, { deliverAs: "followUp" });
						return;
					case "terminal":
						finished = true;
						ctx.ui.notify(res.message, res.terminal?.status === "DONE" ? "info" : "warning");
						await refreshWidget(ctx);
						ctx.shutdown();
						return;
					default: // paused | blocked | wait_expert: stop driving until the expert acts
						ctx.ui.notify(`[g03 ${res.action}] ${res.message}`, "warning");
						return;
				}
			}
			ctx.ui.notify("g03 driver step limit reached; run /g03 resume", "warning");
		} finally {
			driving = false;
		}
	}

	pi.on("session_start", async (_e, ctx) => {
		await refreshWidget(ctx);
	});

	pi.on("agent_settled", async (event, ctx) => {
		if (event.aborted) return; // Escape: the expert takes over; /g03 resume continues
		await drive(ctx);
	});

	// --- /g03 --------------------------------------------------------------------------------------
	pi.registerCommand("g03", {
		description: "G03 conductor: view | pause | resume | stop | next",
		getArgumentCompletions: (prefix) =>
			["pause", "resume", "stop", "next"].filter((a) => a.startsWith(prefix)).map((a) => ({ value: a, label: a })),
		handler: async (args, ctx) => {
			const [action, ...rest] = args.trim().split(/\s+/).filter(Boolean);
			if (action === "pause" || action === "resume" || action === "stop") {
				const out = await runCli(["control", ...R, "--action", action, ...(rest.length ? ["--reason", rest.join(" ")] : [])]);
				ctx.ui.notify(out.ok ? `g03 ${action} 已写入 control.json（下一关卡生效）` : `g03 ${action} failed: ${out.error}`, out.ok ? "info" : "error");
				if (action !== "pause" && ctx.isIdle()) await drive(ctx);
				return;
			}
			if (action === "next") {
				if (ctx.isIdle()) await drive(ctx);
				return;
			}
			const s = await runCli(["status", ...R, "--detail"]);
			const lines: string[] = s.ok ? [...s.widget, "", ...s.detail] : [`status failed: ${s.error}`];
			if (!ctx.hasUI || ctx.mode !== "tui") {
				ctx.ui.notify(lines.join("\n"), "info");
				return;
			}
			await ctx.ui.custom<void>((_tui, theme, _kb, done) => new DetailView(lines, theme, done), { overlay: true });
		},
	});
}

class DetailView {
	private offset = 0;
	constructor(private lines: string[], private theme: any, private done: () => void) {}
	render(width: number): string[] {
		const height = Math.max(5, (process.stdout.rows ?? 40) - 6);
		const body = this.lines.slice(this.offset, this.offset + height).map((l) => truncateToWidth(l, width));
		const footer = this.theme.fg("dim", `  ↑/↓ PgUp/PgDn 滚动 · q/Esc 关闭 · ${this.offset + 1}-${Math.min(this.lines.length, this.offset + height)}/${this.lines.length}`);
		return [this.theme.fg("accent", "G03 conductor（只读）"), ...body, footer];
	}
	handleInput(data: string) {
		const page = Math.max(5, (process.stdout.rows ?? 40) - 6);
		if (matchesKey(data, "escape") || data === "q") return this.done();
		if (matchesKey(data, "up")) this.offset = Math.max(0, this.offset - 1);
		else if (matchesKey(data, "down")) this.offset = Math.min(Math.max(0, this.lines.length - 1), this.offset + 1);
		else if (matchesKey(data, "pageUp")) this.offset = Math.max(0, this.offset - page);
		else if (matchesKey(data, "pageDown")) this.offset = Math.min(Math.max(0, this.lines.length - 1), this.offset + page);
	}
	invalidate() {}
}
