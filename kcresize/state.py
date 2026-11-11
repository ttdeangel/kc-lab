"""Where proposals and decisions live.

github : one GitHub Issue per workload proposal. Owners decide with /approve or /disapprove comments,
         labels show status (kcresize:<status>), the machine-readable record sits in a hidden block
         in the issue body, and the history table in the body is the audit trail. Status changes after
         the owner's decision only edit the body and labels (no comments), so owners are not notified.
file   : a JSON file (local runs, tests, lab without GitHub).
"""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .planner import now

OPEN_STATES = {"pending", "approved", "scheduled"}
MARK = "kcresize-state"


class FileState:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def _load(self):
        if not os.path.exists(self.path):
            return {"proposals": {}, "comments": []}
        with open(self.path) as f:
            return json.load(f)

    def _save(self, d):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, self.path)

    def list(self, statuses=None):
        ps = list(self._load()["proposals"].values())
        return [p for p in ps if not statuses or p["status"] in statuses]

    def get(self, pid):
        return self._load()["proposals"].get(pid or "")

    def get_by_issue(self, number):
        return None

    def save(self, p, comment=None):
        d = self._load()
        d["proposals"][p["id"]] = p
        if comment:
            d["comments"].append({"time": now(), "id": p["id"], "comment": comment})
        self._save(d)
        return p

    def comment(self, p, text):
        d = self._load()
        d["comments"].append({"time": now(), "id": p["id"] if isinstance(p, dict) else p, "comment": text})
        self._save(d)

    def link(self, p):
        return ""


class GitHubState:
    LABELS = [("kcresize", "0e8a16"), ("kcresize:pending", "fbca04"), ("kcresize:approved", "1d76db"),
              ("kcresize:scheduled", "5319e7"), ("kcresize:applied", "0e8a16"), ("kcresize:rejected", "b60205"),
              ("kcresize:declined", "b60205"), ("kcresize:failed", "d93f0b"), ("kcresize:stale", "c5def5"),
              ("kcresize:expired", "c5def5")]

    def __init__(self, repo=None, token=None, api=None):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
        self.token = token or os.environ.get("KCRESIZE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        self.api = (api or os.environ.get("GITHUB_API_URL", "https://api.github.com")).rstrip("/")
        self.web = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        if not self.repo or not self.token:
            raise SystemExit("GitHub state needs GITHUB_REPOSITORY and GITHUB_TOKEN")
        self._cache = None
        self._labels_ok = False

    def _req(self, method, path, body=None, params=None):
        url = f"{self.api}{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, {"message": e.read().decode()[:300]}

    def _ensure_labels(self):
        if not self._labels_ok:
            for name, color in self.LABELS:
                self._req("POST", f"/repos/{self.repo}/labels", {"name": name, "color": color})   # 422 = exists
            self._labels_ok = True

    @staticmethod
    def decode(body):
        m = re.search(rf"<!-- {MARK}:([A-Za-z0-9+/=]+) -->", body or "")
        return json.loads(base64.b64decode(m.group(1))) if m else None

    @staticmethod
    def encode(p):
        return f"<!-- {MARK}:{base64.b64encode(json.dumps(p, separators=(',', ':')).encode()).decode()} -->"

    def _issues(self, state="open"):
        out, page = [], 1
        while True:
            code, items = self._req("GET", f"/repos/{self.repo}/issues", params={"labels": "kcresize", "state": state, "per_page": 100, "page": page})
            if code != 200:
                raise SystemExit(f"GitHub issues list failed: {code} {items}")
            out += [i for i in items if "pull_request" not in i]
            if len(items) < 100:
                return out
            page += 1

    def _all(self):
        if self._cache is None:
            self._cache = {}
            for i in self._issues():
                p = self.decode(i.get("body"))
                if p:
                    p["_issue"] = i["number"]
                    self._cache[p["id"]] = p
        return self._cache

    def list(self, statuses=None):
        return [p for p in self._all().values() if not statuses or p["status"] in statuses]

    def get(self, pid):
        return self._all().get(pid or "")

    def get_by_issue(self, number):
        code, i = self._req("GET", f"/repos/{self.repo}/issues/{number}")
        if code != 200:
            return None
        p = self.decode(i.get("body"))
        if p:
            p["_issue"] = number
        return p

    def save(self, p, comment=None):
        from .report import issue_markdown
        number = p.get("_issue")
        clean = {k: v for k, v in p.items() if not k.startswith("_")}
        body = issue_markdown(p) + "\n\n" + self.encode(clean)
        labels = ["kcresize", f"kcresize:{p['status']}"]
        if number:
            self._req("PATCH", f"/repos/{self.repo}/issues/{number}",
                      {"body": body, "labels": labels, "state": "open" if p["status"] in OPEN_STATES else "closed"})
        else:
            self._ensure_labels()
            title = f"Kubecost resize: {p['id']} (save ~${p['monthlySavings']}/mo)"
            code, i = self._req("POST", f"/repos/{self.repo}/issues", {"title": title, "body": body, "labels": labels})
            if code != 201:
                raise SystemExit(f"GitHub issue create failed: {code} {i}")
            number = p["_issue"] = i["number"]
            gh = [u for u in p.get("owners", {}).get("github", []) if not u.startswith("@")]
            if gh:   # best effort: assignees must be collaborators; assignment emails the owner via GitHub
                self._req("POST", f"/repos/{self.repo}/issues/{number}/assignees", {"assignees": gh})
        if comment:
            self.comment(p, comment)
        self._all()[p["id"]] = p
        return p

    def comment(self, p, text):
        number = p.get("_issue") if isinstance(p, dict) else p
        if number:
            self._req("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": text})

    def link(self, p):
        return f"{self.web}/{self.repo}/issues/{p['_issue']}" if p.get("_issue") else ""

    def is_member(self, user, entries):
        """Listed directly, or a member of a listed '@org/team' (team checks need a token with read:org)."""
        for e in entries or []:
            if e.lower() == (user or "").lower():
                return True
            if e.startswith("@") and "/" in e:
                org, team = e[1:].split("/", 1)
                code, m = self._req("GET", f"/orgs/{org}/teams/{team}/memberships/{user}")
                if code == 200 and m.get("state") == "active":
                    return True
        return False


def open_state(cfg):
    if cfg.data["state"] == "file":
        return FileState(cfg.data["stateFile"])
    return GitHubState()


def is_member(state, user, entries):
    if hasattr(state, "is_member"):
        return state.is_member(user, entries)
    return any(e.lower() == (user or "").lower() for e in entries or [])
