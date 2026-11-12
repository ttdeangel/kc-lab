"""Budget requests from Microsoft Forms (via Power Automate -> GitHub repository_dispatch).

The flow posts event_type "kcbudget-request" with the form answers as client_payload. This module
turns them into one budget entry, validates it against the target file (naming, channels, email
domains, pool headroom), writes the file, and returns a pull request description. Nothing reaches
Kubecost until the PR is reviewed and merged; the deploy workflow then syncs it.

client_payload (all strings, as Forms delivers them; see examples/forms/):
  environment lab|prod · requester · team · costCenter · scopeType namespace|cluster · scopeValues "a, b"
  interval Monthly|Weekly · intervalDay · amount · thresholds '["50%","80%","100%"]' · emails "a@x; b@x"
  slackChannel · teamsChannel · pool · justification · responseId
"""
import json
import re

from . import spec

SLUG = re.compile(r"[^a-z0-9]+")


def _slug(s):
    return SLUG.sub("-", (s or "").lower()).strip("-")


def _list(v, sep=r"[;,\n]"):
    """Forms multi-choice answers arrive as a JSON array in a string; text answers as delimited text."""
    if isinstance(v, list):
        items = v
    else:
        s = (v or "").strip()
        if s.startswith("["):
            try:
                items = json.loads(s)
            except json.JSONDecodeError:
                items = re.split(sep, s.strip("[]"))
        else:
            items = re.split(sep, s)
    return [str(x).strip().strip('"') for x in items if str(x).strip().strip('"')]


def _num(v, what):
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        raise spec.SpecError([f"{what} must be a number (got {v!r})"])


def to_budget(doc, p):
    scope_type = (p.get("scopeType") or "namespace").strip().lower()
    if scope_type not in spec.KNOWN_SCOPE_KEYS:
        raise spec.SpecError([f"scopeType must be one of {', '.join(spec.KNOWN_SCOPE_KEYS)}"])
    scope = [_slug(x) if scope_type == "namespace" else x.strip() for x in _list(p.get("scopeValues"), r"[;,\s]+")]
    if not scope:
        raise spec.SpecError(["scopeValues is empty"])
    interval = (p.get("interval") or "monthly").strip().lower()
    day = int(_num(p.get("intervalDay") or 1, "intervalDay"))
    amount = _num(p.get("amount"), "amount")
    amount = int(amount) if amount.is_integer() else round(amount, 2)
    pcts = sorted({int(_num(x.rstrip("%"), "threshold")) for x in _list(p.get("thresholds"))}) or [80, 100]
    emails = sorted({e.lower() for e in _list(p.get("emails"), r"[;,\s]+")} | ({p["requester"].lower()} if p.get("requester") else set()))
    slack = [_slug(p["slackChannel"])] if (p.get("slackChannel") or "").strip() and p["slackChannel"].strip().lower() != "none" else []
    teams = [_slug(p["teamsChannel"])] if (p.get("teamsChannel") or "").strip() and p["teamsChannel"].strip().lower() != "none" else []
    team = _slug(p.get("team")) or "team"
    short = {"namespace": "ns", "cluster": "cl"}[scope_type]
    name = f"{doc.get('managedPrefix', 'kcb-')}{team}-{short}-{_slug(scope[0])}-{interval}"[:63].rstrip("-")
    alerts = []
    for pc in pcts:
        a = {"percentage": pc}
        if emails:
            a["emails"] = emails
        if slack:
            a["slack"] = slack
        if teams:
            a["teams"] = teams
        alerts.append(a)
    b = {"name": name, "owner": team, "values": {scope_type: scope}, "interval": interval, "intervalDay": day,
         "spendLimit": amount, "alerts": alerts,
         "request": {k: p.get(k) for k in ("requester", "costCenter", "justification", "responseId") if p.get(k)}}
    if p.get("costCenter"):
        b["costCenter"] = p["costCenter"]
    if p.get("pool"):
        b["pool"] = p["pool"]
    return b


def apply_request(doc, payload, webhooks=None):
    """Add or replace the requested budget in doc. Returns (budget, replaced, pr_markdown). Raises SpecError."""
    b = to_budget(doc, payload)
    budgets = [x for x in doc.get("budgets", []) if x.get("name") != b["name"]]
    replaced = len(budgets) != len(doc.get("budgets", []))
    trial = dict(doc, budgets=budgets + [b])
    errors, warnings = spec.validate(trial)
    if webhooks is not None:
        for kind in ("slack", "teams"):
            for c in b["alerts"][0].get(kind, []):
                if c not in (webhooks.get(kind) or {}):
                    warnings.append(f"{kind} channel {c!r} has no webhook in this environment's KCBUDGET_WEBHOOKS yet")
    if errors:
        raise spec.SpecError(errors)
    doc["budgets"] = trial["budgets"]
    pools = spec.pool_usage(doc)
    md = [f"Budget request from Microsoft Forms{' (response ' + str(payload['responseId']) + ')' if payload.get('responseId') else ''}.", "",
          "| Field | Value |", "|---|---|",
          f"| Budget | `{b['name']}`{' (replaces existing)' if replaced else ''} |",
          f"| Requested by | {payload.get('requester', '—')} |",
          f"| Team / cost center | {b['owner']} / {b.get('costCenter', '—')} |",
          "| Scope | " + "; ".join(k + ": " + ", ".join(v) for k, v in b["values"].items()) + " |",
          f"| Limit | ${b['spendLimit']:,} {b['interval']} (resets on day {b['intervalDay']}) |",
          f"| Alerts at | {', '.join(str(a['percentage']) + '%' for a in b['alerts'])} |",
          f"| Notify | {', '.join(b['alerts'][0].get('emails', [])) or '—'}; Slack: {', '.join(b['alerts'][0].get('slack', [])) or '—'}; Teams: {', '.join(b['alerts'][0].get('teams', [])) or '—'} |",
          f"| Justification | {(payload.get('justification') or '—').replace(chr(10), ' ')} |"]
    if b.get("pool") in pools:
        u = pools[b["pool"]]
        md.append(f"| Pool `{b['pool']}` after this | ${u['allocated']:,.2f} of ${u['monthlyLimit']:,.2f} per month |")
    if warnings:
        md += ["", "**Warnings**", ""] + [f"- {w}" for w in warnings]
    md += ["", "Merging this PR deploys the budget to Kubecost through the budgets workflow."]
    return b, replaced, "\n".join(md) + "\n"
