/**
 * g03 guard: path/role gate for pi sessions inside a g03-conductor run.
 *
 * Inert unless G03_ROOT is set. Roles (G03_ROLE):
 *   reviewer  - read-only: blocks write/edit and mutating bash (best effort).
 *   worker    - blocks write/edit to conductor-owned and locked paths.
 * bash writes cannot be intercepted reliably; locked-file tampering is detected by hash checks in Python
 * (design §1: detection, not prevention).
 */

import { spawnSync } from "node:child_process";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const CONDUCTOR = path.resolve(HERE, "..", "..", "..", "tools", "g03_conductor");

// Mutating shell patterns. Heuristic only: the authority is the Python hash check.
const MUTATING_BASH = [
	/(^|[^<>&0-9])>{1,2}(?!&)/, // > or >> redirection (not <, >&, 2>&1)
	/\btee\b/,
	/\b(rm|mv|cp|ln|chmod|chown|truncate|dd|install|mkdir|rmdir|touch)\b/,
	/\bsed\b[^|;&]*\s-i/,
	/\bperl\b[^|;&]*\s-i/,
	/\bgit\s+(commit|push|reset|checkout|restore|clean|rebase|merge|apply|stash|rm|mv|add)\b/,
	/\b(pip|uv|npm|pnpm|yarn)\s+(install|add|remove|uninstall)\b/,
	/\bpython[0-9.]*\b(?![^|;&]*\s-I\b)/, // python without -I isolation
];

export interface GuardPaths {
	protected: string[];
	locked: string[];
	writable: string[];
}

export function loadGuardPaths(root: string, python = "python3"): GuardPaths | undefined {
	const res = spawnSync(python, ["-I", "-B", CONDUCTOR, "guard", "--root", root], { encoding: "utf8", timeout: 30_000 });
	try {
		const out = JSON.parse(res.stdout);
		if (out.ok) return { protected: out.protected, locked: out.locked, writable: out.writable };
	} catch {
		/* fall through */
	}
	return undefined;
}

export function isUnder(target: string, prefixes: string[], cwd: string): string | undefined {
	const abs = path.resolve(cwd, target);
	return prefixes.find((p) => abs === p || abs.startsWith(p.endsWith(path.sep) ? p : p + path.sep));
}

export function mutatingBash(command: string): boolean {
	return MUTATING_BASH.some((re) => re.test(command));
}

export default function (pi: ExtensionAPI) {
	const root = process.env.G03_ROOT;
	const role = process.env.G03_ROLE ?? "worker";
	if (!root) return;

	pi.on("tool_call", async (event, ctx) => {
		const name = event.toolName;
		const input = event.input as Record<string, unknown>;
		if (role === "reviewer") {
			if (name === "write" || name === "edit") {
				return { block: true, reason: "g03 reviewer is read-only" };
			}
			if (name === "bash" && typeof input.command === "string" && mutatingBash(input.command)) {
				return { block: true, reason: "g03 reviewer bash is read-only (cat/ls/grep/sha256sum/git log|diff|show/python3 -I)" };
			}
			return undefined;
		}
		if (name !== "write" && name !== "edit") return undefined;
		const target = typeof input.path === "string" ? input.path : undefined;
		if (!target) return undefined;
		const guard = loadGuardPaths(root);
		if (!guard) return { block: true, reason: "g03 guard unavailable; refusing write (fail closed)" };
		const hit = isUnder(target, guard.protected, ctx.cwd) ?? isUnder(target, guard.locked, ctx.cwd);
		if (hit && !isUnder(target, guard.writable, ctx.cwd)) {
			return { block: true, reason: `g03: ${hit} is conductor-owned or locked; it cannot be modified` };
		}
		return undefined;
	});
}
