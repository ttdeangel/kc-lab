#!/usr/bin/env bash
# lab/e2e-local.sh - full end-to-end run on the lab WITHOUT GitHub (file state), from this machine.
#
#   plan -> owner approve (with note) -> actioning apply -> Kubecost Action -> watch until applied
#
# Real Kubecost, real resize. Notifications use whatever channel secrets are exported
# (KCRESIZE_SMTP_* for email, KCRESIZE_SLACK_*, ...); set KCRESIZE_NOTIFY_DRY_RUN=1 to write them
# to .kcresize/outbox/ instead.
#
#   OWNER=me ACTIONER=me WORKLOAD=tbm-payments/fraud-scorer ./lab/e2e-local.sh
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
[[ -f lab/lab.env ]] && { set -a; source lab/lab.env; set +a; }

WORKLOAD="${WORKLOAD:-tbm-payments/fraud-scorer}"
OWNER="${OWNER:-${OWNER_GITHUB:-lab-owner}}"
ACTIONER="${ACTIONER:-${ACTIONING_GITHUB:-lab-ops}}"
WAIT_MIN="${WAIT_MIN:-120}"
export PYTHONPATH=. KCRESIZE_STATE=file KCRESIZE_STATE_FILE="${KCRESIZE_STATE_FILE:-.kcresize/e2e-state.json}"
CLUSTER="${CLUSTER:-kcresize-lab}" WORKDIR="${WORKDIR:-./.lab/${CLUSTER:-kcresize-lab}}"
# shellcheck disable=SC1091
source lab/ui-port.sh
kc_resolve_ui_port || exit 1
export KUBECOST_URL KUBECOST_CLUSTER="${KUBECOST_CLUSTER:-$CLUSTER}"
NS="${WORKLOAD%%/*}"

# A throwaway config: the chosen namespace, you as owner and as actioning team
CFG="$(mktemp)"; trap 'rm -f "$CFG"' EXIT
python3 - "$CFG" "$NS" "$OWNER" "$ACTIONER" "${OWNER_EMAIL:-}" "${ACTION_EMAIL:-${OWNER_EMAIL:-}}" <<'PY'
import json, sys
path, ns, owner, act, oe, ae = sys.argv[1:]
json.dump({"kubecost": {"cluster": "x"}, "scope": {"namespaces": [ns]},
           "owners": {ns: {"github": [owner], "email": [e for e in [oe] if e]}},
           "actioning": {"github": [act], "email": [e for e in [ae] if e]},
           "notify": {"owner": ["email", "slack", "teams", "webhook"], "actioning": ["email", "slack", "teams", "webhook"]},
           "state": "file"}, open(path, "w"))
PY
export KCRESIZE_CONFIG="$CFG"
kc() { python3 -m kcresize "$@"; }

echo "== doctor";  kc doctor
echo "== plan";    kc plan >/dev/null; kc status
kc show "$WORKLOAD" >/dev/null 2>&1 || { echo "No proposal for $WORKLOAD (no significant recommendation yet?)"; exit 1; }
echo "== owner approves";   KCRESIZE_ACTOR="$OWNER" kc approve "$WORKLOAD" --reason "lab e2e" --force
echo "== actioning applies"; KCRESIZE_ACTOR="$ACTIONER" kc apply "$WORKLOAD"
echo "== waiting for Kubecost (up to $WAIT_MIN min; it typically takes ~70 min)"
start=$(date +%s)
while (( $(date +%s) - start < WAIT_MIN * 60 )); do
  kc watch | grep -v "nothing changed" || true
  st="$(python3 -c "import json;print(json.load(open('$KCRESIZE_STATE_FILE'))['proposals']['$WORKLOAD']['status'])")"
  [[ "$st" == scheduled ]] || break
  printf '  %s still scheduled\n' "$(date -u +%H:%M:%S)"; sleep 60
done
kc status
kc show "$WORKLOAD" | sed -n '1,25p'
[[ "$st" == applied ]] && echo "PASS: $WORKLOAD resized by Kubecost" || { echo "FAIL: status $st"; exit 1; }
