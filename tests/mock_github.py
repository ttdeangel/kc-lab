"""Minimal in-process GitHub REST stand-in: issues, comments, labels, assignees, team memberships."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Store:
    def __init__(self):
        self.issues, self.comments, self.labels = {}, [], set()
        self.teams = {("acme", "platform"): {"ops-bob"}}
        self.dispatches = []


G = Store()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = u.path.strip("/").split("/")
        if parts[-1] == "issues":
            items = [i for i in G.issues.values() if (q.get("state", "open") == "all" or i["state"] == q.get("state", "open"))
                     and all(l in [x["name"] for x in i["labels"]] for l in q.get("labels", "").split(",") if l)]
            return self.send(200, items)
        if len(parts) >= 5 and parts[-2] == "issues":
            i = G.issues.get(int(parts[-1]))
            return self.send(200, i) if i else self.send(404, {})
        if "teams" in parts and "memberships" in parts:   # /orgs/{org}/teams/{team}/memberships/{user}
            org, team, user = parts[1], parts[3], parts[5]
            return self.send(200, {"state": "active"}) if user in G.teams.get((org, team), set()) else self.send(404, {})
        return self.send(404, {})

    def do_POST(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        b = self._body()
        if parts[-1] == "dispatches":
            G.dispatches.append(b)
            self.send_response(204)
            self.end_headers()
            return
        if parts[-1] == "labels":
            if b["name"] in G.labels:
                return self.send(422, {})
            G.labels.add(b["name"])
            return self.send(201, b)
        if parts[-1] == "issues":
            n = len(G.issues) + 1
            G.issues[n] = {"number": n, "title": b["title"], "body": b["body"], "state": "open",
                           "labels": [{"name": l} for l in b.get("labels", [])], "assignees": []}
            return self.send(201, G.issues[n])
        if parts[-1] == "comments":
            G.comments.append((int(parts[-2]), b["body"]))
            return self.send(201, b)
        if parts[-1] == "assignees":
            G.issues[int(parts[-2])]["assignees"] = b["assignees"]
            return self.send(201, {})
        return self.send(404, {})

    def do_PATCH(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        b = self._body()
        i = G.issues[int(parts[-1])]
        if "body" in b:
            i["body"] = b["body"]
        if "labels" in b:
            i["labels"] = [{"name": l} for l in b["labels"]]
        if "state" in b:
            i["state"] = b["state"]
        return self.send(200, i)


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
