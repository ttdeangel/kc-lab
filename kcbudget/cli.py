"""Command line: python -m kcbudget <command> [options]

  preflight                          load runtime settings and report which are present (never values)
  validate  --file F                 check a budget file (names, scopes, alerts, channels, pool headroom)
  doctor                             check the Kubecost Budget API (paths, list, allocation)
  plan      --file F [--prune]       show what apply would change in Kubecost (writes nothing)
  apply     --file F [--prune]       create / update (/ delete with --prune) managed budgets
  check     [--channels ...]         measure spend for budgets IN KUBECOST and notify on thresholds
  export    [--prefix P] [--reveal]  print Kubecost budgets in budget-file format
  smoke                              create, read, update and delete a throwaway budget
  request   --file F --payload P     add a Microsoft Forms request (issue or dispatch event) to the file;
                                     F may contain {environment}, e.g. budgets/{environment}.json

Settings are loaded at runtime on the machine running kcbudget, never from GitHub secrets (see kcbudget/runtime.py):
process env, then KCBUDGET_SECRETS_COMMAND (customer-supplied provider), then KCBUDGET_ENV_FILE (~/.kcbudget/<env>.env).
Keys: KUBECOST_URL (required except validate/request), KUBECOST_AUTH_HEADER or KUBECOST_BASIC_USER/_PASSWORD,
KCBUDGET_WEBHOOKS (JSON) or KCBUDGET_WEBHOOKS_FILE, KCBUDGET_ENV (label), KCBUDGET_READ_ONLY=1,
KCBUDGET_NOTIFY_DRY_RUN=1, KCBUDGET_API_STYLE=documented|rest, SMTP via KCRESIZE_SMTP_*.
Exit codes: 0 ok, 1 error, 2 invalid budget file or request, 4 smoke test failed.
"""
import argparse
import json
import os
import sys

from . import check, intake, runtime, spec, sync
from .api import BudgetAPI, KubecostError


def _out(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def _summary(md):
    print(md)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")


def _api(read_only=False):
    url = os.environ.get("KUBECOST_URL", "").strip()
    if not url:
        raise SystemExit("KUBECOST_URL is required")
    ro = read_only or os.environ.get("KCBUDGET_READ_ONLY", "").lower() in ("1", "true", "yes") or "demo.kubecost.io" in url
    return BudgetAPI(url, read_only=ro)


def _load_valid(path):
    doc = spec.load(path)
    errors, warnings = spec.validate(doc)
    for w in warnings:
        print(f"::warning::{w}")
    if errors:
        for e in errors:
            print(f"::error::{e}")
        raise SystemExit(2)
    return doc


def _args(argv):
    ap = argparse.ArgumentParser(prog="kcbudget", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["validate", "preflight", "doctor", "plan", "apply", "check", "export", "smoke", "request"])
    ap.add_argument("--file", default=os.environ.get("KCBUDGET_FILE", ""))
    ap.add_argument("--env", default=os.environ.get("KCBUDGET_ENV", "lab"))
    ap.add_argument("--prune", action="store_true", default=os.environ.get("KCBUDGET_PRUNE", "").lower() in ("1", "true", "yes"))
    ap.add_argument("--strict", action="store_true", default=os.environ.get("KCBUDGET_STRICT", "").lower() in ("1", "true", "yes"),
                    help="fail when a referenced Slack/Teams channel has no webhook (recommended for prod)")
    ap.add_argument("--max-deletes", type=int, default=int(os.environ.get("KCBUDGET_MAX_DELETES", "10")))
    ap.add_argument("--channels", default=os.environ.get("KCBUDGET_CHANNELS", "teams"),
                    help="repo-side delivery for check: any of email,slack,teams (or none)")
    ap.add_argument("--state", default=os.environ.get("KCBUDGET_STATE", ".kcbudget/check-state.json"))
    ap.add_argument("--prefix", default=None)
    ap.add_argument("--reveal", action="store_true")
    ap.add_argument("--namespace", default=os.environ.get("KCBUDGET_SMOKE_NAMESPACE", "kubecost"))
    ap.add_argument("--payload", default=os.environ.get("GITHUB_EVENT_PATH", ""))
    ap.add_argument("--pr-body", default=".kcbudget/pr-body.md")
    ap.add_argument("--link", default=os.environ.get("KUBECOST_UI_URL", ""))
    return ap.parse_args(argv)


def main(argv=None):
    a = _args(sys.argv[1:] if argv is None else argv)
    try:
        for line in runtime.load():
            print(("::warning::" if line.startswith("warning") and os.environ.get("GITHUB_ACTIONS") else "") + line)
    except runtime.RuntimeError_ as e:
        print(f"::error::{e}")
        return 1
    if os.environ.get("KCBUDGET_NOTIFY_DRY_RUN"):
        os.environ["KCRESIZE_NOTIFY_DRY_RUN"] = os.environ["KCBUDGET_NOTIFY_DRY_RUN"]
    try:
        return _run(a)
    except spec.SpecError as e:
        for m in e.errors:
            print(f"::error::{m}")
        return 2
    except KubecostError as e:
        print(f"::error::Kubecost: {e}")
        return 1


def _run(a):
    if a.command in ("validate", "plan", "apply", "request") and not a.file:
        raise SystemExit("--file (or KCBUDGET_FILE) is required")

    if a.command == "preflight":
        st = runtime.status()
        print("runtime settings present: " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in st.items()))
        ready = st["KUBECOST_URL"]
        if not ready:
            print("::notice::KUBECOST_URL is not set on this runner (process env, KCBUDGET_SECRETS_COMMAND or "
                  f"{os.environ.get('KCBUDGET_ENV_FILE') or runtime.default_env_file()}); skipping.")
        _out(ready=str(ready).lower())
        return 0

    if a.command == "validate":
        doc = _load_valid(a.file)
        pools = spec.pool_usage(doc)
        print(f"{a.file}: {len(doc['budgets'])} budgets OK")
        for p, u in pools.items():
            print(f"  pool {p}: ${u['allocated']:,.2f} of ${u['monthlyLimit']:,.2f}/month across {u['budgets']} budgets")
        _out(budgets=len(doc["budgets"]))
        return 0

    if a.command == "request":
        with open(a.payload) as f:
            payload = intake.payload_from_event(json.load(f))
        a.file = intake.target_file(a.file, payload)
        doc = _load_valid(a.file)
        b, replaced, md = intake.apply_request(doc, payload, spec.webhooks_from_env() if os.environ.get("KCBUDGET_WEBHOOKS") else None)
        spec.save(doc, a.file)
        os.makedirs(os.path.dirname(a.pr_body) or ".", exist_ok=True)
        with open(a.pr_body, "w") as f:
            f.write(md)
        print(md)
        _out(budget=b["name"], replaced=str(replaced).lower(), file=a.file)
        return 0

    api = _api(read_only=a.command in ("plan", "export", "doctor"))

    if a.command == "doctor":
        style = api.detect()
        items = api.list()
        print(f"Budget API reachable ({style} paths); {len(items)} budgets in Kubecost")
        if items:
            print("  fields on a budget:", ", ".join(sorted(items[0].keys())))
        spend = api.spend('namespace:"kubecost"', *_today())
        print(f"  allocation API reachable (kubecost namespace today: ${spend:,.2f})")
        return 0

    if a.command in ("plan", "apply"):
        doc = _load_valid(a.file)
        webhooks = spec.webhooks_from_env()
        result = sync.plan(doc, api, webhooks, prune=a.prune, strict=a.strict)
        for p in result["problems"]:
            print(f"::warning::{p}")
        applied = sync.apply(result, api, max_deletes=a.max_deletes) if a.command == "apply" else None
        _summary(sync.summary_markdown(a.env, result, applied, spec.pool_usage(doc)))
        counts = {op: sum(1 for c in result["changes"] if c["op"] == op) for op in ("create", "update", "delete", "orphan", "noop")}
        _out(**counts, changed=str(any(counts[k] for k in ("create", "update", "delete"))).lower())
        if applied and any("FAILED" in r for _, _, r in applied):
            return 1
        return 0

    if a.command == "check":
        chans = [] if a.channels.strip() in ("", "none") else [c.strip() for c in a.channels.split(",")]
        rows = check.run(api, a.state, chans, env=a.env, prefix=a.prefix or "", link=a.link)
        _summary(check.summary_markdown(a.env, rows, chans))
        _out(over=sum(1 for r in rows if r.get("pct", 0) >= 100), alerted=sum(1 for r in rows if r.get("newThresholds")))
        return 0

    if a.command == "export":
        print(json.dumps(sync.export(api, a.prefix, a.reveal), indent=2))
        return 0

    if a.command == "smoke":
        steps = sync.smoke(api, prefix=a.prefix or "kcb-", namespace=a.namespace, run_id=os.environ.get("GITHUB_RUN_ID"))
        md = [f"### Budget API smoke test: `{a.env}`", "", "| Step | OK | Detail |", "|---|---|---|"]
        md += [f"| {s} | {'✅' if ok else '❌'} | {d} |" for s, ok, d in steps]
        _summary("\n".join(md) + "\n")
        return 0 if steps and all(ok for _, ok, _ in steps) else 4
    return 1


def _today():
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    return check.iso(now.replace(hour=0, minute=0, second=0, microsecond=0)), check.iso(now)
