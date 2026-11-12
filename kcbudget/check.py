"""Repo-side budget checker: read budgets FROM KUBECOST (the source of truth), measure spend for the
current period with the Allocation API, and notify each budget's own recipients when a threshold is
crossed.

Why it exists next to Kubecost's native budget alerts:
  - Microsoft Teams: Office 365 connectors were retired in May 2026, so Teams needs a Workflows URL and
    an Adaptive Card. Kubecost's native Teams post may not render there; this checker sends a card.
  - Email: native budget emails need SMTP configured inside Kubecost; this uses KCRESIZE_SMTP_* runtime settings on the runner.
  - A status table of every budget in the job summary.
Pick which channels it delivers with --channels so nothing is sent twice (see docs/budgets.md).

State (which thresholds were already sent this period) is a small JSON file; the workflow keeps it
in the Actions cache.
"""
import datetime as dt
import json
import os

from kcresize import notify, report

from . import spec

UTC = dt.timezone.utc


def period_start(interval, day, now):
    now = now.astimezone(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if interval == "weekly":
        py_dow = ((day or 0) - 1) % 7                 # budget day: 0 = Sunday; Python: Monday = 0
        return midnight - dt.timedelta(days=(now.weekday() - py_dow) % 7)
    d = max(1, min(int(day or 1), 28))
    if now.day >= d:
        return midnight.replace(day=d)
    first = midnight.replace(day=1)
    prev = first - dt.timedelta(days=1)
    return prev.replace(day=d)


def kubecost_filter(values):
    parts = []
    for k in ("cluster", "namespace"):
        if values.get(k):
            parts.append(f"{k}:" + ",".join(f'"{v}"' for v in values[k]))
    unknown = [k for k in values if k not in ("cluster", "namespace")]
    return "+".join(parts), unknown


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def evaluate(api, now=None, prefix=""):
    """Spend vs. limit for every budget in Kubecost. Returns a list of dicts."""
    now = now or dt.datetime.now(UTC)
    rows = []
    for b in api.list():
        name = b.get("name") or ""
        if prefix and not name.startswith(prefix):
            continue
        n = spec.normalize(b)
        start = period_start(n["interval"], n["intervalDay"], now)
        filt, unknown = kubecost_filter(n["values"])
        row = {"name": name, "id": b.get("id"), "values": n["values"], "interval": n["interval"], "limit": n["spendLimit"],
               "period": iso(start), "actions": n["actions"], "kind": n["kind"]}
        if unknown or not filt:
            row["error"] = f"scope {', '.join(unknown) or '(empty)'} not supported by the checker"
        else:
            try:
                row["spend"] = api.spend(filt, iso(start), iso(now))
                row["pct"] = round(row["spend"] / n["spendLimit"] * 100, 1) if n["spendLimit"] else 0
            except Exception as e:
                row["error"] = str(e)
        rows.append(row)
    return rows


def _message(row, threshold, env):
    scope = "; ".join(f"{k}: {', '.join(v)}" for k, v in row["values"].items())
    over = row["pct"] >= 100
    title = f"Budget {'exceeded' if over else 'alert'}: {row['name']} at {row['pct']:.0f}% ({env})"
    facts = [("Scope", scope), ("Spend this period", f"${row['spend']:,.2f}"), ("Limit", f"${row['limit']:,.2f} {row['interval']}"),
             ("Threshold crossed", f"{threshold}%"), ("Period start", row["period"])]
    return title, facts


def notify_row(row, crossed, channels, env, link=""):
    """Send one message per destination for the highest newly crossed threshold."""
    top = max(crossed, key=lambda a: a["percentage"])
    emails = sorted({e for a in crossed for e in a.get("emails", [])})
    slack = sorted({u for a in crossed for u in a.get("slackWebhooks", [])})
    teams = sorted({u for a in crossed for u in a.get("msTeamsWebhooks", [])})
    title, facts = _message(row, top["percentage"], env)
    text = "\n".join(f"{k}: {v}" for k, v in facts)
    out = {}
    if "email" in channels and emails:
        html = f"<h3>{report._h(title)}</h3><table>" + "".join(
            f"<tr><td><b>{report._h(k)}</b></td><td>{report._h(v)}</td></tr>" for k, v in facts) + "</table>"
        if link:
            html += f'<p><a href="{report._h(link)}">Open in Kubecost</a></p>'
        out["email"] = notify.send_email(emails, "[Kubecost] " + title, html, title + "\n\n" + text)
    if "slack" in channels:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n" + "\n".join(f"• *{k}:* {v}" for k, v in facts)}}]
        if link:
            blocks.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open in Kubecost"}, "url": link}]})
        out["slack"] = [notify.post_json(u, {"text": title, "blocks": blocks}, kind="slack-budget") for u in slack]
    if "teams" in channels:
        actions = [{"type": "Action.OpenUrl", "title": "Open in Kubecost", "url": link}] if link else None
        card = report.teams_card(title, facts, [], actions)
        out["teams"] = [notify.post_json(u, card, kind="teams-budget") for u in teams]
    return out


def run(api, state_path, channels, env="lab", now=None, prefix="", link=""):
    now = now or dt.datetime.now(UTC)
    state = load_state(state_path)
    rows = evaluate(api, now, prefix)
    for row in rows:
        if "pct" not in row:
            continue
        st = state.get(row["name"]) or {}
        if st.get("period") != row["period"] or st.get("limit") != row["limit"]:
            st = {"period": row["period"], "limit": row["limit"], "sent": []}
        crossed = [a for a in row["actions"] if row["pct"] >= (a.get("percentage") or 0) and a.get("percentage") not in st["sent"]]
        if crossed:
            row["notified"] = notify_row(row, crossed, channels, env, link)
            st["sent"] = sorted(set(st["sent"]) | {a["percentage"] for a in crossed})
            row["newThresholds"] = sorted(a["percentage"] for a in crossed)
        state[row["name"]] = st
    live = {r["name"] for r in rows}
    state = {k: v for k, v in state.items() if k in live}
    save_state(state_path, state)
    return rows


def summary_markdown(env, rows, channels):
    over = sum(1 for r in rows if r.get("pct", 0) >= 100)
    lines = [f"### Budget check: `{env}`", "",
             f"{len(rows)} budgets · **{over} over limit** · repo-side channels: {', '.join(channels) or 'none'}", "",
             "| Budget | Scope | Spend | Limit | Used | Alerted now |", "|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: -r.get("pct", -1)):
        scope = "; ".join(f"{k}={','.join(v)}" for k, v in r["values"].items())
        if "pct" in r:
            flag = " 🔴" if r["pct"] >= 100 else (" 🟠" if r["pct"] >= 80 else "")
            lines.append(f"| `{r['name']}` | {scope} | ${r['spend']:,.2f} | ${r['limit']:,.2f} {r['interval']} | {r['pct']:.1f}%{flag} | "
                         f"{', '.join(str(p) + '%' for p in r.get('newThresholds', [])) or '—'} |")
        else:
            lines.append(f"| `{r['name']}` | {scope} | — | ${r['limit']:,.2f} | error | {r['error'][:80]} |")
    return "\n".join(lines) + "\n"
