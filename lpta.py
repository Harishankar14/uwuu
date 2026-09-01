#!/usr/bin/env python3
"""
lpta.py - read and render LPTA transformation logs.

Subcommands:
  list  FILE            ranked overview of every recorded pass, + codegen terminus
  show  FILE PASS       one pass rendered as a git-style red/green IR diff
  diff  FILEA FILEB     cross-run: find where two pipelines diverge, and why

Reads the JSON-Lines files LPTA emits: one record per line. Pass records carry a
"pass" field; codegen records carry "stage":"codegen" and are now PER FUNCTION
(one line per function symbol, plus an optional "(module)" total line). Color
auto-disables when output is piped (like git).
"""

import argparse
import json
import os
import sys
import re
from collections import Counter

# color layer 

class C:
    enabled = True
    RED = "\033[31m"; GREEN = "\033[32m"; CYAN = "\033[36m"
    YELLOW = "\033[33m"; DIM = "\033[2m"; BOLD = "\033[1m"; RESET = "\033[0m"

    @classmethod
    def w(cls, code, s):
        return f"{code}{s}{cls.RESET}" if cls.enabled else s


def red(s):    return C.w(C.RED, s)
def green(s):  return C.w(C.GREEN, s)
def cyan(s):   return C.w(C.CYAN, s)
def yellow(s): return C.w(C.YELLOW, s)
def dim(s):    return C.w(C.DIM, s)
def bold(s):   return C.w(C.BOLD, s)

BAR = "=" * 64
SEP = "-" * 64
DOT = chr(183)
ARROW = chr(8594)
BULLET = chr(8226)


#  loading 

def load_run(path):
    """Parse a JSON-Lines LPTA file -> (pass_records, codegen_dict).
    codegen_dict maps unit-name -> codegen record (per-function schema; may
    include a '(module)' total). Malformed lines are reported and skipped."""
    passes, codegen = [], {}
    try:
        fh = open(path, "r")
    except OSError as e:
        sys.exit(f"lpta: cannot open {path}: {e.strerror}")
    with fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                print(red(f"lpta: {path}:{lineno}: malformed JSON ({e}); skipped"),
                      file=sys.stderr)
                continue
            if rec.get("stage") == "codegen":
                codegen[rec.get("unit", "(module)")] = rec
            elif "pass" in rec:
                passes.append(rec)
            else:
                print(yellow(f"lpta: {path}:{lineno}: record has neither 'pass' nor "
                             f"'stage'; skipped"), file=sys.stderr)
    return passes, codegen


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
    """(total_or_None, {unit: rec}). Uses a '(module)' record for the total if
    present, else sums the per-function records."""
    if not cg:
        return None, {}
    module = cg.get("(module)")
    funcs = {k: v for k, v in cg.items() if k != "(module)"}
    total = module if module else codegen_total(funcs)
    return total, funcs


#  shared rendering 

def inst_key(ir_line):
    s = ir_line.strip()
    s = re.sub(r'^%\S+\s*=\s*', '', s)
    s = re.sub(r'%[A-Za-z0-9._]+', '%v', s)
    s = re.sub(r',?\s*!\S+\s*!\d+', '', s)
    return ' '.join(s.split())


_CALL_PREFIX = {"tail", "musttail", "notail"}


def opcode_of(line):
    """The instruction mnemonic for one printed IR line, best-effort."""
    toks = line.strip().split()
    if len(toks) >= 2 and toks[1] == "=":
        toks = toks[2:]
    while toks and toks[0] in _CALL_PREFIX:
        toks = toks[1:]
    return toks[0] if toks else "?"


def classify_diff(removed, added):
    """Opcode profile of a structural diff + count of lines that appear on both
    sides (same instruction, renamed) -- the churn that inflates raw counts."""
    rem_ops = Counter(opcode_of(l) for l in removed)
    add_ops = Counter(opcode_of(l) for l in added)
    rk = Counter(inst_key(l) for l in removed)
    ak = Counter(inst_key(l) for l in added)
    churn = sum(min(rk[k], ak[k]) for k in (rk.keys() & ak.keys()))
    return {"rem_total": len(removed), "add_total": len(added),
            "rem_ops": rem_ops.most_common(), "add_ops": add_ops.most_common(),
            "churn": churn}


def _ops_str(ops, cap=6):
    parts = [f"{n}{chr(215)}{op}" for op, n in ops[:cap]]
    if len(ops) > cap:
        parts.append(f"+{len(ops) - cap} more")
    return f"  {DOT}  ".join(parts)


def render_classified(removed, added, indent="    "):
    """Compact opcode-profile summary lines for a (removed, added) diff."""
    removed = removed or []
    added = added or []
    if not removed and not added:
        return [dim(f"{indent}(no instruction-level change captured)")]
    c = classify_diff(removed, added)
    out = []
    if c["rem_total"]:
        out.append(f"{indent}{red('-' + str(c['rem_total']))} removed  "
                   f"{dim(_ops_str(c['rem_ops']))}")
    if c["add_total"]:
        out.append(f"{indent}{green('+' + str(c['add_total']))} added  "
                   f"{dim(_ops_str(c['add_ops']))}")
    if c["churn"]:
        out.append(dim(f"{indent}{c['churn']} appear on both sides "
                       f"(renamed / renumbered, not real work)"))
    return out


def fmt_delta(rec):
    d = rec.get("delta")
    return "n/a" if d is None else f"{d:+d}"


def fmt_metrics(rec):
    parts = [f"score {rec.get('score', 0.0):.2f}",
             f"rel {rec.get('rel_score', 0.0):.3f}"]
    d = rec.get("delta")
    parts.append("delta n/a (invalidated)" if d is None else f"{d:+d} instrs")
    vd = rec.get("vector_delta", 0)
    if vd:
        parts.append(green(f"vector {vd:+d}"))
    t = rec.get("time_ms")
    if t is not None:
        parts.append(f"{t:.2f}ms")
    return (f"  {DOT}  ").join(parts)


def render_remarks(rec, indent="  ", limit=6):
    remarks = rec.get("remarks", [])
    if not remarks:
        return []
    seen = {}
    for r in remarks:
        key = (bool(r.get("missed")), " ".join((r.get("msg") or "").split()))
        seen[key] = seen.get(key, 0) + 1
    n_vec = sum(c for (m, msg), c in seen.items()
                if not m and "vectorized loop" in msg)
    n_missed = sum(c for (m, _), c in seen.items() if m)
    out = []
    if n_vec or n_missed:
        bits = []
        if n_vec:
            bits.append(green(f"{n_vec} vectorized"))
        if n_missed:
            bits.append(red(f"{n_missed} missed"))
        out.append(f"{indent}{DOT} " + "  ".join(bits))
    shown = 0
    for (missed, msg), count in seen.items():
        if limit is not None and shown >= limit:
            break
        tag = red("MISSED") if missed else cyan("remark")
        times = dim(f"  (x{count})") if count > 1 else ""
        out.append(f"{indent}{tag}: {msg}{times}")
        shown += 1
    hidden = len(seen) - shown
    if hidden > 0:
        out.append(dim(f"{indent}... {hidden} more distinct "
                       f"remark{'s' if hidden != 1 else ''}"))
    return out


def render_diff_body(removed, added, limit=None, indent="  "):
    lines = []

    def emit(items, sign, colorfn, word):
        shown = items if limit is None else items[:limit]
        for it in shown:
            lines.append(colorfn(f"{indent}{sign} {it.strip()}"))
        hidden = len(items) - len(shown)
        if hidden > 0:
            lines.append(dim(f"{indent}  ... {hidden} more {word}"))

    emit(removed or [], "-", red, "removed")
    emit(added or [], "+", green, "added")
    if not lines:
        lines.append(dim(f"{indent}(no instruction-level change captured)"))
    return lines


def render_pass_block(rec, limit=None):
    ut = rec.get("unit_type", "?")
    out = [cyan(BAR),
           f"  {bold(rec.get('pass', '?'))}  {DOT}  "
           f"{ut} {bold(rec.get('unit', '?'))}",
           f"  {fmt_metrics(rec)}"]
    out += render_remarks(rec)
    out.append(dim(SEP))
    # loop passes carry instKey strings (not full IR) -- note it so it's not confusing
    if ut == "loop" and (rec.get("removed") or rec.get("added")):
        out.append(dim("  loop diff: structural keys (opcode:type(operands)), "
                       "not full IR text"))
    out += render_diff_body(rec.get("removed"), rec.get("added"), limit=limit)
    out.append(dim(SEP))
    nr = len(rec.get("removed", []) or [])
    na = len(rec.get("added", []) or [])
    out.append(f"  {red(str(nr) + ' removed')} {DOT} {green(str(na) + ' added')}")
    return "\n".join(out)


def render_codegen_contrast(cgA, cgB):
    """Module-total machine-code A->B, summed from per-function records."""
    tA, _ = split_codegen(cgA)
    tB, _ = split_codegen(cgB)
    if not tA or not tB:
        print(dim("  (codegen terminus missing in one or both runs)"))
        return
    print(f"  {bold('Machine code:')} {dim('module total, summed over functions')}")
    rows = [("instructions", "asm_instrs", False),
            ("branches", "branches", False),
            ("vector ops", "vector_instrs", True)]
    have_bytes = isinstance(tA.get("code_size_bytes"), (int, float)) and tA.get("code_size_bytes") \
        or isinstance(tB.get("code_size_bytes"), (int, float)) and tB.get("code_size_bytes")
    if have_bytes:
        rows.insert(1, ("code size (B)", "code_size_bytes", False))
    for label, key, hl in rows:
        va, vb = tA.get(key, 0), tB.get(key, 0)
        num = isinstance(va, (int, float)) and isinstance(vb, (int, float))
        note = ""
        vbstr = str(vb)
        if hl and num and vb < va:
            vbstr = red(str(vb)); note = red(f"   {chr(8592)} {va - vb} lost")
        elif hl and num and vb > va:
            vbstr = green(str(vb)); note = green(f"   {chr(8592)} {vb - va} gained")
        elif num:
            note = dim(f"  ({vb - va:+d})")
        print(f"    {label:<15} A {va}  {ARROW}  B {vbstr}{note}")


def render_perfunc_metal(names, funcs_a, funcs_b):
    """Per-function vector A->B for the diverging functions, with a verdict."""
    if not names:
        return
    print(f"  {bold('Per-function vector ops:')} {dim('diverging functions, A ' + ARROW + ' B')}")
    for n in names:
        ra, rb = funcs_a.get(n), funcs_b.get(n)
        if ra is None and rb is None:
            print(f"    {n:<44} {dim('- inlined, no codegen symbol')}")
            continue
        va = ra.get("vector_instrs", 0) if ra else 0
        vb = rb.get("vector_instrs", 0) if rb else 0
        if va == vb:
            verdict = dim(f"{chr(8592)} held")
        elif vb < va:
            verdict = red(f"{chr(8592)} lost {va - vb}")
        else:
            verdict = green(f"{chr(8592)} gained {vb - va}")
        nums = f"vector {va} {ARROW} {vb}"
        print(f"    {n:<44} {nums:<18} {verdict}")


# list

def tier(score):
    if score >= 20: return bold
    if score >= 5:  return lambda s: s
    return dim


def cmd_list(args):
    passes, codegen = load_run(args.file)
    total, funcs = split_codegen(codegen)
    ranked = sorted(passes, key=lambda r: r.get("score", 0.0), reverse=True)
    print(cyan(BAR))
    print(f"  {bold('LPTA')} {DOT} {args.file} {DOT} "
          f"{len(passes)} recorded passes")
    print(cyan(BAR))
    print(f"  {'score':>8}  {'rel':>6}  {'delta':>6}  {'vec':>4}   pass on unit")
    print(dim(SEP))
    for r in ranked:
        score = r.get("score", 0.0)
        vd = r.get("vector_delta", 0)
        vstr = green(f"{vd:+d}") if vd else dim(" 0")
        missed = any(rm.get("missed") for rm in r.get("remarks", []))
        flag = "   " + red("! missed") if missed else ""
        line = (f"  {score:>8.2f}  {r.get('rel_score', 0.0):>6.3f}  "
                f"{fmt_delta(r):>6}  {vstr:>4}   "
                f"{r.get('pass')} on {r.get('unit')}")
        print(tier(score)(line) + flag)
    if total:
        print(dim(SEP))
        print(f"  {bold(chr(9660) + ' codegen terminus')} ({total.get('target', '?')})")
        bits = [f"{total.get('asm_instrs', '?')} instrs"]
        cs = total.get("code_size_bytes")
        if isinstance(cs, (int, float)) and cs:
            bits.append(f"{cs} bytes")
        bits.append(f"{total.get('branches', '?')} branches")
        bits.append(green(f"{total.get('vector_instrs', '?')} vector ops"))
        sc = total.get("scalar_sse_instrs")
        if isinstance(sc, (int, float)) and sc:
            bits.append(dim(f"{sc} scalar SSE"))
        print("    " + f" {DOT} ".join(bits))
        # per-function: where the SIMD lives
        vecfns = sorted((r for r in funcs.values() if r.get("vector_instrs", 0) > 0),
                        key=lambda r: r.get("vector_instrs", 0), reverse=True)
        if vecfns:
            print(dim(f"  vector ops by function (top {min(8, len(vecfns))} of {len(vecfns)}):"))
            for r in vecfns[:8]:
                print(f"    {r.get('unit','?'):<44} "
                      f"{green(str(r.get('vector_instrs',0)))} {dim('vec ' + DOT + ' ' + str(r.get('asm_instrs',0)) + ' instrs')}")
    print(cyan(BAR))


# show 

def cmd_show(args):
    passes, _ = load_run(args.file)
    q = args.pass_name.lower()
    matches = [r for r in passes if q in (r.get("pass") or "").lower()]
    if args.unit:
        matches = [r for r in matches if r.get("unit") == args.unit]
    if not matches:
        names = sorted({r.get("pass") for r in passes})
        print(red(f"lpta: no pass matching '{args.pass_name}' in {args.file}"),
              file=sys.stderr)
        print(dim("available: " + ", ".join(n for n in names if n)), file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(dim(f"  {len(matches)} records match '{args.pass_name}' "
                  f"- showing all in pipeline order\n"))
    limit = None if args.full else args.context
    print("\n\n".join(render_pass_block(r, limit=limit) for r in matches))


#diff (cross-run aligner)

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


def cmd_diff(args):
    passesA, cgA = load_run(args.file_a)
    passesB, cgB = load_run(args.file_b)
    _, funcs_a = split_codegen(cgA)
    _, funcs_b = split_codegen(cgB)
    ops = lcs_align(keyed(passesA), keyed(passesB))

    diverged, only_a, only_b = [], [], []
    for kind, ia, ib in ops:
        if kind == "match":
            ra, rb = passesA[ia], passesB[ib]
            if outcome_sig(ra) != outcome_sig(rb):
                diverged.append((ra, rb))
        elif kind == "a_only":
            only_a.append(passesA[ia])
        else:
            only_b.append(passesB[ib])

    diverged.sort(key=lambda p: divergence_score(*p), reverse=True)
    significant = [p for p in diverged if divergence_score(*p) >= SIG_THRESHOLD]

    print(cyan(BAR))
    print(f"  {bold('LPTA cross-run')}  {DOT}  A={args.file_a}  vs  B={args.file_b}")
    print(f"  {len(passesA)} passes in A {DOT} {len(passesB)} in B {DOT} "
          f"{len(diverged)} differ ({len(significant)} significant) {DOT} "
          f"{len(only_a)} only-A {DOT} {len(only_b)} only-B")
    print(cyan(BAR))

    if not diverged:
        print(green("\n  Pipelines agree on every shared pass "
                    "- no behavioral divergence.\n"))
        render_codegen_contrast(cgA, cgB)
        print(cyan(BAR))
        return

    ra, rb = diverged[0]
    reason = divergence_reason(ra, rb)
    print(f"\n  {bold(red(chr(9654) + ' Root divergence:'))} "
          f"{bold(ra.get('pass'))} on {ra.get('unit')}  {dim('(' + reason + ')')}\n")
    print(f"    {bold('Run A')}:  delta {fmt_delta(ra)}  {DOT}  "
          f"vector {ra.get('vector_delta', 0):+d}")
    for ln in render_remarks(ra, indent="             ", limit=3):
        print(ln)
    print(f"    {bold('Run B')}:  delta {fmt_delta(rb)}  {DOT}  "
          f"vector {rb.get('vector_delta', 0):+d}")
    for ln in render_remarks(rb, indent="             ", limit=3):
        print(ln)

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
    if why_bits:
        print(f"\n  {bold('Why:')} {yellow(', '.join(why_bits))} in B")
    if new_in_b:
        print(f"  {dim('new refusals in B:')}")
        for m in new_in_b[:4]:
            print(f"    {yellow(BULLET)} {m}")
        if len(new_in_b) > 4:
            print(dim(f"    ... {len(new_in_b) - 4} more"))

    if len(significant) > 1:
        print(dim("\n" + SEP))
        print(f"  {dim('other significant divergences:')}")
        for ra2, rb2 in significant[1:8]:
            print(f"    {red(BULLET)} {ra2.get('pass')} on {ra2.get('unit')} "
                  f"{dim('(' + divergence_reason(ra2, rb2) + ')')}")

    # what each run uniquely did at the root -- classified profile (raw with --full)
    a_add, b_add = ra.get("added", []) or [], rb.get("added", []) or []
    a_rem, b_rem = ra.get("removed", []) or [], rb.get("removed", []) or []

    def _only(primary, other):
        pk = {}
        for line in primary:
            pk.setdefault(inst_key(line), line)
        ok = {inst_key(line) for line in other}
        return [ln for k, ln in pk.items() if k not in ok], len(set(pk) & ok)

    a_add_only, add_sh = _only(a_add, b_add)
    b_add_only, _ = _only(b_add, a_add)
    a_rem_only, rem_sh = _only(a_rem, b_rem)
    b_rem_only, _ = _only(b_rem, a_rem)

    if a_add_only or b_add_only or a_rem_only or b_rem_only:
        print(dim("\n" + SEP))
        print(f"  {bold('What each run uniquely did')} {dim('(root pass, operand-aware)')}")
        if a_rem_only or a_add_only:
            print(f"  {dim('Only in A:')}")
            for ln in render_classified(a_rem_only, a_add_only):
                print(ln)
        if b_rem_only or b_add_only:
            print(f"  {dim('Only in B:')}")
            for ln in render_classified(b_rem_only, b_add_only):
                print(ln)
        both = add_sh + rem_sh
        if both:
            print(dim(f"  ({both} instructions identical in both runs - not shown)"))
        if args.full:
            print(dim("\n  --- raw IR ---"))
            for label, items, sign, colorfn in (
                    ("Only A added", a_add_only, "+", green),
                    ("Only B added", b_add_only, "+", red),
                    ("Only A removed", a_rem_only, "-", green),
                    ("Only B removed", b_rem_only, "-", red)):
                if items:
                    print(f"  {colorfn(label)}:")
                    for it in items:
                        print(colorfn(f"    {sign} {it.strip()}"))

    # machine code: module total, then per-function join over diverging functions
    print(dim(SEP))
    render_codegen_contrast(cgA, cgB)
    names, seen = [], set()
    for r, _ in ([diverged[0]] + significant[1:13]):
        u = r.get("unit")
        if u and u not in seen:
            seen.add(u); names.append(u)
    if names and (funcs_a or funcs_b):
        print(dim(SEP))
        render_perfunc_metal(names, funcs_a, funcs_b)

    other = len(diverged) - len(significant) + len(only_a) + len(only_b)
    print(dim(SEP))
    print(f"  {yellow(str(other) + ' other records differ')} "
          f"{dim('(incidental count/score drift or downstream - not ranked)')}")
    print(cyan(BAR))


#  entry 

def main():
    p = argparse.ArgumentParser(prog="lpta",
                                description="Render LPTA transformation logs.")
    p.add_argument("--no-color", action="store_true", help="disable ANSI color")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="ranked overview of one run")
    pl.add_argument("file")
    pl.set_defaults(fn=cmd_list)

    ps = sub.add_parser("show", help="one pass as a red/green IR diff")
    ps.add_argument("file")
    ps.add_argument("pass_name", help="pass name or substring (e.g. Vectorize)")
    ps.add_argument("--unit", help="restrict to this unit (function/loop name)")
    ps.add_argument("--context", type=int, default=8,
                    help="max lines shown per side (default 8)")
    ps.add_argument("--full", action="store_true", help="show every line")
    ps.set_defaults(fn=cmd_show)

    pd = sub.add_parser("diff", help="cross-run: find the divergence")
    pd.add_argument("file_a")
    pd.add_argument("file_b")
    pd.add_argument("--context", type=int, default=8,
                    help="max produced-lines shown (default 8)")
    pd.add_argument("--full", action="store_true", help="show raw IR, not just the profile")
    pd.set_defaults(fn=cmd_diff)

    args = p.parse_args()
    C.enabled = (sys.stdout.isatty() and not args.no_color
                 and os.environ.get("NO_COLOR") is None)
    args.fn(args)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        os._exit(0)
    except KeyboardInterrupt:
        os._exit(130)
