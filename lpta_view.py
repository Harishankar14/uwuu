import html as _html
import http.server
import json
import math
import os
import re
import socket
import socketserver
import sys
import tempfile
import webbrowser
from collections import Counter


def codegen_total(cg):
    """Sum per-function codegen records into one module-level dict."""
    if not cg:
        return None
    keys = ("asm_instrs", "code_size_bytes", "branches", "vector_instrs", "scalar_sse_instrs")
    tot = {k: 0 for k in keys}
    for rec in cg.values():
        for k in keys:
            v = rec.get(k)
            if isinstance(v, (int, float)):
                tot[k] += v
    tot["target"] = next(iter(cg.values())).get("target", "?")
    return tot


def split_codegen(cg):
    """Separate an optional module-total record (unit '(module)') from the
    per-function records. If no module record was emitted, the total is the
    sum of the per-function records. Returns (total_dict_or_None, {unit: rec})."""
    if not cg:
        return None, {}
    module = cg.get("(module)")
    funcs = {k: v for k, v in cg.items() if k != "(module)"}
    total = module if module else codegen_total(funcs)
    return total, funcs


def esc(s):
    return _html.escape("" if s is None else str(s))

def load_run(path):
    passes, codegen = [], {}
    try:
        fh = open(path)
    except OSError as e:
        sys.exit(f"lpta-view: cannot open {path}: {e.strerror}")
    with fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"lpta-view: {path}:{lineno}: bad JSON ({e}); skipped",
                      file=sys.stderr)
                continue
            if rec.get("stage") == "codegen":
                codegen[rec.get("unit", "(module)")] = rec
            elif "pass" in rec:
                passes.append(rec)
    return passes, codegen


def collapse_remarks(remarks):
    """Dedup identical (missed,msg) remarks into counts, first-seen order."""
    seen, order = {}, []
    for r in remarks or []:
        key = (bool(r.get("missed")), " ".join((r.get("msg") or "").split()))
        if key not in seen:
            seen[key] = 0
            order.append(key)
        seen[key] += 1
    return [{"missed": m, "msg": msg, "count": seen[(m, msg)]} for (m, msg) in order]


def rec_for_view(r):
    return {
        "pass": r.get("pass", "?"), "unit": r.get("unit", "?"),
        "unit_type": r.get("unit_type", "?"),
        "score": r.get("score", 0.0), "rel_score": r.get("rel_score", 0.0),
        "delta": r.get("delta"), "vector_delta": r.get("vector_delta", 0),
        "time_ms": r.get("time_ms", 0.0), "invalidated": r.get("invalidated", False),
        "removed": r.get("removed", []) or [], "added": r.get("added", []) or [],
        "remarks": collapse_remarks(r.get("remarks", [])), "note": r.get("note", ""),
    }


# cross-run aligner + ranking (unchanged backend)

def keyed(passes):
    seen, keys = {}, []
    for r in passes:
        base = (r.get("pass"), r.get("unit"))
        occ = seen.get(base, 0)
        seen[base] = occ + 1
        keys.append((r.get("pass"), r.get("unit"), occ))
    return keys


def lcs_align(a, b):
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            dp[i][j] = (dp[i + 1][j + 1] + 1) if a[i] == b[j] \
                else max(dp[i + 1][j], dp[i][j + 1])
    ops, i, j = [], 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            ops.append(("match", i, j)); i += 1; j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            ops.append(("a_only", i, None)); i += 1
        else:
            ops.append(("b_only", None, j)); j += 1
    while i < n:
        ops.append(("a_only", i, None)); i += 1
    while j < m:
        ops.append(("b_only", None, j)); j += 1
    return ops


def outcome_sig(rec):
    opcodes = tuple(sorted((rec.get("opcodes") or {}).items()))
    n_missed = sum(1 for r in rec.get("remarks", []) if r.get("missed"))
    return (rec.get("delta"), rec.get("vector_delta"), opcodes, n_missed)


def inst_key(ir_line):
    s = ir_line.strip()
    s = re.sub(r'^%\S+\s*=\s*', '', s)
    s = re.sub(r'%[A-Za-z0-9._]+', '%v', s)
    s = re.sub(r',?\s*!\S+\s*!\d+', '', s)
    return ' '.join(s.split())


def only_in(primary, other):
    pk = {}
    for line in primary:
        pk.setdefault(inst_key(line), line)
    ok = {inst_key(line) for line in other}
    uniq = [ln for k, ln in pk.items() if k not in ok]
    return uniq, len(set(pk) & ok)


def divergence_score(ra, rb):
    s = 0.0
    va, vb = ra.get("vector_delta", 0) or 0, rb.get("vector_delta", 0) or 0
    if (va != 0) != (vb != 0):
        s += 1000.0
    elif va != vb:
        s += 100.0
    ma = sum(1 for r in ra.get("remarks", []) if r.get("missed"))
    mb = sum(1 for r in rb.get("remarks", []) if r.get("missed"))
    if (ma > 0) != (mb > 0):
        s += 500.0
    s += abs(ma - mb) * 25.0
    sa, sb = ra.get("score") or 0.0, rb.get("score") or 0.0
    s += abs(sa - sb) / max(abs(sa), abs(sb), 1.0) * 20.0
    da, db = ra.get("delta") or 0, rb.get("delta") or 0
    s += abs(da - db) / max(abs(da), abs(db), 1) * 10.0
    return s


def divergence_reason(ra, rb):
    va, vb = ra.get("vector_delta", 0) or 0, rb.get("vector_delta", 0) or 0
    if (va != 0) != (vb != 0):
        return "vectorization lost" if (va != 0 and vb == 0) else "vectorization gained"
    if va != vb:
        return "vectorization width changed"
    ma = sum(1 for r in ra.get("remarks", []) if r.get("missed"))
    mb = sum(1 for r in rb.get("remarks", []) if r.get("missed"))
    if (ma > 0) != (mb > 0):
        return "a pass began refusing" if mb > ma else "a pass stopped refusing"
    if abs((ra.get("score") or 0) - (rb.get("score") or 0)) > 1:
        return "significance diverged"
    return "instruction count diverged"


SIG_THRESHOLD = 50.0

_CALL_PREFIX = {"tail", "musttail", "notail"}


def opcode_of(line):
    toks = line.strip().split()
    if len(toks) >= 2 and toks[1] == "=":
        toks = toks[2:]
    while toks and toks[0] in _CALL_PREFIX:
        toks = toks[1:]
    return toks[0] if toks else "?"


def classify_diff(removed, added):
    rem_ops = Counter(opcode_of(l) for l in removed)
    add_ops = Counter(opcode_of(l) for l in added)
    rk = Counter(inst_key(l) for l in removed)
    ak = Counter(inst_key(l) for l in added)
    churn = sum(min(rk[k], ak[k]) for k in (rk.keys() & ak.keys()))
    return {"rem_total": len(removed), "add_total": len(added),
            "rem_ops": rem_ops.most_common(), "add_ops": add_ops.most_common(),
            "churn": churn}


#data builders 

def build_single_data(path):
    passes, codegen = load_run(path)
    recs = [rec_for_view(r) for r in passes]
    changed = sum(1 for r in recs if r["removed"] or r["added"] or r["remarks"])
    total_ms = sum(r["time_ms"] for r in recs)
    return {"mode": "single",
            "meta": {"file": os.path.basename(path), "recorded": len(recs),
                     "changed": changed, "total_ms": total_ms},
            "codegen": codegen,
            "records": recs}


def build_cross_data(path_a, path_b):
    pa, cga = load_run(path_a)
    pb, cgb = load_run(path_b)
    ops = lcs_align(keyed(pa), keyed(pb))

    diverged, only_a, only_b = [], [], []
    for kind, ia, ib in ops:
        if kind == "match":
            ra, rb = pa[ia], pb[ib]
            if outcome_sig(ra) != outcome_sig(rb):
                diverged.append((ra, rb, ia))
        elif kind == "a_only":
            only_a.append(pa[ia])
        else:
            only_b.append(pb[ib])

    diverged.sort(key=lambda p: divergence_score(p[0], p[1]), reverse=True)
    significant = [p for p in diverged if divergence_score(p[0], p[1]) >= SIG_THRESHOLD]

    na = max(len(pa), 1)
    strip = [{"frac": (p[2] / na), "score": divergence_score(p[0], p[1]),
              "reason": divergence_reason(p[0], p[1]),
              "pass": p[0].get("pass"), "unit": p[0].get("unit")}
             for p in significant]

    root, others = None, []
    if diverged:
        ra, rb, _ = diverged[0]
        a_add_only, add_sh = only_in(ra.get("added", []) or [], rb.get("added", []) or [])
        b_add_only, _ = only_in(rb.get("added", []) or [], ra.get("added", []) or [])
        a_rem_only, rem_sh = only_in(ra.get("removed", []) or [], rb.get("removed", []) or [])
        b_rem_only, _ = only_in(rb.get("removed", []) or [], ra.get("removed", []) or [])

        def profile(rec):
            vec = sum(1 for r in rec.get("remarks", [])
                      if not r.get("missed") and "vectorized loop" in (r.get("msg") or ""))
            missed = sum(1 for r in rec.get("remarks", []) if r.get("missed"))
            return vec, missed
        va_vec, va_miss = profile(ra)
        vb_vec, vb_miss = profile(rb)
        dvec, dmiss = vb_vec - va_vec, vb_miss - va_miss
        a_msgs = {" ".join((r.get("msg") or "").split()) for r in ra.get("remarks", [])}
        new_in_b = []
        for r in rb.get("remarks", []):
            m = " ".join((r.get("msg") or "").split())
            if m and m not in a_msgs and m not in new_in_b:
                new_in_b.append(m)
        why_bits = []
        if dvec < 0:
            why_bits.append(f"{-dvec} fewer loops vectorized")
        elif dvec > 0:
            why_bits.append(f"{dvec} more loops vectorized")
        if dmiss > 0:
            why_bits.append(f"{dmiss} more refused")
        why = (", ".join(why_bits) + " in B") if why_bits else ""

        root = {
            "pass": ra.get("pass"), "unit": ra.get("unit"),
            "reason": divergence_reason(ra, rb),
            "a": {"delta": ra.get("delta"), "vector_delta": ra.get("vector_delta", 0),
                  "remarks": collapse_remarks(ra.get("remarks", []))},
            "b": {"delta": rb.get("delta"), "vector_delta": rb.get("vector_delta", 0),
                  "remarks": collapse_remarks(rb.get("remarks", []))},
            "why": why, "why_new": new_in_b[:4],
            "a_add_only": a_add_only, "b_add_only": b_add_only,
            "a_rem_only": a_rem_only, "b_rem_only": b_rem_only,
            "both": add_sh + rem_sh,
        }
        for ra2, rb2, _ in significant[1:13]:
            others.append({"pass": ra2.get("pass"), "unit": ra2.get("unit"),
                           "reason": divergence_reason(ra2, rb2)})

    other_count = (len(diverged) - len(significant)) + len(only_a) + len(only_b)
    return {"mode": "cross",
            "meta": {"file_a": os.path.basename(path_a), "file_b": os.path.basename(path_b),
                     "a_count": len(pa), "b_count": len(pb),
                     "diverged": len(diverged), "significant": len(significant),
                     "only_a": len(only_a), "only_b": len(only_b),
                     "other": other_count},
            "codegen_a": cga, "codegen_b": cgb,
            "strip": strip, "series_a": pa, "series_b": pb,
            "root": root, "others": others}


# ---- HTML rendering ---------------------------------------------------------

CSS = r"""
:root{--bg:#0d1117;--panel:#161b22;--panel2:#0f141b;--line:#30363d;--hair:#21262d;
--text:#c9d1d9;--muted:#8b949e;--dim:#6e7681;--accent:#58a6ff;--green:#3fb950;
--red:#f85149;--amber:#d29922;--purple:#bc8cff;
--mono:ui-monospace,"JetBrains Mono","SF Mono",Menlo,Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",sans-serif}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 var(--sans);
-webkit-font-smoothing:antialiased}
.hd{padding:14px 24px;border-bottom:1px solid var(--line);display:flex;
justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap;
position:sticky;top:0;background:rgba(13,17,23,.92);backdrop-filter:blur(6px);z-index:5}
.hd .brand{font-weight:700;font-size:16px;letter-spacing:-.2px}
.hd .brand b{color:var(--accent)}
.hd .m{font-family:var(--mono);font-size:12px;color:var(--muted)}
.hd .m b{color:var(--text)}
.wrap{max-width:1040px;margin:0 auto;padding:24px 24px 80px}
h2{font-size:11px;text-transform:uppercase;letter-spacing:1.3px;color:var(--dim);
margin:34px 0 12px;font-weight:600;display:flex;align-items:baseline;gap:10px}
h2 .sub{text-transform:none;letter-spacing:0;color:var(--dim);font-weight:400;font-size:11px}
.lede{color:var(--muted);font-size:12.5px;margin:-4px 0 14px;max-width:70ch}
.stats{display:flex;flex-wrap:wrap;gap:30px;font-family:var(--mono)}
.stats .s{font-size:11px;color:var(--muted);letter-spacing:.3px}
.stats .s b{font-size:23px;display:block;font-weight:600;color:var(--text);
letter-spacing:-.5px;line-height:1.25}
.stats .s.g b{color:var(--green)}.stats .s.r b{color:var(--red)}.stats .s.a b{color:var(--amber)}
.chart{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
padding:16px 16px 10px;margin-top:4px}
.chart svg{display:block;width:100%;height:auto}
.tl-zero{stroke:var(--dim);stroke-width:1;stroke-dasharray:3 3;opacity:.7}
.tl-grid{stroke:var(--hair);stroke-width:1}
.tl-line{fill:none;stroke:var(--accent);stroke-width:1.6;
stroke-linejoin:round;stroke-linecap:round}
.tl-line.b{stroke:var(--amber)}
.tl-guide{stroke:var(--line);stroke-width:1;stroke-dasharray:2 3}
.tl-dot{fill:var(--accent);stroke:var(--bg);stroke-width:1.5}
.tl-dot:hover{r:5}
.vec-line{fill:none;stroke:var(--green);stroke-width:1.6;stroke-linejoin:round}
.vec-dot{fill:var(--green);stroke:var(--bg);stroke-width:1.4}
.stem{stroke-width:2.4;stroke-linecap:round}
.stem.vec{stroke:var(--red)}.stem.ref{stroke:var(--amber)}.stem.oth{stroke:var(--dim)}
.tl-lbl{fill:var(--muted);font:600 10px var(--mono)}
.tl-ax{fill:var(--dim);font:10px var(--mono)}
.tl-cap{fill:var(--dim);font:600 9.5px var(--mono);text-transform:uppercase;letter-spacing:.6px}
.leg{display:flex;gap:16px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;
color:var(--muted);margin-top:8px;padding-left:2px}
.leg i{display:inline-block;width:10px;height:2px;vertical-align:middle;margin-right:6px}
.leg .dot{width:8px;height:8px;border-radius:50%}
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:2px 0 10px}
.filters input[type=text]{background:var(--panel);border:1px solid var(--line);
color:var(--text);font:12px var(--mono);padding:6px 10px;border-radius:6px;
width:280px;max-width:60vw}
.filters input[type=text]::placeholder{color:var(--dim)}
.filters input[type=text]:focus{outline:none;border-color:var(--accent)}
.filters input[type=checkbox]{position:absolute;opacity:0;width:0;height:0}
.filters label{font-family:var(--mono);font-size:11.5px;color:var(--muted);
border:1px solid var(--line);border-radius:6px;padding:6px 11px;cursor:pointer;
user-select:none;transition:.12s}
.filters label:hover{border-color:var(--dim);color:var(--text)}
#f-changed:checked+label{border-color:var(--accent);color:var(--accent);background:#132033}
#f-vec:checked+label{border-color:var(--green);color:var(--green);background:#0f2318}
#f-rmk:checked+label{border-color:var(--amber);color:var(--amber);background:#241d0d}
.count{font-family:var(--mono);font-size:11px;color:var(--dim);margin-left:auto}
body:has(#f-changed:checked) tr.row:not(.is-changed){display:none}
body:has(#f-changed:checked) details.det:not(.is-changed){display:none}
body:has(#f-vec:checked) tr.row:not(.has-vec){display:none}
body:has(#f-vec:checked) details.det:not(.has-vec){display:none}
body:has(#f-rmk:checked) tr.row:not(.has-rmk){display:none}
body:has(#f-rmk:checked) details.det:not(.has-rmk){display:none}
.hidden{display:none !important}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
th{text-align:right;color:var(--muted);font-weight:500;text-transform:uppercase;
font-size:10px;letter-spacing:.5px;padding:7px 10px;border-bottom:1px solid var(--line);
position:sticky;top:52px;background:var(--bg)}
th.l,td.l{text-align:left}
td{text-align:right;padding:5px 10px;border-bottom:1px solid var(--hair)}
tr:hover td{background:var(--panel)}
.pos{color:var(--green)}.neg{color:var(--red)}.zero{color:var(--dim)}
.bar{display:inline-block;height:7px;border-radius:2px;vertical-align:middle;
background:var(--accent);opacity:.35}
td .pname{color:var(--text)}
.chip{font-size:9px;padding:1px 5px;border-radius:4px;margin-left:6px;
font-weight:600;letter-spacing:.3px;vertical-align:middle}
.chip.v{color:var(--green);background:#0f2318;border:1px solid #14361f}
.chip.m{color:var(--amber);background:#241d0d;border:1px solid #3a2f14}
details{background:var(--panel);border:1px solid var(--line);border-radius:6px;margin:7px 0}
summary{padding:9px 14px;cursor:pointer;font-family:var(--mono);font-size:12px;
list-style:none;color:var(--text)}
summary::-webkit-details-marker{display:none}
summary::before{content:"\25b8  ";color:var(--dim)}
details[open]>summary::before{content:"\25be  "}
summary .u{color:var(--dim)}
.body{padding:11px 14px 13px;border-top:1px solid var(--line)}
.cls{font-family:var(--mono);font-size:12px;line-height:1.85;margin-bottom:2px}
.cls .crow{padding:1px 0}
.cls .rem{color:var(--red);font-weight:600}.cls .add{color:var(--green);font-weight:600}
details.raw{background:var(--panel2);margin:8px 0 0}
details.raw>summary{padding:6px 12px;font-size:11px;color:var(--muted)}
details.raw .body{padding:8px 12px}
.diff{font-family:var(--mono);font-size:12px;white-space:pre;overflow-x:auto;line-height:1.5}
.diff div{padding:0 4px}
.diff .rem{color:#ff9b95}.diff .add{color:#7ee787}
.rmk{font-family:var(--mono);font-size:12px;line-height:1.75;margin-bottom:8px}
.rmk .miss{color:var(--red);font-weight:600}.rmk .ok{color:var(--accent);font-weight:600}
.rmk .c{color:var(--dim)}
.rmk .new{background:#241d0d;border-left:2px solid var(--amber);padding-left:6px;
margin-left:-8px;display:inline-block}
.rmk .same{opacity:.5}
.root{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--red);
border-radius:6px;padding:16px 18px}
.root .p{font-family:var(--mono);font-size:15px;font-weight:600;letter-spacing:-.2px}
.root .p .u{color:var(--dim)}.root .p .r{color:var(--amber)}
.ab{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:13px}
.ab .c{background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:11px 13px;
font-family:var(--mono);font-size:12px}
.ab .c .h{color:var(--muted);text-transform:uppercase;font-size:10px;letter-spacing:.5px;
margin-bottom:8px;display:flex;justify-content:space-between}
.ab .c.a .h b{color:var(--accent)}.ab .c.b .h b{color:var(--amber)}
.why{margin-top:13px;padding:11px 13px;background:#241d0d;border:1px solid #3a2f14;
border-radius:5px;font-family:var(--mono);font-size:12px;color:#f0d68a}
.why b{color:var(--amber)}.why .n{color:var(--dim)}
.olist{font-family:var(--mono);font-size:12px}
.olist div{padding:3px 0;color:var(--muted)}.olist .r{color:var(--amber)}
.olist .u{color:var(--dim)}
.qbox{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:13px 15px}
.note{color:var(--dim);font-family:var(--mono);font-size:12px;margin-top:16px;line-height:1.6}
.note code{background:var(--panel);padding:1px 5px;border-radius:4px;color:var(--muted)}
.ok-msg{font-family:var(--mono);color:var(--green)}
.pflist{font-family:var(--mono);font-size:12px;margin:2px 0}
.pf{display:flex;gap:14px;align-items:baseline;padding:5px 10px;border-bottom:1px solid var(--hair)}
.pf .fn{color:var(--text);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pf .nums{color:var(--muted)}
.pf.lost .nums,.pf .lost{color:var(--red);font-weight:600}
.pf.gain .nums,.pf .gain{color:var(--green);font-weight:600}
.pf .held{color:var(--dim)}
.pf .miss{color:var(--dim);font-style:italic}
@media(max-width:640px){.ab{grid-template-columns:1fr}.stats{gap:20px}}
"""


def h_remarks(remarks, limit=8, new_msgs=None):
    if not remarks:
        return ""
    new_msgs = new_msgs or set()
    nvec = sum(r["count"] for r in remarks
               if not r["missed"] and "vectorized loop" in r["msg"])
    nmiss = sum(r["count"] for r in remarks if r["missed"])
    if new_msgs:
        remarks = sorted(remarks, key=lambda r: (r["msg"] not in new_msgs))
    out = ['<div class="rmk">']
    if nvec or nmiss:
        bits = []
        if nvec:
            bits.append(f'<span class="pos">{nvec} vectorized</span>')
        if nmiss:
            bits.append(f'<span class="neg">{nmiss} missed</span>')
        out.append(" &middot; ".join(bits) + "<br>")
    shown = remarks[:limit]
    for r in shown:
        tag = '<span class="miss">MISSED</span>' if r["missed"] else '<span class="ok">remark</span>'
        c = f' <span class="c">(x{r["count"]})</span>' if r["count"] > 1 else ""
        cls = "new" if r["msg"] in new_msgs else ("same" if new_msgs else "")
        line = f'{tag}: {esc(r["msg"])}{c}'
        out.append(f'<span class="{cls}">{line}</span><br>' if cls else f'{line}<br>')
    if len(remarks) > limit:
        out.append(f'<span class="c">... {len(remarks) - limit} more distinct</span>')
    out.append('</div>')
    return "".join(out)


def h_diff(removed, added, cap=60):
    if not removed and not added:
        return '<div class="diff"><div class="zero">(no instruction-level change captured)</div></div>'
    out = ['<div class="diff">']
    for l in removed[:cap]:
        out.append(f'<div class="rem">- {esc(l.strip())}</div>')
    if len(removed) > cap:
        out.append(f'<div class="zero">  ... {len(removed) - cap} more removed</div>')
    for l in added[:cap]:
        out.append(f'<div class="add">+ {esc(l.strip())}</div>')
    if len(added) > cap:
        out.append(f'<div class="zero">  ... {len(added) - cap} more added</div>')
    out.append('</div>')
    return "".join(out)


def h_class(removed, added):
    if not removed and not added:
        return '<div class="diff"><span class="zero">(no instruction-level change captured)</span></div>'
    c = classify_diff(removed, added)

    def ops_str(ops, cap=7):
        parts = [f'{n}&times;{esc(op)}' for op, n in ops[:cap]]
        if len(ops) > cap:
            parts.append(f'+{len(ops) - cap} more')
        return ' &middot; '.join(parts)

    out = ['<div class="cls">']
    if c["rem_total"]:
        out.append(f'<div class="crow"><span class="rem">&minus;{c["rem_total"]}</span> removed '
                   f'&nbsp;<span class="zero">{ops_str(c["rem_ops"])}</span></div>')
    if c["add_total"]:
        out.append(f'<div class="crow"><span class="add">+{c["add_total"]}</span> added '
                   f'&nbsp;<span class="zero">{ops_str(c["add_ops"])}</span></div>')
    if c["churn"]:
        out.append(f'<div class="crow zero">{c["churn"]} appear on both sides '
                   f'&mdash; same instruction renamed / renumbered, not real work</div>')
    out.append('</div>')
    out.append('<details class="raw"><summary>raw IR</summary><div class="body">')
    out.append(h_diff(removed, added))
    out.append('</div></details>')
    return "".join(out)


def _dfmt(d):
    if d is None:
        return "n/a", "zero"
    return (f"+{d}" if d > 0 else str(d)), ("zero" if d == 0 else ("pos" if d > 0 else "neg"))


def _vfmt(vd):
    if not vd:
        return "0", "zero"
    return (f"+{vd}" if vd > 0 else str(vd)), "pos"


def short_pass(name):
    n = name or "?"
    return n[:-4] if n.endswith("Pass") else n


def h_perfunc_vec(name, rec_a, rec_b):
    """One cross-run per-function row: vector A -> B with a directional verdict.
    rec_a / rec_b are that function's codegen record in each run, or None if the
    function has no object symbol (inlined away)."""
    if rec_a is None and rec_b is None:
        return (f'<div class="pf"><span class="fn">{esc(name)}</span>'
                f'<span class="miss">&mdash; inlined, no codegen symbol</span></div>')
    va = rec_a.get("vector_instrs", 0) if rec_a else 0
    vb = rec_b.get("vector_instrs", 0) if rec_b else 0
    if va == vb:
        verdict, cls = '<span class="held">&mdash; held</span>', "held"
    elif vb < va:
        verdict, cls = f'<span class="lost">&larr; lost {va - vb}</span>', "lost"
    else:
        verdict, cls = f'<span class="gain">&larr; gained {vb - va}</span>', "gain"
    return (f'<div class="pf {cls}"><span class="fn">{esc(name)}</span>'
            f'<span class="nums">vector {va} &rarr; {vb}</span>{verdict}</div>')


#  SVG: the transformation timeline 

def timeline_svg(records):
    n = len(records)
    if n < 2:
        return ""
    cd, cv, acc, accv = [], [], 0, 0
    for r in records:
        acc += (r.get("delta") or 0)
        accv += (r.get("vector_delta") or 0)
        cd.append(acc); cv.append(accv)

    W, PX0, PX1 = 960, 62, 944
    plotW = PX1 - PX0
    PY0, PY1 = 22, 210
    VY0, VY1 = 250, 306
    H = 322
    lo = min(min(cd), 0); hi = max(max(cd), 0)
    if hi == lo:
        hi = lo + 1
    vlo = min(min(cv), 0); vhi = max(max(cv), 0)
    if vhi == vlo:
        vhi = vlo + 1

    def X(i):
        return PX0 + (i / (n - 1)) * plotW

    def Y(v):
        return PY1 - (v - lo) / (hi - lo) * (PY1 - PY0)

    def VY(v):
        return VY1 - (v - vlo) / (vhi - vlo) * (VY1 - VY0)

    def path(xs, ys):
        return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))

    xs = [X(i) for i in range(n)]
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" '
         f'aria-label="Cumulative IR change across {n} passes">']
    zy = Y(0)
    s.append(f'<text class="tl-cap" x="{PX0}" y="14">cumulative &#916; instructions '
             f'(net, from first pass)</text>')
    s.append(f'<line class="tl-grid" x1="{PX0}" y1="{PY0}" x2="{PX1}" y2="{PY0}"/>')
    s.append(f'<line class="tl-zero" x1="{PX0}" y1="{zy:.1f}" x2="{PX1}" y2="{zy:.1f}"/>')
    s.append(f'<text class="tl-ax" x="{PX0-6}" y="{PY0+4}" text-anchor="end">+{hi:g}</text>')
    s.append(f'<text class="tl-ax" x="{PX0-6}" y="{zy+3:.1f}" text-anchor="end">0</text>')
    if lo < 0:
        s.append(f'<text class="tl-ax" x="{PX0-6}" y="{PY1+3}" text-anchor="end">{lo:g}</text>')
    s.append(f'<path class="tl-line" d="{path(xs, [Y(v) for v in cd])}"/>')
    idx = sorted(range(n), key=lambda i: abs(records[i].get("delta") or 0), reverse=True)
    movers = [i for i in idx if (records[i].get("delta") or 0) != 0][:6]
    movers.sort()
    last_lx = -999
    for i in movers:
        x, y = X(i), Y(cd[i])
        r = records[i]
        d = r.get("delta") or 0
        s.append(f'<line class="tl-guide" x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{zy:.1f}"/>')
        s.append(f'<g><title>{esc(r.get("pass"))} &#183; {esc(r.get("unit"))}  '
                 f'&#916;{d:+d}  &#183; pass #{i}</title>'
                 f'<circle class="tl-dot" cx="{x:.1f}" cy="{y:.1f}" r="3.4"/></g>')
        if x - last_lx > 92:
            above = y > PY0 + 26
            ly = (y - 9) if above else (y + 15)
            anchor = "middle"
            if x < PX0 + 40:
                anchor = "start"
            elif x > PX1 - 40:
                anchor = "end"
            s.append(f'<text class="tl-lbl" x="{x:.1f}" y="{ly:.1f}" '
                     f'text-anchor="{anchor}">{esc(short_pass(r.get("pass")))}</text>')
            last_lx = x
    s.append(f'<text class="tl-cap" x="{PX0}" y="{VY0-8}">IR vector ops (cumulative)</text>')
    s.append(f'<line class="tl-grid" x1="{PX0}" y1="{VY1}" x2="{PX1}" y2="{VY1}"/>')
    s.append(f'<path class="vec-line" d="{path(xs, [VY(v) for v in cv])}"/>')
    s.append(f'<text class="tl-ax" x="{PX0-6}" y="{VY0+4}" text-anchor="end">{vhi:g}</text>')
    for i in range(n):
        if (records[i].get("vector_delta") or 0) != 0:
            r = records[i]
            s.append(f'<g><title>{esc(r.get("pass"))} &#183; {esc(r.get("unit"))}  '
                     f'vec {r.get("vector_delta"):+d} &#183; pass #{i}</title>'
                     f'<circle class="vec-dot" cx="{X(i):.1f}" cy="{VY(cv[i]):.1f}" r="3"/></g>')
    s.append(f'<text class="tl-ax" x="{PX0}" y="{H-3}">pass 0</text>')
    s.append(f'<text class="tl-ax" x="{PX1}" y="{H-3}" text-anchor="end">pass {n-1}</text>')
    s.append('</svg>')
    return "".join(s)


def cross_strip_svg(strip, series_a, series_b):
    if not strip:
        return ""
    W, PX0, PX1 = 960, 20, 944
    plotW = PX1 - PX0
    BASE, TOP, H = 118, 20, 140
    mx = max(x["score"] for x in strip)
    sc = math.log10(mx + 1) or 1

    def X(f):
        return PX0 + f * plotW

    def hgt(v):
        return (math.log10(v + 1) / sc) * (BASE - TOP)

    cls = {"vectorization lost": "vec", "vectorization gained": "vec",
           "vectorization width changed": "vec",
           "a pass began refusing": "ref", "a pass stopped refusing": "ref"}
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="divergence positions">']
    s.append(f'<text class="tl-cap" x="{PX0}" y="12">significant divergences along the pipeline</text>')
    s.append(f'<line class="tl-grid" x1="{PX0}" y1="{BASE}" x2="{PX1}" y2="{BASE}"/>')
    tallest = max(range(len(strip)), key=lambda k: strip[k]["score"])
    for k, d in enumerate(strip):
        x = X(d["frac"])
        y = BASE - hgt(d["score"])
        c = cls.get(d["reason"], "oth")
        s.append(f'<g><title>{esc(d["pass"])} &#183; {esc(d["unit"])} &#8212; '
                 f'{esc(d["reason"])} (score {d["score"]:.0f})</title>'
                 f'<line class="stem {c}" x1="{x:.1f}" y1="{BASE}" x2="{x:.1f}" y2="{y:.1f}"/></g>')
        if k == tallest:
            lx = max(PX0, min(x, PX1 - 150))
            s.append(f'<text class="tl-lbl" x="{lx:.1f}" y="{y-5:.1f}">'
                     f'{esc(short_pass(d["pass"]))}</text>')
    s.append(f'<text class="tl-ax" x="{PX0}" y="{H-4}">start</text>')
    s.append(f'<text class="tl-ax" x="{PX1}" y="{H-4}" text-anchor="end">codegen</text>')
    s.append('</svg>')
    leg = ('<div class="leg">'
           '<span><i class="dot" style="background:var(--red)"></i>vectorization</span>'
           '<span><i class="dot" style="background:var(--amber)"></i>refusal change</span>'
           '<span><i class="dot" style="background:var(--dim)"></i>other</span></div>')
    return "".join(s) + leg


# ---- page sections ----------------------------------------------------------

def filter_bar():
    return (
        '<div class="filters">'
        '<input type="text" id="q" placeholder="filter by pass or function\u2026" '
        'oninput="lptaFilter(this.value)" autocomplete="off">'
        '<input type="checkbox" id="f-changed"><label for="f-changed">changed IR</label>'
        '<input type="checkbox" id="f-vec"><label for="f-vec">vector ops</label>'
        '<input type="checkbox" id="f-rmk"><label for="f-rmk">has remarks</label>'
        '<span class="count" id="count"></span>'
        '</div>'
    )


def _codesize(total):
    cs = total.get("code_size_bytes") if total else None
    if isinstance(cs, (int, float)) and cs > 0:
        return f'{cs/1024:.1f}K'
    return None


def render_single(data):
    recs, cg, meta = data["records"], data["codegen"], data["meta"]
    total, funcs = split_codegen(cg)
    ranked = sorted(recs, key=lambda r: r["score"], reverse=True)
    maxscore = max((r["score"] for r in recs), default=1.0) or 1.0

    P = ['<h2>Run summary</h2>']
    P.append('<div class="stats">')
    P.append(f'<div class="s"><b>{meta["recorded"]}</b>passes recorded</div>')
    P.append(f'<div class="s"><b>{meta["changed"]}</b>changed the IR</div>')
    P.append(f'<div class="s"><b>{meta["total_ms"]:.0f}<span class="zero"> ms</span></b>total pass time</div>')
    if total:
        P.append(f'<div class="s"><b>{total.get("asm_instrs","?")}</b>machine instrs</div>')
        cstxt = _codesize(total)
        if cstxt:
            P.append(f'<div class="s"><b>{cstxt}</b>code size</div>')
        P.append(f'<div class="s g"><b>{total.get("vector_instrs","?")}</b>vector ops</div>')
    P.append('</div>')

    tl = timeline_svg(recs)
    if tl:
        P.append('<h2>Transformation timeline <span class="sub">the pipeline\u2019s shape, pass by pass</span></h2>')
        P.append('<p class="lede">Net instructions added and removed across every recorded pass, '
                 'and where vectorization enters the IR. Labelled points are the biggest movers; '
                 'hover any point for the pass and unit.</p>')
        P.append(f'<div class="chart">{tl}</div>')

    P.append(f'<h2>Passes by significance <span class="sub">{len(recs)} recorded</span></h2>')
    P.append(filter_bar())
    P.append('<table><tr><th class="l">#</th><th>score</th><th>&Delta;</th>'
             '<th>vec</th><th>ms</th><th class="l">pass</th><th class="l">unit</th></tr>')
    for i, r in enumerate(ranked, 1):
        ds, dc = _dfmt(r["delta"])
        vs, vc = _vfmt(r["vector_delta"])
        classes = ["row"]
        if r["removed"] or r["added"] or (r["delta"] not in (None, 0)):
            classes.append("is-changed")
        if r["vector_delta"]:
            classes.append("has-vec")
        if r["remarks"]:
            classes.append("has-rmk")
        ds_txt = " ".join([r["pass"], r["unit"], r["unit_type"]]).lower()
        bw = int(round((r["score"] / maxscore) * 46)) if r["score"] > 0 else 0
        bar = f'<span class="bar" style="width:{bw}px"></span> ' if bw else ''
        P.append(f'<tr class="{" ".join(classes)}" data-s="{esc(ds_txt)}">'
                 f'<td class="l zero">{i}</td>'
                 f'<td>{bar}{r["score"]:.0f}</td>'
                 f'<td class="{dc}">{ds}</td>'
                 f'<td class="{vc}">{vs}</td>'
                 f'<td class="zero">{r["time_ms"]:.2f}</td>'
                 f'<td class="l"><span class="pname">{esc(r["pass"])}</span></td>'
                 f'<td class="l">{esc(r["unit"])} <span class="zero">{esc(r["unit_type"])}</span></td></tr>')
    P.append('</table>')

    detailed = [r for r in ranked if (r["removed"] or r["added"] or r["remarks"])][:60]
    if detailed:
        P.append(f'<h2>Transformation detail <span class="sub">top {len(detailed)} by significance</span></h2>')
        for r in detailed:
            ds, _ = _dfmt(r["delta"])
            vd = r["vector_delta"]
            vpart = f' &middot; vec {vd:+d}' if vd else ''
            classes = ["det"]
            if r["removed"] or r["added"] or (r["delta"] not in (None, 0)):
                classes.append("is-changed")
            if r["vector_delta"]:
                classes.append("has-vec")
            if r["remarks"]:
                classes.append("has-rmk")
            chips = ''
            if r["vector_delta"]:
                chips += '<span class="chip v">VEC</span>'
            if any(x["missed"] for x in r["remarks"]):
                chips += '<span class="chip m">MISSED</span>'
            note = ' <span style="color:var(--amber)">&plusmn;</span>' if r["note"] else ''
            ds_txt = " ".join([r["pass"], r["unit"]]).lower()
            P.append(f'<details class="{" ".join(classes)}" data-s="{esc(ds_txt)}">'
                     f'<summary>{esc(r["pass"])} <span class="u">&middot; {esc(r["unit"])}</span>'
                     f'{chips} &mdash; score {r["score"]:.0f} &middot; {ds}{vpart} '
                     f'&middot; {r["time_ms"]:.2f}ms{note}</summary><div class="body">')
            if r["note"]:
                P.append(f'<div class="note" style="color:var(--amber);margin:0 0 9px">'
                         f'diff note: {esc(r["note"])}</div>')
            P.append(h_remarks(r["remarks"]))
            P.append(h_class(r["removed"], r["added"]))
            P.append('</div></details>')

    if total:
        P.append('<h2>Codegen terminus <span class="sub">final backend output</span></h2>')
        P.append('<div class="stats" style="border-left:3px solid var(--green);padding-left:16px">')
        P.append(f'<div class="s"><b>{esc(total.get("target","?"))}</b>target</div>')
        P.append(f'<div class="s"><b>{total.get("asm_instrs","?")}</b>instrs</div>')
        cstxt = _codesize(total)
        if cstxt:
            P.append(f'<div class="s"><b>{cstxt}</b>code size</div>')
        P.append(f'<div class="s"><b>{total.get("branches","?")}</b>branches</div>')
        P.append(f'<div class="s g"><b>{total.get("vector_instrs","?")}</b>vector ops</div>')
        sc = total.get("scalar_sse_instrs")
        if isinstance(sc, (int, float)) and sc > 0:
            P.append(f'<div class="s"><b>{sc}</b>scalar SSE</div>')
        P.append('</div>')
        P.append('<p class="note">Codegen is measured once after the whole backend, '
                 'not attributed to individual machine passes. Per-function totals below.</p>')

        # per-function vector ranking (single run: no A->B, so rank by where SIMD lives)
        ranked_fns = sorted((r for r in funcs.values() if r.get("vector_instrs", 0) > 0),
                            key=lambda r: r.get("vector_instrs", 0), reverse=True)
        if ranked_fns:
            P.append('<h2>Vector ops by function <span class="sub">where the SIMD lives</span></h2>')
            P.append('<div class="pflist">')
            for r in ranked_fns[:8]:
                P.append(f'<div class="pf"><span class="fn">{esc(r.get("unit","?"))}</span>'
                         f'<span class="nums">vector {r.get("vector_instrs",0)} '
                         f'<span class="zero">&middot; {r.get("asm_instrs",0)} instrs</span></span></div>')
            P.append('</div>')
            if len(ranked_fns) > 8:
                P.append(f'<details class="raw"><summary>+ {len(ranked_fns)-8} more functions with vector ops</summary><div class="body"><div class="pflist">')
                for r in ranked_fns[8:]:
                    P.append(f'<div class="pf"><span class="fn">{esc(r.get("unit","?"))}</span>'
                             f'<span class="nums">vector {r.get("vector_instrs",0)}</span></div>')
                P.append('</div></div></details>')
    return "".join(P)


def render_cross(data):
    meta, root, others = data["meta"], data["root"], data["others"]
    cga, cgb = data["codegen_a"], data["codegen_b"]
    ta, funcs_a = split_codegen(cga)
    tb, funcs_b = split_codegen(cgb)

    P = ['<h2>Divergence summary</h2><div class="stats">']
    P.append(f'<div class="s a"><b>{meta["a_count"]}</b>passes in A</div>')
    P.append(f'<div class="s"><b>{meta["b_count"]}</b>passes in B</div>')
    P.append(f'<div class="s r"><b>{meta["significant"]} / {meta["diverged"]}</b>significant / differ</div>')
    P.append(f'<div class="s"><b>{meta["only_a"]}</b>only in A</div>')
    P.append(f'<div class="s"><b>{meta["only_b"]}</b>only in B</div>')
    P.append('</div>')

    strip = cross_strip_svg(data.get("strip"), data.get("series_a"), data.get("series_b"))
    if strip:
        P.append('<h2>Where they diverge <span class="sub">significance along the pipeline</span></h2>')
        P.append(f'<div class="chart">{strip}</div>')

    if not root:
        P.append('<h2>Result</h2><p class="ok-msg">'
                 'Pipelines agree on every shared pass &mdash; no behavioral divergence.</p>')
    else:
        r = root
        new_set = set(r.get("why_new") or [])
        rr = f' <span class="r">({esc(r["reason"])})</span>' if r.get("reason") else ''
        P.append('<h2>Root divergence <span class="sub">ranked by significance, not pipeline position</span></h2>')
        P.append('<div class="root">')
        P.append(f'<div class="p">&#9654; {esc(r["pass"])} <span class="u">&middot; {esc(r["unit"])}</span>{rr}</div>')
        P.append('<div class="ab">')
        for lab, side, o in (("A", "a", r["a"]), ("B", "b", r["b"])):
            ds, _ = _dfmt(o["delta"])
            vs, _ = _vfmt(o["vector_delta"])
            nm = new_set if side == "b" else None
            P.append(f'<div class="c {side}"><div class="h"><b>Run {lab}</b>'
                     f'<span>&#916; {ds} &middot; vec {vs}</span></div>'
                     f'{h_remarks(o["remarks"], limit=4, new_msgs=nm)}</div>')
        P.append('</div>')
        if r.get("why") or r.get("why_new"):
            P.append('<div class="why">')
            if r.get("why"):
                P.append(f'<b>Why:</b> {esc(r["why"])}')
            if r.get("why_new"):
                P.append('<br><span class="n">new refusals in B:</span>')
                for m in r["why_new"]:
                    P.append(f'<br>&nbsp;&nbsp;&bull; {esc(m)}')
            P.append('</div>')
        P.append('</div>')

        if others:
            P.append('<h2>Other significant divergences</h2><div class="olist">')
            for o in others:
                P.append(f'<div>&bull; {esc(o["pass"])} <span class="u">&middot; {esc(o["unit"])}</span> '
                         f'<span class="r">({esc(o["reason"])})</span></div>')
            P.append('</div>')

        P.append('<h2>What each run uniquely did <span class="sub">root pass, operand-aware</span></h2>')
        P.append('<div class="qbox">')
        any_side = False
        for lab, add_ln, rem_ln in (("Only in A", r["a_add_only"], r["a_rem_only"]),
                                     ("Only in B", r["b_add_only"], r["b_rem_only"])):
            if add_ln or rem_ln:
                any_side = True
                P.append(f'<div class="cls" style="margin-top:4px"><b class="zero">{lab}</b></div>')
                P.append(h_class(rem_ln, add_ln))
        if not any_side:
            P.append('<div class="cls zero">Both runs made the same structural edits here; '
                     'the divergence is in vectorization / remarks above, not instruction shape.</div>')
        if r["both"]:
            P.append(f'<div class="cls zero" style="margin-top:6px">'
                     f'{r["both"]} instructions identical in both &mdash; not shown.</div>')
        P.append('</div>')

    # ---- machine code: module headline + per-function join ----
    if ta and tb:
        P.append('<h2>Machine code <span class="sub">A &rarr; B module total, measured after the backend</span></h2><table>')
        rows = [("instructions", "asm_instrs", False),
                ("branches", "branches", False),
                ("vector ops", "vector_instrs", True)]
        if _codesize(ta) or _codesize(tb):
            rows.insert(1, ("code size (bytes)", "code_size_bytes", False))
        for lab, key, hl in rows:
            av, bv = ta.get(key, 0), tb.get(key, 0)
            num = isinstance(av, (int, float)) and isinstance(bv, (int, float))
            lost = hl and num and bv < av
            gain = hl and num and bv > av
            if lost:
                bcell = f'<span class="neg">{bv} &larr; {av-bv} lost</span>'
            elif gain:
                bcell = f'<span class="pos">{bv} &larr; {bv-av} gained</span>'
            else:
                delta = f' <span class="zero">({bv-av:+d})</span>' if num else ''
                bcell = f'{esc(bv)}{delta}'
            P.append(f'<tr><td class="l">{lab}</td><td class="zero">A</td><td>{esc(av)}</td>'
                     f'<td class="zero">&rarr; B</td><td>{bcell}</td></tr>')
        P.append('</table>')

        # per-function join, only over the functions the IR analysis flagged
        names = []
        if root:
            names.append(root["unit"])
        names += [o["unit"] for o in others]
        seen = set()
        names = [n for n in names if not (n in seen or seen.add(n))]
        if names:
            P.append('<h2>Per-function vector ops <span class="sub">diverging functions, A &rarr; B</span></h2>')
            P.append('<div class="pflist">')
            for n in names:
                P.append(h_perfunc_vec(n, funcs_a.get(n), funcs_b.get(n)))
            P.append('</div>')

    if meta.get("other"):
        P.append(f'<div class="note">{meta["other"]} other records differ '
                 f'(incidental count / score drift or downstream of the root &mdash; not ranked). '
                 f'Use the CLI <code>diff</code> for the exhaustive list.</div>')
    return "".join(P)


FILTER_JS = r"""
<script>
function lptaFilter(q){
  q=(q||'').trim().toLowerCase();
  var rows=document.querySelectorAll('[data-s]'),shown=0,tot=0;
  rows.forEach(function(el){
    var isRow=el.classList.contains('row');
    if(isRow)tot++;
    var hit=!q||el.getAttribute('data-s').indexOf(q)>-1;
    el.classList.toggle('hidden',!hit);
    if(isRow&&hit)shown++;
  });
  var c=document.getElementById('count');
  if(c)c.textContent=q?(shown+' / '+tot+' passes'):'';
}
</script>
"""


def build_page(data):
    if data["mode"] == "single":
        title = f'LPTA — {data["meta"]["file"]}'
        brand = 'Pass Transformation Analyzer'
        meta = f'<b>{esc(data["meta"]["file"])}</b> &middot; {data["meta"]["recorded"]} passes'
        body = render_single(data)
    else:
        title = f'LPTA cross-run — {data["meta"]["file_a"]} vs {data["meta"]["file_b"]}'
        brand = 'Cross-Run Divergence'
        meta = f'<b>{esc(data["meta"]["file_a"])}</b> vs <b>{esc(data["meta"]["file_b"])}</b>'
        body = render_cross(data)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
        f"<div class='hd'><div class='brand'><b>LPTA</b> {brand}</div>"
        f"<div class='m'>{meta}</div></div>"
        f"<div class='wrap'>{body}</div>{FILTER_JS}</body></html>"
    )


def serve(html):
    d = tempfile.mkdtemp(prefix="lpta-view-")
    with open(os.path.join(d, "index.html"), "w") as f:
        f.write(html)
    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=d, **k)

        def log_message(self, *a):
            pass

    url = f"http://localhost:{port}/index.html"
    print(f"\n  LPTA report ready at  \033[36m{url}\033[0m")
    try:
        ans = input("  Open in browser? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"
    if ans in ("", "y", "yes"):
        webbrowser.open(url)
    print("  Serving — press Ctrl-C to stop.")
    httpd = socketserver.TCPServer(("", port), Quiet)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    write_only = "--html" in sys.argv[1:]
    if len(args) == 1:
        data = build_single_data(args[0])
    elif len(args) == 2:
        data = build_cross_data(args[0], args[1])
    else:
        sys.exit("usage: lpta_view.py FILE  |  lpta_view.py FILE_A FILE_B  [--html]")
    html = build_page(data)
    if write_only:
        with open("lpta_view_out.html", "w") as f:
            f.write(html)
        print(f"wrote lpta_view_out.html ({len(html)} bytes)")
    else:
        serve(html)


if __name__ == "__main__":
    main()
