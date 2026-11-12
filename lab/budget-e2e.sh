#!/usr/bin/env bash
# lab/budget-e2e.sh - kcbudget end to end on the lab with EMAIL alerts, every step checked.
#
#   ./lab/budget-e2e.sh            ask for inputs (defaults = last run's answers), then run all steps
#   ./lab/budget-e2e.sh -y         no questions: reuse saved answers / defaults
#   kcctl budget-test [options]    same thing through kcctl
#
# Steps, each with an expected result (PASS / FAIL, log per step):
#    1 tools        python3, curl (+ docker for Mailpit)
#    2 mail         Mailpit inbox up (mailpit mode) / SMTP settings present (smtp) / outbox (dry-run)
#    3 preflight    runtime settings found, KUBECOST_URL present
#    4 doctor       Budget API and Allocation API reachable
#    5 validate     test copy of budgets/lab.json: 50 budgets valid
#    6 apply        50 managed budgets created/updated in Kubecost, no failures
#    7 replan       a second plan shows 0 creates / updates / deletes (idempotent)
#    8 smoke        throwaway budget: create, read, update, delete
#    9 dry-run      check writes alert emails to the outbox, sends nothing
#   10 send         check sends the alerts (mailpit: arrival confirmed via its API; smtp: you confirm)
#   11 dedupe       a second check sends nothing new in the same period
#   12 intake       a Microsoft Forms request (simulated issue) becomes a budget in Kubecost
#   13 restore      Kubecost back in line with the repo's budgets/lab.json (prune), 0 changes left
#
# Nothing in the repository is modified: the steps work on a copy of budgets/lab.json in the run
# directory. Recipients can be redirected to one test address; tripwire budgets can be forced to
# $0.01 so alerts fire immediately. The SMTP password is never written to disk.
#
# Options (each also settable as the env var shown, and saved for the next run except the password):
#   -y, --yes              accept defaults, no prompts            BE_YES=1
#   --url URL              Kubecost API                           KUBECOST_URL   (default: lab port, 9090)
#   --mode M               mailpit | smtp | dry-run               BE_MODE        (default: mailpit)
#   --to ADDRESS           send every alert to this address       BE_TO          (default: keep example.com)
#   --no-force             don't force tripwires to $0.01         BE_FORCE=0
#   --no-intake            skip the Forms intake step             BE_INTAKE=0
#   --keep                 skip the restore step                  BE_KEEP=1
#   --continue             keep going after a failed step         BE_CONTINUE=1
#   --reset                ignore saved answers
#   SMTP (mode smtp): BE_SMTP_HOST, BE_SMTP_PORT, BE_SMTP_TLS (starttls|ssl|none), BE_SMTP_USER,
#                     BE_SMTP_FROM, and the password from KCRESIZE_SMTP_PASSWORD or a hidden prompt.
# Exit code: 0 all steps passed, 1 a step failed, 2 bad input.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
# shellcheck disable=SC1091
[[ -f lab/lab.env ]] && { set -a; source lab/lab.env; set +a; }
CLUSTER="${CLUSTER:-kcresize-lab}"
WORKDIR="${WORKDIR:-./.lab/$CLUSTER}"
case "$WORKDIR" in /*) ;; *) WORKDIR="$ROOT/${WORKDIR#./}" ;; esac
ANSWERS="$WORKDIR/budget-e2e.answers"

if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then B=""; G=""; Y=""; R=""; D=""; N=""
else B=$'\033[1;34m'; G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[1;31m'; D=$'\033[2m'; N=$'\033[0m'; fi
die() { printf '%sERROR:%s %s\n' "$R" "$N" "$*" >&2; exit 2; }

# ---------- options
RESET=0
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) BE_YES=1 ;;
    --url) KUBECOST_URL="${2:?--url needs a value}"; shift ;;
    --mode) BE_MODE="${2:?--mode needs a value}"; shift ;;
    --to) BE_TO="${2:?--to needs a value}"; shift ;;
    --no-force) BE_FORCE=0 ;;
    --no-intake) BE_INTAKE=0 ;;
    --keep) BE_KEEP=1 ;;
    --continue) BE_CONTINUE=1 ;;
    --reset) RESET=1 ;;
    -h|--help) awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
  shift
done

# saved answers fill only what the command line / environment did not set
if [ "$RESET" = 0 ] && [ -f "$ANSWERS" ]; then
  while IFS='=' read -r k v; do
    case "$k" in BE_*|KUBECOST_URL) [ -z "${!k:-}" ] && printf -v "$k" '%s' "$v" ;; esac
  done < "$ANSWERS"
fi
if [ -z "${KUBECOST_URL:-}" ]; then
  # shellcheck disable=SC1091
  source lab/ui-port.sh && kc_resolve_ui_port >/dev/null 2>&1 || KUBECOST_URL="http://localhost:9090"
fi
BE_YES="${BE_YES:-0}"

ask() {  # ask VAR "question" default
  local var="$1" q="$2" def="${!1:-$3}" v
  if [ "$BE_YES" = 1 ]; then v="$def"
  else printf '%s%s%s [%s]: ' "$B" "$q" "$N" "$def"; read -r v; v="${v:-$def}"; fi
  printf -v "$var" '%s' "$v"
}
yesno() {  # yesno VAR "question" default(1|0)
  local var="$1" q="$2" def="${!1:-$3}" v
  if [ "$BE_YES" = 1 ]; then printf -v "$var" '%s' "$def"; return; fi
  printf '%s%s%s [%s]: ' "$B" "$q" "$N" "$([ "$def" = 1 ] && echo Y/n || echo y/N)"; read -r v
  case "${v:-}" in y|Y|yes) v=1 ;; n|N|no) v=0 ;; *) v="$def" ;; esac
  printf -v "$var" '%s' "$v"
}

echo "${B}kcbudget end-to-end test (email)${N}  cluster $CLUSTER"
ask KUBECOST_URL "Kubecost API URL" "$KUBECOST_URL"
ask BE_MODE "Email mode: mailpit (local inbox) | smtp (real mail) | dry-run (files only)" "mailpit"
case "$BE_MODE" in mailpit|smtp|dry-run) ;; *) die "mode must be mailpit, smtp or dry-run" ;; esac
if [ "$BE_MODE" = mailpit ]; then
  ask BE_MAILPIT_SMTP "Mailpit SMTP port" "1025"
  ask BE_MAILPIT_UI "Mailpit web/API port" "8025"
fi
if [ "$BE_MODE" = smtp ]; then
  ask BE_SMTP_HOST "SMTP host" "smtp.gmail.com"
  ask BE_SMTP_PORT "SMTP port" "587"
  ask BE_SMTP_TLS "SMTP TLS: starttls | ssl | none" "starttls"
  ask BE_SMTP_USER "SMTP user (blank = no login)" ""
  ask BE_SMTP_FROM "From address" "${BE_SMTP_USER:-kcbudget@lab.local}"
  if [ -n "$BE_SMTP_USER" ] && [ -z "${KCRESIZE_SMTP_PASSWORD:-}" ]; then
    [ "$BE_YES" = 1 ] && die "mode smtp with a user needs KCRESIZE_SMTP_PASSWORD when run with --yes"
    printf '%sSMTP password (hidden, not saved)%s: ' "$B" "$N"; read -rs KCRESIZE_SMTP_PASSWORD; echo
  fi
fi
ask BE_TO "Send all alerts to one test address (blank = keep the example.com recipients)" \
  "$([ "$BE_MODE" = smtp ] && echo "${BE_SMTP_USER:-}" || echo "")"
if [ -n "$BE_TO" ] && ! [[ "$BE_TO" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then die "not an email address: $BE_TO"; fi
if [ "$BE_MODE" = smtp ] && [ -z "$BE_TO" ]; then
  echo "${Y}Note:${N} real SMTP with example.com recipients will bounce; set a test address to see the mail."
fi
yesno BE_FORCE "Force the 3 tripwire budgets to \$0.01 so alerts fire now?" 1
yesno BE_INTAKE "Include the Microsoft Forms intake step?" 1
yesno BE_KEEP "Leave the test budgets in Kubecost afterwards (skip restore)?" 0
BE_CONTINUE="${BE_CONTINUE:-0}"

mkdir -p "$WORKDIR"
{ for k in KUBECOST_URL BE_MODE BE_MAILPIT_SMTP BE_MAILPIT_UI BE_SMTP_HOST BE_SMTP_PORT BE_SMTP_TLS BE_SMTP_USER BE_SMTP_FROM \
           BE_TO BE_FORCE BE_INTAKE BE_KEEP; do [ -n "${!k:-}" ] && printf '%s=%s\n' "$k" "${!k}"; done; } > "$ANSWERS"

# ---------- run directory and an isolated kcbudget environment
RUN="$WORKDIR/budget-e2e/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN" && chmod 700 "$RUN"
: > "$RUN/empty.env"; chmod 600 "$RUN/empty.env"
export PYTHONPATH="$ROOT" KUBECOST_URL KCBUDGET_ENV=kcbudget-lab \
       KCBUDGET_ENV_FILE="$RUN/empty.env" KCBUDGET_STATE="$RUN/check-state.json" KCRESIZE_OUTBOX="$RUN/outbox"
unset KCBUDGET_SECRETS_COMMAND KCBUDGET_WEBHOOKS KCBUDGET_WEBHOOKS_FILE KCRESIZE_NOTIFY_DRY_RUN KCBUDGET_NOTIFY_DRY_RUN
case "$BE_MODE" in
  mailpit) export KCRESIZE_SMTP_HOST=127.0.0.1 KCRESIZE_SMTP_PORT="$BE_MAILPIT_SMTP" KCRESIZE_SMTP_TLS=none \
                  KCRESIZE_SMTP_FROM="Kubecost Budgets <kcbudget@lab.local>"; unset KCRESIZE_SMTP_USER KCRESIZE_SMTP_PASSWORD ;;
  smtp)    export KCRESIZE_SMTP_HOST="$BE_SMTP_HOST" KCRESIZE_SMTP_PORT="$BE_SMTP_PORT" KCRESIZE_SMTP_TLS="$BE_SMTP_TLS" \
                  KCRESIZE_SMTP_FROM="$BE_SMTP_FROM"
           if [ -n "$BE_SMTP_USER" ]; then export KCRESIZE_SMTP_USER="$BE_SMTP_USER" KCRESIZE_SMTP_PASSWORD
           else unset KCRESIZE_SMTP_USER KCRESIZE_SMTP_PASSWORD; fi ;;
  dry-run) export KCRESIZE_SMTP_HOST=dry-run.invalid ;;
esac
PY="$(command -v python3 || true)"
MAILPIT="http://127.0.0.1:${BE_MAILPIT_UI:-8025}"
FILE="$RUN/lab.json"

kb() {  # kb <step-number> <kcbudget args...>  -> stdout/stderr in the step log, outputs in $RUN/out.<n>
  local n="$1"; shift
  GITHUB_OUTPUT="$RUN/out.$n" "$PY" -m kcbudget "$@"
}
outv() { sed -n "s/^$2=//p" "$RUN/out.$1" 2>/dev/null | tail -1; }        # outv <step> <key>
mail_total() { curl -fsS "$MAILPIT/api/v1/messages?limit=1" 2>/dev/null | "$PY" -c 'import sys,json; print(json.load(sys.stdin).get("total",0))' 2>/dev/null; }
outbox_emails() { ls "$RUN/outbox" 2>/dev/null | grep -c -- '-email\.json$' || true; }

# ---------- step runner
TOTAL=13; NUM=0; PASSED=0; FAILED=0; SKIPPED=0; ROWS=()
step() {  # step "name" function   -> the function prints its output, sets MSG, returns 0 (pass) / 1 (fail) / 3 (skip)
  local name="$1" fn="$2" rc log
  NUM=$((NUM + 1)); log="$RUN/$(printf '%02d' "$NUM")-$name.log"; MSG=""
  printf '\n%s[%2d/%d] %s%s\n' "$B" "$NUM" "$TOTAL" "$name" "$N"
  "$fn" > "$log" 2>&1; rc=$?
  local hidden; hidden="$(grep -c 'is not in KCBUDGET_WEBHOOKS' "$log" || true)"
  grep -v '^::add-mask::\|is not in KCBUDGET_WEBHOOKS\|^\*\*Warnings\*\*$' "$log" | grep -v '^[[:space:]]*$' \
    | tail -n "${BE_SHOW_LINES:-12}" | sed "s/^/  ${D}│${N} /"
  [ "${hidden:-0}" -gt 0 ] && printf '  %s│ (%s Slack/Teams "no webhook" warnings hidden: expected in an email-only test)%s\n' "$D" "$hidden" "$N"
  case "$rc" in
    0) PASSED=$((PASSED + 1)); printf '  %sPASS%s %s\n' "$G" "$N" "$MSG"; ROWS+=("PASS|$name|$MSG") ;;
    3) SKIPPED=$((SKIPPED + 1)); printf '  %sSKIP%s %s\n' "$Y" "$N" "$MSG"; ROWS+=("SKIP|$name|$MSG") ;;
    *) FAILED=$((FAILED + 1)); printf '  %sFAIL%s %s\n  log: %s\n' "$R" "$N" "$MSG" "$log"; ROWS+=("FAIL|$name|$MSG")
       if [ "$BE_CONTINUE" != 1 ]; then summary; exit 1; fi ;;
  esac
}
summary() {
  printf '\n%sSummary%s  run dir: %s\n' "$B" "$N" "$RUN"
  local r; for r in "${ROWS[@]}"; do
    IFS='|' read -r st nm ms <<< "$r"
    case "$st" in PASS) c="$G" ;; SKIP) c="$Y" ;; *) c="$R" ;; esac
    printf '  %s%-4s%s %-10s %s\n' "$c" "$st" "$N" "$nm" "$ms"
  done
  printf '  %d passed, %d failed, %d skipped\n' "$PASSED" "$FAILED" "$SKIPPED"
}

# ---------- steps
s_tools() {
  local miss=()
  for t in python3 curl; do command -v "$t" >/dev/null || miss+=("$t"); done
  [ "$BE_MODE" = mailpit ] && { command -v docker >/dev/null || miss+=(docker); }
  "$PY" --version 2>/dev/null; curl --version 2>/dev/null | head -1
  [ "$BE_MODE" = mailpit ] && docker --version 2>/dev/null
  if [ ${#miss[@]} -gt 0 ]; then MSG="missing: ${miss[*]}"; return 1; fi
  MSG="tools present"
}

s_mail() {
  case "$BE_MODE" in
    dry-run) MSG="dry-run: alerts are written to $RUN/outbox only"; return 3 ;;
    smtp) echo "SMTP $KCRESIZE_SMTP_HOST:$KCRESIZE_SMTP_PORT tls=$KCRESIZE_SMTP_TLS user=${KCRESIZE_SMTP_USER:-<none>} from=$KCRESIZE_SMTP_FROM"
          if (exec 3<>"/dev/tcp/$KCRESIZE_SMTP_HOST/$KCRESIZE_SMTP_PORT") 2>/dev/null; then MSG="SMTP server reachable"
          else MSG="cannot connect to $KCRESIZE_SMTP_HOST:$KCRESIZE_SMTP_PORT"; return 1; fi ;;
    mailpit)
      if ! curl -fsS "$MAILPIT/api/v1/info" >/dev/null 2>&1; then
        if docker ps -a --format '{{.Names}}' | grep -qx kcbudget-mailpit; then docker start kcbudget-mailpit
        else docker run -d --name kcbudget-mailpit -p "127.0.0.1:$BE_MAILPIT_SMTP:1025" -p "127.0.0.1:$BE_MAILPIT_UI:8025" axllent/mailpit; fi
        for _ in $(seq 1 30); do curl -fsS "$MAILPIT/api/v1/info" >/dev/null 2>&1 && break; sleep 1; done
      fi
      curl -fsS "$MAILPIT/api/v1/info" >/dev/null 2>&1 || { MSG="Mailpit not reachable at $MAILPIT"; return 1; }
      curl -fsS -X DELETE "$MAILPIT/api/v1/messages" >/dev/null 2>&1 || true
      echo "inbox: $MAILPIT (emptied for this run)"
      MSG="Mailpit up, inbox $MAILPIT, total $(mail_total) messages" ;;
  esac
}

s_preflight() {
  kb 3 preflight || { MSG="preflight exited non-zero"; return 1; }
  [ "$(outv 3 ready)" = true ] || { MSG="KUBECOST_URL not found (is the lab running? kcctl resume)"; return 1; }
  MSG="KUBECOST_URL=$KUBECOST_URL"
}

s_doctor() {
  if ! kb 4 doctor > "$RUN/doctor.txt" 2>&1; then
    cat "$RUN/doctor.txt"
    if grep -qi "refused\|timed out\|Name or service\|nodename" "$RUN/doctor.txt"; then
      MSG="Kubecost not reachable at $KUBECOST_URL (lab running? see 'kcctl env' / 'kcctl resume', or pass --url)"
    elif grep -q "HTTP 404" "$RUN/doctor.txt"; then
      MSG="Budget API path not found (HTTP 404): re-run with KCBUDGET_API_STYLE=rest"
    else MSG="doctor failed (see log)"; fi
    return 1
  fi
  cat "$RUN/doctor.txt"
  grep -q "Budget API reachable" "$RUN/doctor.txt" || { MSG="Budget API not confirmed"; return 1; }
  MSG="$(head -1 "$RUN/doctor.txt")"
}

s_validate() {
  "$PY" - "$ROOT/budgets/lab.json" "$FILE" "$BE_TO" "$BE_FORCE" <<'EOF'
import json, sys
src, dst, to, force = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
d = json.load(open(src))
if to:
    d["allowedEmailDomains"] = sorted({to.split("@", 1)[1].lower(), *d.get("allowedEmailDomains", [])})
    for b in d["budgets"]:
        for a in b.get("alerts", []):
            if a.get("emails"):
                a["emails"] = [to]
forced = 0
if force:
    for b in d["budgets"]:
        if b["name"].startswith("kcb-tripwire-"):
            b["spendLimit"] = 0.01; forced += 1
json.dump(d, open(dst, "w"), indent=2)
print(f"test copy: {dst}  recipients: {to or 'unchanged (example.com)'}  tripwires forced: {forced}")
EOF
  kb 5 validate --file "$FILE" || { MSG="budget file invalid"; return 1; }
  [ "$(outv 5 budgets)" = 50 ] || { MSG="expected 50 budgets, got $(outv 5 budgets)"; return 1; }
  MSG="50 budgets valid"
}

s_apply() {
  kb 6 apply --file "$FILE" --prune --max-deletes 60 || { MSG="apply reported failures"; return 1; }
  local c u d; c="$(outv 6 create)"; u="$(outv 6 update)"; d="$(outv 6 delete)"
  [ $(( c + u + $(outv 6 noop) )) -eq 50 ] || { MSG="expected 50 managed budgets, got create=$c update=$u noop=$(outv 6 noop)"; return 1; }
  MSG="created $c, updated $u, deleted $d, unchanged $(outv 6 noop)"
}

s_replan() {
  kb 7 plan --file "$FILE" || { MSG="plan failed"; return 1; }
  [ "$(outv 7 changed)" = false ] || { MSG="plan still shows changes (create=$(outv 7 create) update=$(outv 7 update) delete=$(outv 7 delete))"; return 1; }
  MSG="0 changes, $(outv 7 noop) budgets in sync"
}

s_smoke() {
  kb 8 smoke | tee "$RUN/smoke.txt" || { MSG="smoke test failed"; return 1; }
  ! grep -q '❌' "$RUN/smoke.txt" || { MSG="a smoke step failed"; return 1; }
  MSG="create, read, update, delete OK"
}

s_dryrun() {
  rm -rf "$RUN/outbox"
  KCBUDGET_NOTIFY_DRY_RUN=1 KCBUDGET_STATE="$RUN/dry-state.json" kb 9 check --channels email || { MSG="check failed"; return 1; }
  local a e; a="$(outv 9 alerted)"; e="$(outbox_emails)"
  echo "outbox: $RUN/outbox"; ls "$RUN/outbox" 2>/dev/null | head -5
  [ "${a:-0}" -ge 1 ] || { MSG="no budget crossed a threshold (enable forcing tripwires, or wait for spend)"; return 1; }
  [ "$e" -ge 1 ] || { MSG="$a budgets alerted but no email in the outbox"; return 1; }
  MSG="$a budgets over a threshold, $e emails written to the outbox (nothing sent)"
}

s_send() {
  if [ "$BE_MODE" = dry-run ]; then MSG="dry-run mode"; return 3; fi
  local before; before="$(mail_total)"
  kb 10 check --channels email || { MSG="check failed"; return 1; }
  local a; a="$(outv 10 alerted)"
  [ "${a:-0}" -ge 1 ] || { MSG="no alerts sent"; return 1; }
  if [ "$BE_MODE" = mailpit ]; then
    local got=0
    for _ in $(seq 1 20); do got=$(( $(mail_total) - ${before:-0} )); [ "$got" -ge "$a" ] && break; sleep 1; done
    curl -fsS "$MAILPIT/api/v1/messages?limit=10" | "$PY" -c '
import sys, json
for m in json.load(sys.stdin).get("messages", []):
    print("  mail:", m.get("Subject"), "->", ", ".join(t.get("Address", "") for t in m.get("To") or []))'
    [ "$got" -ge "$a" ] || { MSG="sent $a, but only $got arrived in Mailpit"; return 1; }
    MSG="$a alerts sent, $got emails arrived in Mailpit ($MAILPIT)"
  else
    if [ "$BE_YES" = 1 ]; then MSG="$a alerts sent to ${BE_TO:-the budget recipients} (not confirmed: --yes)"; return 0; fi
    local v; printf '%sDid the alert email(s) arrive at %s? [y/N]%s ' "$B" "${BE_TO:-the recipients}" "$N" > /dev/tty
    read -r v < /dev/tty
    case "$v" in y|Y|yes) MSG="$a alerts sent and confirmed received" ;; *) MSG="$a sent, not confirmed received"; return 1 ;; esac
  fi
}

s_dedupe() {
  if [ "$BE_MODE" = dry-run ]; then MSG="dry-run mode"; return 3; fi
  local before; before="$(mail_total)"
  kb 11 check --channels email || { MSG="check failed"; return 1; }
  [ "$(outv 11 alerted)" = 0 ] || { MSG="check alerted again in the same period ($(outv 11 alerted))"; return 1; }
  if [ "$BE_MODE" = mailpit ]; then sleep 2; [ "$(mail_total)" = "$before" ] || { MSG="new mail arrived"; return 1; }; fi
  MSG="no repeat alerts in the same period"
}

s_intake() {
  if [ "$BE_INTAKE" != 1 ]; then MSG="disabled"; return 3; fi
  local requester="${BE_TO:-you@example.com}"
  "$PY" - "$RUN/issue.json" "$requester" <<'EOF'
import json, sys
form = {"environment": "lab", "requester": sys.argv[2], "team": "e2e test", "costCenter": "CC-0000",
        "scopeType": "namespace", "scopeValues": "shop-dev", "interval": "Weekly", "intervalDay": "1",
        "amount": "0.01", "thresholds": "[\"100%\"]", "pool": "bu-commerce", "justification": "budget-e2e.sh",
        "slackChannel": "None", "teamsChannel": "None"}
body = "Budget request submitted through Microsoft Forms.\n\n```json\n" + json.dumps(form, indent=2) + "\n```\n"
json.dump({"action": "opened", "issue": {"number": 999, "title": "[kcbudget-request] e2e test", "body": body}}, open(sys.argv[1], "w"))
EOF
  kb 12 request --file "$RUN/{environment}.json" --payload "$RUN/issue.json" --pr-body "$RUN/pr-body.md" || { MSG="request rejected"; return 1; }
  local name; name="$(outv 12 budget)"
  echo "--- PR body:"; cat "$RUN/pr-body.md"
  kb 12 apply --file "$FILE" || { MSG="apply of the request failed"; return 1; }
  [ "$(outv 12 create)" = 1 ] || { MSG="expected 1 new budget, got create=$(outv 12 create)"; return 1; }
  "$PY" -m kcbudget export --prefix "$name" | grep -q "\"$name\"" || { MSG="$name not found in Kubecost"; return 1; }
  MSG="request -> $name created in Kubecost"
}

s_restore() {
  if [ "$BE_KEEP" = 1 ]; then MSG="kept test budgets (run again without --keep to restore)"; return 3; fi
  kb 13 apply --file "$ROOT/budgets/lab.json" --prune --max-deletes 60 || { MSG="restore apply failed"; return 1; }
  kb 13 plan --file "$ROOT/budgets/lab.json" >/dev/null || { MSG="plan failed"; return 1; }
  [ "$(outv 13 changed)" = false ] || { MSG="Kubecost still differs from budgets/lab.json"; return 1; }
  MSG="Kubecost matches budgets/lab.json again (0 changes)"
}

step tools     s_tools
step mail      s_mail
step preflight s_preflight
step doctor    s_doctor
step validate  s_validate
step apply     s_apply
step replan    s_replan
step smoke     s_smoke
step dry-run   s_dryrun
step send      s_send
step dedupe    s_dedupe
step intake    s_intake
step restore   s_restore
summary
[ "$FAILED" = 0 ] && echo "${G}All steps passed.${N} Re-run any time: ./lab/budget-e2e.sh -y" || exit 1
