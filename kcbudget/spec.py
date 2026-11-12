"""Budget files (budgets/<env>.json): load, validate, resolve channels, convert to Kubecost bodies.

A budget file holds non-secret settings only. Slack and Teams destinations are referenced by NAME
("slack": ["finops"]); the URLs come from the KCBUDGET_WEBHOOKS secret, a JSON object:
    {"slack": {"finops": "https://hooks.slack.com/..."}, "teams": {"platform": "https://...logic.azure.com/..."}}
so the file can live in the repository and be reviewed in pull requests.
"""
import json
import os
import re

KNOWN_SCOPE_KEYS = ("namespace", "cluster")       # verified in the Budget API docs; others pass through with a warning
INTERVALS = ("weekly", "monthly")
KINDS = ("soft", "hard")
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
WEEKS_PER_MONTH = 52 / 12


class SpecError(ValueError):
    def __init__(self, errors):
        super().__init__("\n".join(errors))
        self.errors = errors


def load(path):
    with open(path) as f:
        doc = json.load(f)
    doc.setdefault("managedPrefix", "kcb-")
    doc.setdefault("defaults", {})
    doc.setdefault("pools", {})
    doc.setdefault("budgets", [])
    return doc


def save(doc, path):
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")


def effective(doc, b):
    """A budget with file defaults applied (alerts are replaced, not merged)."""
    out = {k: v for k, v in doc.get("defaults", {}).items()}
    out.update(b)
    out.setdefault("kind", "soft")
    out.setdefault("interval", "monthly")
    out.setdefault("intervalDay", 1)
    out.setdefault("alerts", [])
    return out


def monthly_equivalent(b):
    return float(b["spendLimit"]) * (WEEKS_PER_MONTH if b["interval"] == "weekly" else 1)


def webhooks_from_env():
    raw = os.environ.get("KCBUDGET_WEBHOOKS", "").strip()
    if not raw:
        return {"slack": {}, "teams": {}}
    try:
        w = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SpecError([f"KCBUDGET_WEBHOOKS is not valid JSON: {e}"])
    return {"slack": w.get("slack") or {}, "teams": w.get("teams") or {}}


def validate(doc):
    """Return (errors, warnings). Errors block plan/apply; warnings are printed."""
    errors, warnings = [], []
    prefix = doc.get("managedPrefix", "kcb-")
    domains = [d.lower() for d in doc.get("allowedEmailDomains", [])]
    seen, pool_totals = set(), {}
    for p, pv in doc.get("pools", {}).items():
        if not isinstance(pv.get("monthlyLimit"), (int, float)) or pv["monthlyLimit"] <= 0:
            errors.append(f"pool {p}: monthlyLimit must be a positive number")
    for i, raw in enumerate(doc.get("budgets", [])):
        b = effective(doc, raw)
        n = b.get("name", "")
        where = f"budget #{i + 1} ({n or 'unnamed'})"
        if not NAME_RE.match(n or ""):
            errors.append(f"{where}: name must be lowercase letters, digits and '-' (max 63)")
        if not n.startswith(prefix):
            errors.append(f"{where}: name must start with the managed prefix '{prefix}'")
        if n in seen:
            errors.append(f"{where}: duplicate name")
        seen.add(n)
        vals = b.get("values")
        if not isinstance(vals, dict) or not vals:
            errors.append(f"{where}: values must be an object such as {{\"namespace\": [\"payments\"]}}")
        else:
            for k, v in vals.items():
                if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
                    errors.append(f"{where}: values.{k} must be a non-empty list of names")
                if k not in KNOWN_SCOPE_KEYS:
                    warnings.append(f"{where}: values.{k} is not a documented budget scope; check it with `kcbudget doctor` first")
        if b["interval"] not in INTERVALS:
            errors.append(f"{where}: interval must be weekly or monthly")
        day = b.get("intervalDay")
        if b["interval"] == "weekly" and not (isinstance(day, int) and 0 <= day <= 6):
            errors.append(f"{where}: weekly intervalDay is 0-6 (0 = Sunday)")
        if b["interval"] == "monthly" and not (isinstance(day, int) and 1 <= day <= 28):
            errors.append(f"{where}: monthly intervalDay is 1-28")
        if b["kind"] not in KINDS:
            errors.append(f"{where}: kind must be soft or hard")
        lim = b.get("spendLimit")
        if not isinstance(lim, (int, float)) or isinstance(lim, bool) or lim <= 0:
            errors.append(f"{where}: spendLimit must be a positive number")
            lim = None
        pcts = set()
        if not b["alerts"]:
            warnings.append(f"{where}: no alerts; nobody is notified when it is exceeded")
        for a in b["alerts"]:
            pc = a.get("percentage")
            if not isinstance(pc, int) or not 1 <= pc <= 500:
                errors.append(f"{where}: alert percentage must be an integer 1-500")
            elif pc in pcts:
                errors.append(f"{where}: duplicate alert percentage {pc}")
            pcts.add(pc)
            if not (a.get("emails") or a.get("slack") or a.get("teams")):
                errors.append(f"{where}: alert at {pc}% has no recipients (emails, slack or teams)")
            for e in a.get("emails", []):
                if not EMAIL_RE.match(e):
                    errors.append(f"{where}: invalid email {e!r}")
                elif domains and e.split("@", 1)[1].lower() not in domains:
                    errors.append(f"{where}: {e} is outside allowedEmailDomains")
            for kind in ("slack", "teams"):
                for c in a.get(kind, []):
                    if not isinstance(c, str) or not CHANNEL_RE.match(c) or c.startswith("http"):
                        errors.append(f"{where}: {kind} entries are channel NAMES from KCBUDGET_WEBHOOKS, not URLs ({c!r})")
        pool = b.get("pool")
        if pool:
            if pool not in doc.get("pools", {}):
                errors.append(f"{where}: unknown pool {pool!r}")
            elif lim:
                pool_totals[pool] = pool_totals.get(pool, 0) + monthly_equivalent(b)
    for p, total in pool_totals.items():
        cap = doc["pools"][p].get("monthlyLimit") or 0
        if cap and total > cap + 1e-6:
            errors.append(f"pool {p}: budgets allocate ${total:,.2f}/month, more than its monthlyLimit ${cap:,.2f}")
    return errors, warnings


def pool_usage(doc):
    out = {p: {"monthlyLimit": v.get("monthlyLimit", 0), "allocated": 0.0, "budgets": 0} for p, v in doc.get("pools", {}).items()}
    for raw in doc.get("budgets", []):
        b = effective(doc, raw)
        if b.get("pool") in out and isinstance(b.get("spendLimit"), (int, float)):
            out[b["pool"]]["allocated"] += monthly_equivalent(b)
            out[b["pool"]]["budgets"] += 1
    return out


def to_kubecost(doc, raw, webhooks, strict=False, problems=None):
    """Kubecost request body for one budget. Unknown channel names: error if strict, else dropped with a warning."""
    b = effective(doc, raw)
    problems = problems if problems is not None else []
    actions = []
    for a in sorted(b["alerts"], key=lambda x: x["percentage"]):
        act = {"percentage": a["percentage"]}
        if a.get("emails"):
            act["emails"] = sorted(a["emails"])
        for kind, field in (("slack", "slackWebhooks"), ("teams", "msTeamsWebhooks")):
            urls = []
            for c in a.get(kind, []):
                u = (webhooks.get(kind) or {}).get(c)
                if u:
                    urls.append(u)
                else:
                    problems.append(f"{b['name']}: {kind} channel {c!r} is not in KCBUDGET_WEBHOOKS")
            if urls:
                act[field] = sorted(urls)
        if len(act) > 1:
            actions.append(act)
    if strict and problems:
        raise SpecError(problems)
    body = {"name": b["name"], "values": {k: sorted(v) for k, v in b["values"].items()},
            "kind": b["kind"], "interval": b["interval"], "intervalDay": b["intervalDay"],
            "spendLimit": b["spendLimit"], "actions": actions}
    if b.get("type"):
        body["type"] = b["type"]
    return body


def normalize(budget):
    """Comparable form of a Kubecost budget (from the API or from to_kubecost)."""
    acts = []
    for a in budget.get("actions") or []:
        n = {"percentage": a.get("percentage")}
        for f in ("emails", "slackWebhooks", "msTeamsWebhooks"):
            if a.get(f):
                n[f] = sorted(a[f])
        acts.append(n)
    vals = budget.get("values") or {}
    return {
        "values": {k: sorted(v) if isinstance(v, list) else v for k, v in vals.items() if v},
        "kind": budget.get("kind") or "soft",
        "interval": budget.get("interval"),
        "intervalDay": budget.get("intervalDay"),
        "spendLimit": float(budget.get("spendLimit") or 0),
        "actions": sorted(acts, key=lambda a: a.get("percentage") or 0),
    }


def mask(s):
    """Hide webhook URLs in logs and summaries."""
    if isinstance(s, str) and s.startswith("http"):
        m = re.match(r"^(https?://[^/]+)", s)
        return (m.group(1) if m else "https://") + "/***"
    if isinstance(s, list):
        return [mask(x) for x in s]
    if isinstance(s, dict):
        return {k: mask(v) for k, v in s.items()}
    return s
