#!/usr/bin/env bash
# lab/setup.sh - wire a fresh kc-lab (kind + Kubecost) to this GitHub repository.
#
# Settings: lab/lab.env (copy lab/lab.env.example). Nothing here is tied to a person or machine.
#
#   ./lab/setup.sh config        write .github/kcresize.json from lab.env (owners, actioning team, scope)
#   ./lab/setup.sh workloads     deploy lab/workloads.yaml (over-requested sample apps)
#   ./lab/setup.sh github        repo variables, actioning environment, labels (needs gh CLI, logged in)
#   ./lab/setup.sh secrets       prompt for notification secrets (SMTP / Slack / Teams / webhook) -> repo secrets
#   ./lab/setup.sh runner        install a self-hosted GitHub Actions runner on this machine (label kc-lab)
#   ./lab/setup.sh doctor        check the runner can reach Kubecost and the controller is alive
#   ./lab/setup.sh all           config workloads github runner doctor
#
# Why a self-hosted runner: the lab's Kubecost listens on http://localhost:9090 on this machine,
# which GitHub-hosted runners cannot reach. For a real deployment, point KUBECOST_URL at an internal
# address your runners can reach (or ARC runners inside the cluster) instead.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
[[ -f lab/lab.env ]] && { set -a; source lab/lab.env; set +a; }

CLUSTER="${CLUSTER:-kcresize-lab}"
CTX="${CTX:-kind-$CLUSTER}"
UI_PORT="${UI_PORT:-9091}"
KUBECOST_URL="${KUBECOST_URL:-http://localhost:$UI_PORT}"
RUNNER_LABEL="${RUNNER_LABEL:-$CLUSTER}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner-$CLUSTER}"
APPROVAL_LABEL="${APPROVAL_LABEL:-kcresize.io/approval=required}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
repo() { gh repo view --json nameWithOwner -q .nameWithOwner; }

step_config() {
  local gh_user owner owner_email act act_email
  gh_user="$(gh api user -q .login 2>/dev/null || true)"
  owner="${OWNER_GITHUB:-$gh_user}"; owner_email="${OWNER_EMAIL:-}"
  act="${ACTIONING_GITHUB:-$owner}"; act_email="${ACTIONING_EMAIL:-$owner_email}"
  [[ -n "$owner" ]] || die "Set OWNER_GITHUB in lab/lab.env (or log in with gh)"
  log "Writing .github/kcresize.json (owner $owner, actioning $act)"
  python3 lab/render-config.py "$owner" "$owner_email" "$act" "$act_email" "$APPROVAL_LABEL" > .github/kcresize.json
  [[ -n "$owner_email" ]] || echo "No OWNER_EMAIL set: owners still get GitHub's own issue-assignment emails."
  echo "Commit and push .github/kcresize.json so the workflows use it."
}

step_workloads() {
  log "Sample workloads (tbm-payments, shop-prod, shop-dev)"
  kubectl --context "$CTX" apply -f lab/workloads.yaml
  for d in tbm-payments/fraud-scorer tbm-payments/checkout-api shop-prod/api shop-dev/web; do
    kubectl --context "$CTX" -n "${d%%/*}" rollout status "deploy/${d#*/}" --timeout=180s || true
  done
  echo "Kubecost needs a few hours of usage data before recommendations settle (window 48h)."
}

step_github() {
  command -v gh >/dev/null || die "Install the GitHub CLI: brew install gh && gh auth login"
  local r; r="$(repo)"
  log "Repository $r"
  gh variable set KUBECOST_URL --body "$KUBECOST_URL"
  gh variable set KUBECOST_CLUSTER --body "$CLUSTER"
  gh variable set KCRESIZE_RUNS_ON --body "[\"self-hosted\",\"$RUNNER_LABEL\"]"
  gh variable set KCRESIZE_SETUP_PYTHON --body "false"      # macOS runner already has python3
  log "Environment kubecost-actioning (add required reviewers in Settings > Environments for a second sign-off)"
  gh api -X PUT "repos/$r/environments/kubecost-actioning" >/dev/null
  log "Labels"
  for l in kcresize:0e8a16 kcresize:pending:fbca04 kcresize:approved:1d76db kcresize:scheduled:5319e7 \
           kcresize:applied:0e8a16 kcresize:rejected:b60205 kcresize:declined:b60205 kcresize:failed:d93f0b \
           kcresize:stale:c5def5 kcresize:expired:c5def5; do
    gh label create "${l%:*}" --color "${l##*:}" --force >/dev/null
  done
  local me; me="$(gh api user -q .login)"
  if grep -q YOUR_GITHUB_USER .github/kcresize.json; then
    echo "Next: ./lab/setup.sh config  (you are '$me'), then commit and push .github/kcresize.json"
  fi
}

step_secrets() {
  command -v gh >/dev/null || die "Install the GitHub CLI first"
  log "Notification secrets (press Enter to skip any)"
  local name v
  for name in KCRESIZE_SMTP_HOST KCRESIZE_SMTP_PORT KCRESIZE_SMTP_USER KCRESIZE_SMTP_PASSWORD KCRESIZE_SMTP_FROM \
              KCRESIZE_SLACK_OWNER_WEBHOOK KCRESIZE_SLACK_ACTION_WEBHOOK KCRESIZE_TEAMS_OWNER_WEBHOOK \
              KCRESIZE_TEAMS_ACTION_WEBHOOK KCRESIZE_WEBHOOK_URL KCRESIZE_WEBHOOK_SECRET; do
    read -r -s -p "$name: " v; echo
    [[ -n "$v" ]] && printf '%s' "$v" | gh secret set "$name" && echo "  set $name"
  done
  echo "Gmail: KCRESIZE_SMTP_HOST=smtp.gmail.com, PORT=587, USER=<you>@gmail.com, PASSWORD=<16-char app password>."
}

step_runner() {
  command -v gh >/dev/null || die "Install the GitHub CLI first"
  local r os arch ver tok url
  r="$(repo)"
  case "$(uname -s)" in Darwin) os=osx;; Linux) os=linux;; *) die "unsupported OS";; esac
  case "$(uname -m)" in x86_64) arch=x64;; arm64|aarch64) arch=arm64;; *) die "unsupported arch";; esac
  ver="$(gh api repos/actions/runner/releases/latest -q .tag_name)"; ver="${ver#v}"
  url="https://github.com/actions/runner/releases/download/v$ver/actions-runner-$os-$arch-$ver.tar.gz"
  log "Self-hosted runner $ver ($os-$arch) in $RUNNER_DIR, label $RUNNER_LABEL"
  mkdir -p "$RUNNER_DIR"
  [[ -x "$RUNNER_DIR/config.sh" ]] || curl -fsSL "$url" | tar -xz -C "$RUNNER_DIR"
  tok="$(gh api -X POST "repos/$r/actions/runners/registration-token" -q .token)"
  ( cd "$RUNNER_DIR" && ./config.sh --unattended --replace --url "https://github.com/$r" --token "$tok" \
      --name "$CLUSTER-$(hostname -s)" --labels "$RUNNER_LABEL" --work _work )
  if [[ "$os" == osx ]]; then
    ( cd "$RUNNER_DIR" && ./svc.sh install && ./svc.sh start ) || echo "Start it manually: cd $RUNNER_DIR && ./run.sh"
  else
    ( cd "$RUNNER_DIR" && sudo ./svc.sh install "$USER" && sudo ./svc.sh start ) || echo "Start it manually: cd $RUNNER_DIR && ./run.sh"
  fi
}

step_doctor() {
  log "kcresize doctor against $KUBECOST_URL"
  PYTHONPATH=. KUBECOST_URL="$KUBECOST_URL" KUBECOST_CLUSTER="$CLUSTER" KCRESIZE_CONFIG=.github/kcresize.json python3 -m kcresize doctor
}

[[ $# -gt 0 ]] || { awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$0"; exit 1; }
for s in "$@"; do
  case "$s" in
    all) step_config; step_workloads; step_github; step_runner; step_doctor ;;
    config|workloads|github|secrets|runner|doctor) "step_$s" ;;
    *) die "unknown step $s" ;;
  esac
done
