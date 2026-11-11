"""Notification channels: slack, teams, webhook, email.

Routing rule: owners receive ONLY the approval request. Everything about the action (work orders,
disapproval FYIs, scheduled / applied / failed / stale / expired) goes to the actioning team.

Secrets come from the environment:
  Slack    KCRESIZE_SLACK_OWNER_WEBHOOK, KCRESIZE_SLACK_ACTION_WEBHOOK   (incoming webhooks)
  Teams    KCRESIZE_TEAMS_OWNER_WEBHOOK, KCRESIZE_TEAMS_ACTION_WEBHOOK   (Workflows webhooks)
  Webhook  KCRESIZE_WEBHOOK_URL, KCRESIZE_WEBHOOK_SECRET (HMAC-SHA256 -> X-KC-Signature)
  Email    KCRESIZE_SMTP_HOST, KCRESIZE_SMTP_PORT (587), KCRESIZE_SMTP_USER, KCRESIZE_SMTP_PASSWORD,
           KCRESIZE_SMTP_FROM, KCRESIZE_SMTP_TLS (starttls|ssl|none)
Per-namespace owner channels: config owners.<ns>.slack / .teams name an ENV VAR holding the URL.
KCRESIZE_INTERACTIVE=1 adds buttons (requires gateway/). KCRESIZE_NOTIFY_DRY_RUN=1 writes payloads
to .kcresize/outbox/ instead of sending.
"""
import hashlib
import hmac
import json
import os
import smtplib
import ssl
import time
import urllib.request
from email.message import EmailMessage

from . import report

OUTBOX = os.environ.get("KCRESIZE_OUTBOX", ".kcresize/outbox")


def _env(name):
    return os.environ.get(name, "").strip()


def _dry():
    return _env("KCRESIZE_NOTIFY_DRY_RUN").lower() in ("1", "true", "yes")


def _interactive():
    return _env("KCRESIZE_INTERACTIVE").lower() in ("1", "true", "yes")


def _outbox(kind, payload):
    os.makedirs(OUTBOX, exist_ok=True)
    path = os.path.join(OUTBOX, f"{time.time_ns():020d}-{kind}.json")   # sortable, unique
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


def post_json(url, payload, headers=None, kind="post"):
    if _dry():
        return f"dry-run -> {_outbox(kind, {'url': url[:40] + '...', 'headers': headers or {}, 'payload': payload})}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return f"HTTP {r.status}"
    except Exception as e:     # a notification failure never breaks the workflow
        return f"failed: {e}"


def send_email(to, subject, html_body, text_body):
    to = [t for t in to if t]
    if not to:
        return "no recipients"
    if _dry():
        return f"dry-run -> {_outbox('email', {'to': to, 'subject': subject, 'html': html_body, 'text': text_body})}"
    host = _env("KCRESIZE_SMTP_HOST")
    if not host:
        return "skipped (KCRESIZE_SMTP_HOST not set)"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, _env("KCRESIZE_SMTP_FROM") or _env("KCRESIZE_SMTP_USER"), ", ".join(to)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    port, mode = int(_env("KCRESIZE_SMTP_PORT") or 587), (_env("KCRESIZE_SMTP_TLS") or "starttls").lower()
    try:
        if mode == "ssl":
            s = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30)
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            if mode == "starttls":
                s.starttls(context=ssl.create_default_context())
        if _env("KCRESIZE_SMTP_USER"):
            s.login(_env("KCRESIZE_SMTP_USER"), _env("KCRESIZE_SMTP_PASSWORD"))
        s.send_message(msg)
        s.quit()
        return f"sent to {len(to)}"
    except Exception as e:
        return f"failed: {e}"


def webhook(event, audience, p, link="", extra=None):
    url = _env("KCRESIZE_WEBHOOK_URL")
    if not url:
        return None
    body = {"event": event, "audience": audience, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workload": p["id"], "plan": p.get("planId"), "status": p["status"], "link": link,
            "proposal": {k: v for k, v in p.items() if not k.startswith("_")}, **(extra or {})}
    headers = {}
    if _env("KCRESIZE_WEBHOOK_SECRET"):
        raw = json.dumps(body).encode()
        headers["X-KC-Signature"] = "sha256=" + hmac.new(_env("KCRESIZE_WEBHOOK_SECRET").encode(), raw, hashlib.sha256).hexdigest()
    return post_json(url, body, headers, kind=f"webhook-{event}")


def owner_request(cfg, p, link=""):
    """The only message an owner ever gets from kcresize."""
    out = {}
    chans = cfg.channels("owner")
    owner = cfg.owner(p["namespace"])
    if "email" in chans:
        s, h, t = report.email_owner_request(p, link)
        out["email"] = send_email(owner.get("email", []), s, h, t)
    if "slack" in chans:
        url = _env(owner.get("slack", "")) if owner.get("slack") else _env("KCRESIZE_SLACK_OWNER_WEBHOOK")
        if url:
            out["slack"] = post_json(url, report.slack_owner_request(p, link, _interactive()), kind="slack-owner")
    if "teams" in chans:
        url = _env(owner.get("teams", "")) if owner.get("teams") else _env("KCRESIZE_TEAMS_OWNER_WEBHOOK")
        if url:
            out["teams"] = post_json(url, report.teams_owner_request(p, link, _interactive()), kind="teams-owner")
    if "webhook" in chans:
        out["webhook"] = webhook("approval_requested", "owner", p, link)
    return out


def actioning(cfg, p, kind, detail="", link=""):
    """kind: work_order | disapproved | scheduled | applied | failed | declined | stale | expired."""
    out = {}
    chans = cfg.channels("actioning")
    if "email" in chans:
        s, h, t = report.email_actioning(p, kind, detail, link)
        out["email"] = send_email(cfg.actioning.get("email", []), s, h, t)
    if "slack" in chans and _env("KCRESIZE_SLACK_ACTION_WEBHOOK"):
        out["slack"] = post_json(_env("KCRESIZE_SLACK_ACTION_WEBHOOK"), report.slack_actioning(p, kind, detail, link, _interactive()), kind=f"slack-action-{kind}")
    if "teams" in chans and _env("KCRESIZE_TEAMS_ACTION_WEBHOOK"):
        out["teams"] = post_json(_env("KCRESIZE_TEAMS_ACTION_WEBHOOK"), report.teams_actioning(p, kind, detail, link), kind=f"teams-action-{kind}")
    if "webhook" in chans:
        out["webhook"] = webhook(kind, "actioning", p, link, {"detail": detail})
    return out
