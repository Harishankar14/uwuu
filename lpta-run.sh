#!/usr/bin/env bash
#
# lpta-run.sh — capture one LPTA run to <label>.json
#
# Runs the LPTA plugin over an .ll file at a chosen optimization level (with
# optional extra opt flags), captures the codegen terminus, and writes a
# validated <label>.json ready for `lpta.py list/show/diff`.
#
# Usage:
#   ./lpta-run.sh <input.ll> <label> [-O <level>] [-x "<extra opt flags>"]
#
# Examples:
#   ./lpta-run.sh lz4.ll  O2                               # -O2 baseline
#   ./lpta-run.sh lz4.ll  O3     -O 3                      # -O3
#   ./lpta-run.sh lz4.ll  novec  -x "-disable-loop-vectorization"
#   ./lpta-run.sh lz4.ll  avx    -O 3 -x "-mcpu=native"
#
# Plugin path defaults to ./build/libLPTA.so; override with LPTA_PLUGIN=/path.
#
set -euo pipefail

PLUGIN="${LPTA_PLUGIN:-./build/libLPTA.so}"

usage() {
  sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# parse args (flags accepted in any position)
INPUT=""; LABEL=""; LEVEL="2"; EXTRA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -O|--opt-level) LEVEL="${2:?-O needs a level, e.g. -O 3}"; shift 2 ;;
    -x|--extra)     EXTRA="${2:?-x needs a quoted flag string}"; shift 2 ;;
    -h|--help)      usage 0 ;;
    -*)             echo "lpta-run: unknown option '$1'" >&2; usage 1 ;;
    *)
      if   [[ -z "$INPUT" ]]; then INPUT="$1"
      elif [[ -z "$LABEL" ]]; then LABEL="$1"
      else echo "lpta-run: unexpected argument '$1'" >&2; usage 1
      fi
      shift ;;
  esac
done

[[ -n "$INPUT" && -n "$LABEL" ]] || { echo "lpta-run: need <input.ll> and <label>" >&2; usage 1; }
[[ -f "$INPUT" ]] || { echo "lpta-run: input not found: $INPUT" >&2; exit 1; }
[[ -f "$PLUGIN" ]] || { echo "lpta-run: plugin not found: $PLUGIN (set LPTA_PLUGIN=)" >&2; exit 1; }

# split the extra-flags string into an array (empty stays empty)
EXTRA_ARR=()
[[ -n "$EXTRA" ]] && read -ra EXTRA_ARR <<< "$EXTRA"

BC="${LABEL}.opt.bc"
OBJ="${LABEL}.o"

echo "lpta-run: $INPUT  ->  $LABEL.json   (-O$LEVEL${EXTRA:+  extra: $EXTRA})"

# IR pipeline: plugin writes its fixed lpta.json 
opt -load-pass-plugin="$PLUGIN" -passes="default<O${LEVEL}>" \
    "${EXTRA_ARR[@]}" "$INPUT" -o "$BC"

#  IR -> object 
llc -filetype=obj "-O${LEVEL}" "$BC" -o "$OBJ"

#  end-state metrics, PER FUNCTION 
DIS=$(objdump -d --no-show-raw-insn "$OBJ")
CGLINES=""                       # accumulates the per-function JSON codegen lines
NFUNC=0
T_INSTRS=0; T_BRANCH=0; T_VEC=0; T_SCALAR=0   # running totals for the sanity check

# awk splits the disassembly into per-function blocks and prints, per function:
#   name <TAB> instrs <TAB> branches <TAB> vector <TAB> scalar
while IFS=$'\t' read -r FN FI FB FV FS; do
  [[ -z "$FN" ]] && continue
  CGLINES+=$(printf '{"stage": "codegen", "unit": "%s", "target": "x86_64", "asm_instrs": %s,"branches": %s, "vector_instrs": %s, "scalar_sse_instrs": %s}' \
             "$FN" "$FI" "$FB" "$FV" "$FS")
  CGLINES+=$'\n'
  NFUNC=$((NFUNC+1))
  T_INSTRS=$((T_INSTRS+FI)); T_BRANCH=$((T_BRANCH+FB)); T_VEC=$((T_VEC+FV)); T_SCALAR=$((T_SCALAR+FS))
done < <(printf '%s\n' "$DIS" | awk '
  # a new function header: "<addr> <name>:"
  /^[0-9a-f]+ <.*>:/ {
    if (name != "") print name "\t" ins "\t" br "\t" vec "\t" sca
    name = $0; sub(/^[0-9a-f]+ </, "", name); sub(/>:$/, "", name)
    ins = br = vec = sca = 0
    next
  }
  # an instruction line: "   <addr>:   mnemonic ..."
  /^[[:space:]]+[0-9a-f]+:/ {
    ins++
    # isolate the mnemonic = first token after the address+colon
    m = $0; sub(/^[[:space:]]+[0-9a-f]+:[[:space:]]+/, "", m); sub(/[[:space:]].*/, "", m)
    if (m ~ /^j[a-z]+$/) br++
    if (m ~ /(ps|pd)$/ || m ~ /^v?(padd|psub|pmul|pcmp|pand|por|pxor|pshuf|punpck|pack|pmovmsk|pmin|pmax|pavg|psll|psrl|psra|pblend|ptest)/ || m ~ /^v?movdq/) vec++
    else if (m ~ /(ss|sd)$/) sca++
  }
  END { if (name != "") print name "\t" ins "\t" br "\t" vec "\t" sca }
')

BYTES=$(size "$OBJ" | awk 'NR==2 {print $1}'); BYTES=${BYTES:-0}

# append per-function codegen terminus, then name the artifact 
printf '{"stage": "codegen", "unit": "(module)", "target": "x86_64", "asm_instrs": %s, "code_size_bytes": %s, "branches": %s, "vector_instrs": %s, "scalar_sse_instrs": %s}\n' \
  "$T_INSTRS" "$BYTES" "$T_BRANCH" "$T_VEC" "$T_SCALAR" >> lpta.json
printf '%s' "$CGLINES" >> lpta.json
mv lpta.json "${LABEL}.json"

echo "lpta-run: per-function codegen totals — funcs=$NFUNC instrs=$T_INSTRS branches=$T_BRANCH vector=$T_VEC scalar=$T_SCALAR"

#validity gate (the jq reflex, baked in; python is always present)
if python3 - "$LABEL.json" <<'PY'
import json, sys
bad = 0
for i, line in enumerate(open(sys.argv[1]), 1):
    line = line.strip()
    if not line:
        continue
    try:
        json.loads(line)
    except json.JSONDecodeError as e:
        print(f"  BAD line {i}: {e}"); bad += 1
sys.exit(1 if bad else 0)
PY
then
  echo "lpta-run: wrote $LABEL.json — $NFUNC functions, ${T_INSTRS} instrs, ${BYTES} bytes, ${T_BRANCH} branches, ${T_VEC} vector ops  [VALID]"
  echo "lpta-run: next  ->  python3 lpta.py list $LABEL.json"
else
  echo "lpta-run: WROTE $LABEL.json BUT IT HAS INVALID JSON (see above)" >&2
  exit 1
fi
