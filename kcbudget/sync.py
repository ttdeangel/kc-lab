"""Plan / apply / export / smoke: keep Kubecost budgets in step with a budget file.

Ownership: only budgets whose name starts with the file's managedPrefix are managed. Budgets created
by hand in the Kubecost UI (any other name) are never changed or deleted.
"""
import time

from . import spec

FIELDS = ("values", "kind", "interval", "intervalDay", "spendLimit", "actions")


def _diff(cur, want):
    a, b = spec.normalize(cur), spec.normalize(want)
    return [f for f in FIELDS if a[f] != b[f]]


def plan(doc, api, webhooks, prune=False, strict=False):
    """Return {"changes": [...], "problems": [...], "remote": n}. Nothing is written."""
    prefix = doc.get("managedPrefix", "kcb-")
    problems = []
    desired = {}
    for raw in doc.get("budgets", []):
        body = spec.to_kubecost(doc, raw, webhooks, strict=False, problems=problems)
        desired[body["name"]] = body
    if strict and problems:
        raise spec.SpecError(problems)
    remote = api.list()
    by_name = {}
    for r in remote:
        by_name.setdefault(r.get("name"), []).append(r)
    changes = []
    for name, body in desired.items():
        cur = by_name.get(name) or []
        if not cur:
            changes.append({"op": "create", "name": name, "body": body})
            continue
        first, extra = cur[0], cur[1:]
        d = _diff(first, body)
        changes.append({"op": "update" if d else "noop", "name": name, "id": first.get("id"), "body": body, "fields": d,
                        "before": spec.normalize(first)})
        for x in extra:
            problems.append(f"{name}: duplicate budget id {x.get('id')} in Kubecost" + ("; deleting it" if prune else "; run with prune to remove it"))
            if prune:
                changes.append({"op": "delete", "name": name, "id": x.get("id"), "reason": "duplicate"})
    for name, items in by_name.items():
        if name in desired or not (name or "").startswith(prefix):
            continue
        for r in items:
            changes.append({"op": "delete" if prune else "orphan", "name": name, "id": r.get("id")})
    order = {"delete": 0, "update": 1, "create": 2, "orphan": 3, "noop": 4}
    changes.sort(key=lambda c: (order[c["op"]], c["name"]))
    return {"changes": changes, "problems": problems, "remote": len(remote), "unmanaged": sum(1 for r in remote if not (r.get("name") or "").startswith(prefix))}


def apply(result, api, max_deletes=None):
    """Execute a plan. Returns a list of (op, name, outcome)."""
    deletes = [c for c in result["changes"] if c["op"] == "delete"]
    if max_deletes is not None and len(deletes) > max_deletes:
        raise SystemExit(f"refusing to delete {len(deletes)} budgets (max-deletes={max_deletes}); raise the limit if intended")
    log = []
    for c in result["changes"]:
        try:
            if c["op"] == "create":
                bid = api.create(c["body"])
                log.append(("create", c["name"], f"id {bid}" if bid else "created"))
            elif c["op"] == "update":
                api.update(c["id"], c["body"])
                log.append(("update", c["name"], "updated: " + ", ".join(c["fields"])))
            elif c["op"] == "delete":
                api.delete(c["id"])
                log.append(("delete", c["name"], "deleted"))
        except Exception as e:      # keep going; the run fails at the end
            log.append((c["op"], c["name"], f"FAILED: {e}"))
    return log


def summary_markdown(env, result, applied=None, pools=None):
    counts = {}
    for c in result["changes"]:
        counts[c["op"]] = counts.get(c["op"], 0) + 1
    lines = [f"### Kubecost budgets: `{env}`", "",
             "| Create | Update | Delete | Unchanged | Orphaned (not in file) | Unmanaged (UI) |", "|---|---|---|---|---|---|",
             f"| {counts.get('create', 0)} | {counts.get('update', 0)} | {counts.get('delete', 0)} | {counts.get('noop', 0)} "
             f"| {counts.get('orphan', 0)} | {result.get('unmanaged', 0)} |", ""]
    rows = [c for c in result["changes"] if c["op"] != "noop"]
    if rows:
        lines += ["| Op | Budget | Detail |", "|---|---|---|"]
        for c in rows:
            if c["op"] in ("create", "update"):
                b = c["body"]
                scope = "; ".join(f"{k}={','.join(v)}" for k, v in b["values"].items())
                det = f"{scope} · ${b['spendLimit']:,} {b['interval']} · alerts {','.join(str(a['percentage']) + '%' for a in b['actions']) or 'none'}"
                if c["op"] == "update":
                    det = "changed: " + ", ".join(c["fields"]) + " · " + det
            elif c["op"] == "orphan":
                det = "in Kubecost but not in the file; apply with prune to delete"
            else:
                det = c.get("reason", "not in the file")
            lines.append(f"| {c['op']} | `{c['name']}` | {det} |")
        lines.append("")
    if pools:
        lines += ["| Pool | Allocated / month | Limit / month | Budgets |", "|---|---|---|---|"]
        for p, u in pools.items():
            pct = (u["allocated"] / u["monthlyLimit"] * 100) if u["monthlyLimit"] else 0
            lines.append(f"| {p} | ${u['allocated']:,.2f} ({pct:.0f}%) | ${u['monthlyLimit']:,.2f} | {u['budgets']} |")
        lines.append("")
    if result["problems"]:
        lines += ["**Warnings**", ""] + [f"- {p}" for p in result["problems"]] + [""]
    if applied is not None:
        lines += ["**Applied**", "", "| Op | Budget | Result |", "|---|---|---|"]
        lines += [f"| {o} | `{n}` | {r} |" for o, n, r in applied] or ["| — | — | nothing to do |"]
    return "\n".join(lines) + "\n"


def export(api, prefix=None, reveal=False):
    """Kubecost budgets as a budget file (to adopt existing budgets). Webhooks are masked unless reveal."""
    out = []
    for r in api.list():
        if prefix and not (r.get("name") or "").startswith(prefix):
            continue
        n = spec.normalize(r)
        alerts = []
        for a in n["actions"]:
            al = {"percentage": a["percentage"]}
            if a.get("emails"):
                al["emails"] = a["emails"]
            if a.get("slackWebhooks"):
                al["slackWebhooksFound"] = a["slackWebhooks"] if reveal else spec.mask(a["slackWebhooks"])
            if a.get("msTeamsWebhooks"):
                al["teamsWebhooksFound"] = a["msTeamsWebhooks"] if reveal else spec.mask(a["msTeamsWebhooks"])
            alerts.append(al)
        out.append({"name": r.get("name"), "values": n["values"], "kind": n["kind"], "interval": n["interval"],
                    "intervalDay": n["intervalDay"], "spendLimit": n["spendLimit"], "alerts": alerts, "_kubecostId": r.get("id")})
    return {"managedPrefix": prefix or "kcb-", "budgets": out}


def smoke(api, prefix="kcb-", namespace="kubecost", run_id=None):
    """Create -> read -> update -> delete a throwaway budget. Returns list of (step, ok, detail)."""
    name = f"{prefix}smoke-{run_id or int(time.time())}"[:63].rstrip("-")
    body = {"name": name, "values": {"namespace": [namespace]}, "kind": "soft", "interval": "weekly",
            "intervalDay": 1, "spendLimit": 1, "actions": []}
    steps, bid = [], None

    def find():
        return [b for b in api.list() if b.get("name") == name]

    try:
        steps.append(("detect API", True, api.detect()))
        bid = api.create(body)
        got = find()
        if not bid and got:
            bid = got[0].get("id")
        steps.append(("create", bool(got and bid), f"id {bid}"))
        if got:
            d = _diff(got[0], body)
            steps.append(("read back", not d, "fields match" if not d else "differs: " + ", ".join(d)))
            api.update(bid, dict(body, spendLimit=2))
            got = find()
            ok = bool(got) and float(got[0].get("spendLimit") or 0) == 2.0
            steps.append(("update", ok, f"spendLimit {got[0].get('spendLimit') if got else 'missing'}"))
    except Exception as e:
        steps.append(("error", False, str(e)))
    finally:
        if bid:
            try:
                api.delete(bid)
                steps.append(("delete", not find(), "removed"))
            except Exception as e:
                steps.append(("delete", False, str(e)))
    return steps
