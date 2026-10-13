// P2 harness: loads the extension with a mock ExtensionAPI and drives it with a scripted "main session".
// No LLM, no GPU. Usage: node extensions/g03-conductor/test/harness.mjs <root> <protocol.yaml> <methods_dir> [good|overfit]
import { execFileSync } from "node:child_process";
import { copyFileSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const [root, protoFile, methodsDir, method = "good"] = process.argv.slice(2);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const piPkg = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim() + "/@earendil-works/pi-coding-agent/";
const { createJiti } = createRequire(piPkg + "package.json")("jiti");
// Same module table pi uses for extensions, so imports resolve exactly as in a real session.
const { VIRTUAL_MODULES } = await import(piPkg + "dist/core/extensions/virtual-modules.js");
const jiti = createJiti(piPkg, { moduleCache: false, virtualModules: VIRTUAL_MODULES, tryNative: false });
process.env.G03_ROOT = root;
const ext = (await jiti.import(path.join(HERE, "..", "index.ts"))).default;

const tools = new Map(), handlers = new Map(), commands = new Map(), inbox = [], notes = [];
let widget = null, shutdown = false;
const pi = {
	registerTool: (t) => tools.set(t.name, t),
	registerCommand: (n, o) => commands.set(n, o),
	on: (ev, h) => (handlers.set(ev, [...(handlers.get(ev) ?? []), h]), () => {}),
	sendUserMessage: async (m) => inbox.push(m),
};
const ctx = {
	cwd: root, hasUI: true, mode: "tui", isIdle: () => true, shutdown: () => (shutdown = true),
	ui: { setWidget: (_k, l) => (widget = l), setStatus: () => {}, notify: (m, t) => notes.push(`${t}: ${m}`) },
};
ext(pi);
const fire = async (ev, e = {}) => { for (const h of handlers.get(ev) ?? []) await h({ type: ev, ...e }, ctx); };

// Calls a tool through the tool_call gate exactly as pi would.
async function call(toolName, input) {
	for (const h of handlers.get("tool_call") ?? []) {
		const r = await h({ type: "tool_call", toolName, input, toolCallId: "t" }, ctx);
		if (r?.block) return { blocked: r.reason };
	}
	const t = tools.get(toolName);
	if (!t) return { ok: true, builtin: toolName };
	return (await t.execute("t", input, undefined, undefined, ctx)).details;
}
const assert = (c, m) => { if (!c) { console.error("ASSERT FAILED:", m); process.exit(1); } };

assert(["g03_status", "g03_propose", "g03_run", "g03_note"].every((n) => tools.has(n)), "tools registered");
assert(commands.has("g03"), "/g03 registered");
assert(![...tools.keys()].some((n) => /transition|advance|stage/.test(n)), "no stage-transition tool");
await fire("session_start");
assert(widget && widget[0].startsWith("G03"), "widget shown");

const checks = [];
let steps = 0;
await fire("agent_settled", { aborted: false });
while (!shutdown && steps++ < 200) {
	const msg = inbox.shift();
	assert(msg, `driver stalled; notes=${JSON.stringify(notes.slice(-3))}`);
	const stage = /\[g03-conductor (\w+)\]/.exec(msg)[1];
	if (stage === "S0") {
		assert((await call("g03_run", { kind: "diag" })).blocked, "S0 blocks g03_run");
		await call("g03_propose", { kind: "protocol", content: readFileSync(protoFile, "utf8") });
	} else if (stage === "S1") {
		const w = await call("write", { path: path.join(root, "g03", "ledger.jsonl"), content: "x" });
		assert(w.blocked, "write to ledger blocked");
		assert((await call("g03_run", { kind: "screen" })).blocked, "S1 blocks screen");
		assert((await call("bash", { command: `echo x >> ${root}/g03/ledger.jsonl` })).blocked, "bash ledger write blocked");
		await call("g03_run", { kind: "diag" });
		await call("g03_run", { kind: "diag", mult: 2 });
		await call("g03_note", { tag: "diagnosis", text: "harness: x2 budget matches x1; converged" });
		checks.push("S1 gates");
	} else if (stage === "S1_5" || stage === "S4") {
		if (stage === "S4") {
			const scorer = path.join(root, "scorer", "score.py");
			assert((await call("edit", { path: scorer, edits: [] })).blocked, "locked scorer edit blocked");
			checks.push("S4 lock gate");
		}
		const c = await call("g03_propose", { kind: "candidate", description: `harness ${method}` });
		copyFileSync(path.join(methodsDir, method, "method.json"), path.join(c.path, "method.json"));
		const w = await call("write", { path: path.join(c.path, "notes.md"), content: "ok" });
		assert(!w.blocked, "candidate dir writable");
		await call("g03_run", { kind: stage === "S1_5" ? "dev" : "screen", method: c.version });
	} else {
		assert(false, `unexpected continue in ${stage}: ${msg}`);
	}
	await fire("agent_settled", { aborted: false });
}
assert(shutdown, "ctx.shutdown() called at terminal");
const st = JSON.parse(execFileSync("python3", ["-I", "-B", path.join(HERE, "..", "..", "..", "tools", "g03_conductor"), "status", "--root", root]));
console.log(JSON.stringify({ terminal: st.terminal, checks, steps, widget, lastNotes: notes.slice(-2) }, null, 1));
