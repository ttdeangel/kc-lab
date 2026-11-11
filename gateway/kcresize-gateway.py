#!/usr/bin/env python3
"""kcresize-gateway: turn Slack buttons / Teams Workflow responses / signed webhooks into GitHub
repository_dispatch events. It only checks that a request is authentic; WHO may approve or apply is
decided by the kcresize workflows in the repository (config owners / actioning).

  POST /slack/interactions   Slack interactivity Request URL       (SLACK_SIGNING_SECRET)
  POST /webhook              {event, workload, plan?, actor, reason?}  X-KC-Signature: sha256=<HMAC(body)>
                             or X-KC-Token: <GATEWAY_TOKEN>  (Teams Workflows, ServiceNow, Jira)
  GET  /healthz

Events forwarded: approve | approve_force | disapprove (owners), apply | decline (actioning team).
Slack actors are sent as their Slack user ID (list those IDs under owners.<ns>.ids / actioning.ids).

Env: GATEWAY_HOST=0.0.0.0  GITHUB_REPOSITORY=org/repo  GITHUB_TOKEN=<token with repo scope (fine-grained: Contents read/write)>
     SLACK_SIGNING_SECRET  GATEWAY_SECRET  GATEWAY_TOKEN  GATEWAY_PORT=8088  GITHUB_API_URL
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
SLACK = os.environ.get("SLACK_SIGNING_SECRET", "")
SECRET = os.environ.get("GATEWAY_SECRET", "")
GTOKEN = os.environ.get("GATEWAY_TOKEN", "")
PORT = int(os.environ.get("GATEWAY_PORT", "8088"))
HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
EVENTS = {"approve", "approve_force", "disapprove", "apply", "decline"}
SLACK_ACTIONS = {"kc_approve": "approve", "kc_approve_force": "approve_force", "kc_disapprove": "disapprove",
                 "kc_apply": "apply", "kc_decline": "decline"}


def log(m):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), m, flush=True)


def dispatch(payload):
    """Send repository_dispatch(event_type=kcresize). GitHub returns 204."""
    req = urllib.request.Request(f"{API}/repos/{REPO}/dispatches", method="POST",
                                 data=json.dumps({"event_type": "kcresize", "client_payload": payload}).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        log(f"dispatch failed: {e.code} {e.read()[:200]}")
        return e.code


def validate(m):
    ev = m.get("event")
    if ev not in EVENTS:
        return f"unknown event {ev}"
    if "/" not in m.get("workload", ""):
        return "workload must be <namespace>/<deployment>"
    if ev == "disapprove" and not (m.get("reason") or "").strip():
        return "A reason is required to disapprove."
    if not m.get("actor"):
        return "actor is required"
    return None


def post(url, obj):
    try:
        urllib.request.urlopen(urllib.request.Request(url, json.dumps(obj).encode(), {"Content-Type": "application/json"}), timeout=10)
    except Exception as e:
        log(f"reply failed: {e}")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.reply(200 if self.path == "/healthz" else 404, {"ok": self.path == "/healthz"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if self.path == "/slack/interactions":
            return self.slack(body)
        if self.path == "/webhook":
            return self.webhook(body)
        self.reply(404, {"error": "not found"})

    def webhook(self, body):
        sig, ok = self.headers.get("X-KC-Signature", ""), False
        if SECRET and sig.startswith("sha256="):
            ok = hmac.compare_digest(sig[7:], hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest())
        if not ok and GTOKEN:
            ok = hmac.compare_digest(self.headers.get("X-KC-Token", ""), GTOKEN)
        if not ok:
            return self.reply(401, {"error": "unauthorized"})
        try:
            m = json.loads(body)
        except ValueError:
            return self.reply(400, {"error": "invalid JSON"})
        err = validate(m)
        if err:
            return self.reply(400, {"ok": False, "message": err})
        code = dispatch({k: m.get(k, "") for k in ("event", "workload", "plan", "actor", "reason")})
        log(f"webhook {m['event']} {m['workload']} by {m['actor']} -> dispatch {code}")
        self.reply(202 if code == 204 else 502, {"ok": code == 204, "message": "queued" if code == 204 else f"GitHub dispatch failed ({code})"})

    def slack(self, body):
        ts, sig = self.headers.get("X-Slack-Request-Timestamp", "0"), self.headers.get("X-Slack-Signature", "")
        if not SLACK or abs(time.time() - int(ts or 0)) > 300:
            return self.reply(401, {"error": "unauthorized"})
        mine = "v0=" + hmac.new(SLACK.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mine, sig):
            return self.reply(401, {"error": "unauthorized"})
        p = json.loads(urllib.parse.parse_qs(body.decode())["payload"][0])
        act = (p.get("actions") or [{}])[0]
        ev = SLACK_ACTIONS.get(act.get("action_id"))
        self.reply(200, {})     # Slack needs an answer within 3 s
        if not ev:
            return
        plan, workload = act.get("value", "|").split("|", 1)
        vals = (p.get("state") or {}).get("values") or {}
        reason = ((vals.get(f"reason|{plan}|{workload}") or {}).get("reason") or {}).get("value") or ""
        m = {"event": ev, "workload": workload, "plan": plan, "actor": (p.get("user") or {}).get("id", ""), "reason": reason}

        def work():
            err = validate(m)
            if err:
                text = f":warning: {err}"
            else:
                code = dispatch(m)
                text = (":white_check_mark: Thanks, your decision is recorded." if ev in ("approve", "approve_force", "disapprove")
                        else f":white_check_mark: {ev} request sent.") if code == 204 else f":warning: Could not record it (GitHub {code})."
            if p.get("response_url"):   # private reply to the person who clicked
                post(p["response_url"], {"response_type": "ephemeral", "replace_original": False, "text": text})
        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    if not (REPO and TOKEN):
        sys.exit("GITHUB_REPOSITORY and GITHUB_TOKEN are required")
    if not (SLACK or SECRET or GTOKEN):
        sys.exit("Set SLACK_SIGNING_SECRET, GATEWAY_SECRET and/or GATEWAY_TOKEN")
    log(f"kcresize-gateway :{PORT} -> {REPO}  slack={'on' if SLACK else 'off'} hmac={'on' if SECRET else 'off'} token={'on' if GTOKEN else 'off'}")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
