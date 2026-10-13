"""Read-only status, widget and /g03 detail lines (design §9). Never mutates the ledger."""
from __future__ import annotations

import time

from . import stats as S
from .state import ALLOWED

STAGE_NAMES = {"S0": "立题", "S1": "Baseline 诊断", "S1_5": "可行性预检", "S2": "冻结", "S3": "Baseline 正式",
               "S4": "探索", "S5": "正式确认", "S6": "判定", "DONE": "完成", "STOP_UNMET": "未达标停止"}


def _dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec % 60:02d}s"


def _f(x) -> str:
    return "-" if x is None else f"{x:.4g}"


class Views:
    def protected_paths(self) -> dict:
        d = self.dir
        prot = [str(d / x) for x in ("ledger.jsonl", "control.json", "lock.json", "protocol.yaml", "locks", "runs",
                                     "reviews", "frozen", "summary", "final", "drafts", "notes", ".conductor.lock")]
        locked = []
        st = self.st
        try:
            proto = self.proto()
        except Exception:
            proto = None
        if proto and (st.baseline_fixed or st.lock):
            locked.append(str(self.rpath(proto["paths"]["baseline"])))
        if proto and st.lock:
            from .protocol import scorer_paths
            locked += [str(self.rpath(s)) for s in scorer_paths(proto)]
            locked += [str(self.rpath(e["path"])) for e in proto["data"].values()]
        return {"protected": prot, "locked": locked, "writable": [str(d / "candidates")]}

    def _window_view(self) -> dict | None:
        st = self.st
        try:
            proto = self.proto()
        except Exception:
            return None
        direction, U = proto["metric"]["direction"], proto["U"]["value"]
        if st.formal_b_stats:
            fb = st.formal_b_stats
            win, src, B = fb["window"], "formal", fb["B"]
            kind = "screen"
        else:
            dev = [r for r in st.runs.values() if r["method"] == "base" and r["kind"] in ("dev", "diag")
                   and r["mult"] == 1 and r["status"] == "VALID" and r["major"] == st.major]
            by = {}
            for r in sorted(dev, key=lambda r: r["seq"]):
                by[r["seed"]] = r["metric"]
            vals = [by[s] for s in proto["seeds"]["dev"] if s in by]
            if len(vals) < 2:
                return {"source": None, "U": U}
            B, sig = S.mean(vals), S.sample_std(vals)
            win, src, kind = S.window(direction, B, sig, U), "dev", "dev"
        ref_B = win["B"]
        if src == "formal":
            base_s = [r["metric"] for r in st.runs.values() if r["method"] == "base" and r["kind"] == "screen"
                      and r["status"] == "VALID" and r["major"] == st.major]
            ref_B = S.mean(base_s) if base_s else None
        best = None
        for v in st.candidates if ref_B is not None else ():
            if v in st.rejected:
                continue
            rs = [r for r in st.runs.values() if r["method"] == v and r["kind"] == kind and r["status"] == "VALID"
                  and r["major"] == st.major]
            if rs:
                d = S.signed_delta(direction, ref_B, S.mean([r["metric"] for r in rs]))
                if best is None or d > best[1]:
                    best = (v, d)
        return {"source": src, "B": win["B"], "U": U, "sigma": win["sigma_B"], "d_min": win.get("d_min"),
                "d_max": win.get("d_max"), "valid": win["valid"], "best": best}

    def _axis(self, w: dict, width: int = 40) -> str:
        if not w or w.get("source") is None or w.get("d_min") is None:
            return "窗口：尚无足够 Baseline 数据"
        gap = abs(w["U"] - w["B"]) or 1.0
        cells = ["─"] * (width + 1)

        def pos(delta):
            return max(0, min(width, round(delta / gap * width)))
        cells[pos(w["d_min"])] = "["
        cells[pos(w["d_max"])] = "]"
        if w.get("best"):
            cells[pos(w["best"][1])] = "▲"
        tail = f" best {w['best'][0]} Δ={_f(w['best'][1])}" if w.get("best") else ""
        return f"B={_f(w['B'])} {''.join(cells)} U={_f(w['U'])}  Δ∈[{_f(w['d_min'])},{_f(w['d_max'])}] σ={_f(w['sigma'])} ({w['source']}){tail}"

    def status(self) -> dict:
        st = self.st
        tot = self.cfg["budget_total"]
        last = st.events[-1] if st.events else {}
        try:
            proto = self.proto()
            K = proto["K"]
        except Exception:
            proto, K = None, 2
        allowed = {tool: (sorted(kinds) if kinds is not None else None) for tool, kinds in ALLOWED[st.stage].items()}
        w = self._window_view()
        out = {
            "ok": True, "task_id": self.cfg["task_id"], "stage": st.stage, "stage_name": STAGE_NAMES[st.stage],
            "side": st.side(), "stage_since": st.stage_since, "stage_age": _dur(time.time() - st.stage_since),
            "major": st.major, "draft_id": st.draft_id, "lock_id": st.lock_id, "k": st.attempts_a, "K": K,
            "revisions_c": st.rev_c, "revisions_b": st.rev_b,
            "budget": {"gpu_hours_used": st.gpu_seconds / 3600, "gpu_hours": tot["gpu_hours"],
                       "deadline_at": tot["deadline_at"], "wall_left": _dur(tot["deadline_epoch"] - time.time())},
            "last_event": {k: last.get(k) for k in ("seq", "type", "at")} | ({"to": last.get("to")} if last.get("type") == "transition" else {}),
            "window": w, "allowed": allowed, "guard": self.protected_paths(), "terminal": st.terminal,
            "paused": st.paused, "blocked": st.blocked,
        }
        out["widget"] = self.widget_lines(out)
        return out

    def widget_lines(self, s: dict) -> list[str]:
        side = f" [{s['side']}]" if s["side"] else ""
        b = s["budget"]
        lines = [
            f"G03 {s['task_id']} │ {s['stage']} {s['stage_name']}{side} │ 停留 {s['stage_age']} │ p{s['major']}",
            self._axis(s["window"]),
            f"预算 GPU {b['gpu_hours_used']:.2f}/{b['gpu_hours']:g}h │ 墙钟剩余 {b['wall_left']} │ k={s['k']}/{s['K']} │ 修订 {s['revisions_c']}/1",
        ]
        le = s["last_event"]
        lines.append(f"最近：#{le.get('seq')} {le.get('type')}{' → ' + le['to'] if le.get('to') else ''} @ {le.get('at')}")
        return lines

    def detail_lines(self) -> list[str]:
        st = self.st
        hide_formal_r = st.stage in ("S5",)
        out = ["== 阶段时间线 =="]
        for t in st.transitions:
            out.append(f"  {t['at']}  {t['from'] or '-'} → {t['to']}  {t['reason']}")
        out.append("== 候选（含负结果）==")
        for v, c in sorted(st.candidates.items(), key=lambda kv: int(kv[0][1:])):
            runs = [r for r in st.runs.values() if r["method"] == v and r["kind"] in ("dev", "screen")]
            done = ", ".join(f"{r['kind']}:{r['seed']}={'✗' if r['status'] != 'VALID' else _f(r['metric'])}" for r in runs)
            tag = "REJECTED: " + st.rejected[v]["reason"] if v in st.rejected else ("FROZEN" if st.frozen and st.frozen["version"] == v else "")
            out.append(f"  {v} {tag}  {c['description'][:60]}")
            if done:
                out.append(f"      {done}")
        if st.group:
            out.append(f"== 正式确认（组 {st.group['idx']}，seeds {st.group['seeds']}）==")
            for kind, method in (("formal_b", "base"), ("formal_r", st.frozen["version"] if st.frozen else None)):
                if method is None:
                    continue
                cells = []
                for s in st.group["seeds"]:
                    rs = [r for r in st.runs.values() if r["kind"] == kind and r["seed"] == s and r["major"] == st.major
                          and r["request_id"] not in st.invalidated and r.get("group_idx") == st.group["idx"]]
                    if not rs:
                        cells.append(f"{s}:…")
                    elif rs[-1]["status"] != "VALID":
                        cells.append(f"{s}:✗")
                    else:
                        cells.append(f"{s}:✓" if (kind == "formal_r" and hide_formal_r) else f"{s}:{_f(rs[-1]['metric'])}")
                out.append(f"  {kind:<8} " + "  ".join(cells))
            if hide_formal_r:
                out.append("  （正式确认跑完前不显示中间统计）")
        if st.confirms:
            out.append("== 确认尝试 ==")
            for c in st.confirms:
                v = c["verdict"] or {}
                out.append(f"  k={c['k']} {c['version']} 组{c['group_idx']} {'作废' if c['seq'] in st.voided_confirms else ''}"
                           f" {'无效' if c['invalid'] else v.get('label')} Δ/σ={_f(v.get('sigma_multiple'))} score={_f(v.get('score'))}")
        if st.failures or st.revisions:
            out.append("== 失败与回退 ==")
            for f in st.failures:
                out.append(f"  #{f['seq']} 类型 {f['class']} p{f['major']}: {f['reason']}")
        if st.reviews:
            out.append("== 审查 ==")
            for r in st.reviews[-12:]:
                out.append(f"  #{r['seq']} {r['gate']} {r['verdict']} defect={r['defect']} {r.get('problem') or ''} → {r['report']}")
        return out
