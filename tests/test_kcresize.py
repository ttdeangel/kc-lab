"""Unit + end-to-end tests against in-process Kubecost and GitHub stand-ins.  Run: python -m unittest -v"""
import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import mock_github  # noqa: E402
import mock_kubecost  # noqa: E402
from kcresize import cli, config, planner, workflow  # noqa: E402
from kcresize.kubecost import Kubecost, maintenance_window_now  # noqa: E402
from kcresize.state import FileState, GitHubState  # noqa: E402

CFG = {
    "kubecost": {"cluster": "example-cluster"},
    "scope": {"namespaces": ["tbm-payments", "shop-prod", "shop-dev", "kc-turndown-test", "kubecost"]},
    "owners": {"tbm-payments": {"github": ["fraud-owner"], "email": ["fraud@example.com"]},
               "shop-prod": {"github": ["shop-owner"], "email": ["shop@example.com"]},
               "shop-dev": {"github": ["shop-owner"]}},
    "actioning": {"github": ["ops-alice", "@acme/platform"], "email": ["platform@example.com"]},
    "notify": {"owner": ["email", "slack", "teams", "webhook"], "actioning": ["email", "slack", "teams", "webhook"]},
}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kc_srv = mock_kubecost.serve()
        cls.gh_srv = mock_github.serve()

    def setUp(self):
        mock_kubecost.S.__init__()
        mock_github.G.__init__()
        self.tmp = tempfile.mkdtemp()
        self.outbox = os.path.join(self.tmp, "outbox")
        cfgpath = os.path.join(self.tmp, "kcresize.json")
        with open(cfgpath, "w") as f:
            json.dump(CFG, f)
        self.env = {
            "KCRESIZE_CONFIG": cfgpath, "KUBECOST_URL": f"http://127.0.0.1:{self.kc_srv.server_port}",
            "KCRESIZE_NOTIFY_DRY_RUN": "1", "KCRESIZE_OUTBOX": self.outbox,
            "KCRESIZE_SLACK_OWNER_WEBHOOK": "https://hooks.slack.test/owner", "KCRESIZE_SLACK_ACTION_WEBHOOK": "https://hooks.slack.test/action",
            "KCRESIZE_TEAMS_OWNER_WEBHOOK": "https://teams.test/owner", "KCRESIZE_TEAMS_ACTION_WEBHOOK": "https://teams.test/action",
            "KCRESIZE_WEBHOOK_URL": "https://hooks.test/kc", "KCRESIZE_WEBHOOK_SECRET": "s3cret",
            "GITHUB_REPOSITORY": "acme/platform-finops", "GITHUB_TOKEN": "t", "GITHUB_API_URL": f"http://127.0.0.1:{self.gh_srv.server_port}",
            "KCRESIZE_STATE_FILE": os.path.join(self.tmp, "state.json"),
        }
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        import kcresize.notify as n
        n.OUTBOX = self.outbox

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def outbox_items(self):
        out = []
        for f in sorted(glob.glob(os.path.join(self.outbox, "*.json"))):
            with open(f) as fh:
                out.append((os.path.basename(f).split("-", 1)[-1][:-5], json.load(fh)))
        return out


class TestUnits(Base):
    def test_units(self):
        self.assertEqual(planner.cpu_m("500m"), 500)
        self.assertEqual(planner.cpu_m("1"), 1000)
        self.assertEqual(planner.mem_mi("1Gi"), 1024)
        self.assertAlmostEqual(planner.mem_mi("512Mi"), 512)

    def test_comment_parsing(self):
        self.assertEqual(cli.parse_comment("/approve looks fine"), ("approve", False, "looks fine"))
        self.assertEqual(cli.parse_comment("/approve --force ok\nmore"), ("approve", True, "ok"))
        self.assertEqual(cli.parse_comment("/disapprove Friday peak"), ("disapprove", False, "Friday peak"))
        self.assertEqual(cli.parse_comment("please /approve"), (None, False, ""))
        self.assertEqual(cli.parse_comment("/apply"), ("apply", False, ""))

    def test_maintenance_window(self):
        w = maintenance_window_now()
        self.assertRegex(w, r"^\* \d{1,2} \* \* [0-6]\|\|\* \d{1,2} \* \* [0-6]$")

    def test_profile_param_rejected(self):
        kc = Kubecost(os.environ["KUBECOST_URL"])
        with self.assertRaises(Exception):
            kc.get("/model/savings/requestSizingV2", window="2d", profile="Production")

    def test_guardrails(self):
        cfg = config.load()
        _, props, skipped = planner.make_plan(cfg, Kubecost(cfg.kc["url"]))
        by = {p["id"]: p for p in props}
        self.assertIn("tbm-payments/fraud-scorer", by)
        self.assertEqual(by["tbm-payments/fraud-scorer"]["profile"], "Development")
        self.assertEqual(by["shop-prod/api"]["profile"], "Production")
        self.assertTrue(by["shop-prod/api"]["largeCut"])
        self.assertTrue(by["kc-turndown-test/resource-hog"]["blocked"])       # 302117m bad data
        self.assertIn("shop-dev/worker", skipped)                             # below minChangePct
        self.assertNotIn("kubecost/kubecost-frontend", by)                     # excluded namespace
        self.assertEqual(len(by["shop-prod/worker"]["containers"]), 2)
        a = by["tbm-payments/fraud-scorer"]["allocation"]
        self.assertAlmostEqual(a["monthlyCost"], round(0.46 / 1333 * 43800, 2))

    def test_namespace_label_scope(self):
        os.environ["KCRESIZE_NAMESPACES"] = ""
        cfg = config.load()
        cfg.data["scope"]["namespaces"] = []
        cfg.data["scope"]["namespaceLabel"] = "kc-lab.io/resize-approval=required"
        _, props, _ = planner.make_plan(cfg, Kubecost(cfg.kc["url"]))
        self.assertEqual({p["namespace"] for p in props}, {"shop-prod"})

    def test_read_only_demo(self):
        os.environ["KUBECOST_URL"] = "https://demo.kubecost.io"
        self.assertTrue(config.load().kc["readOnly"])
        kc = Kubecost("http://127.0.0.1:1", read_only=True)
        with self.assertRaises(Exception):
            kc.create_resize_action("x", "Development", "f", "48h", "* 1 * * 1")


class TestFlowGitHub(Base):
    """Full flow with GitHub Issues as the record."""

    def run_cli(self, *args, actor=None):
        if actor:
            os.environ["KCRESIZE_ACTOR"] = actor
        try:
            return cli.main(list(args))
        finally:
            os.environ.pop("KCRESIZE_ACTOR", None)

    def test_end_to_end(self):
        self.assertEqual(self.run_cli("doctor"), 0)
        self.assertEqual(self.run_cli("plan"), 0)
        issues = mock_github.G.issues
        titles = sorted(i["title"] for i in issues.values())
        self.assertEqual(len(issues), 4, titles)                   # fraud, checkout, shop-prod api + worker (blocked one not filed)
        st = GitHubState()
        p = st.get("tbm-payments/fraud-scorer")
        self.assertEqual(p["status"], "pending")
        fraud_issue = p["_issue"]
        self.assertIn("/disapprove <reason, required>", issues[fraud_issue]["body"])
        self.assertEqual(issues[fraud_issue]["assignees"], ["fraud-owner"])

        # owners got exactly the approval requests, on every owner channel
        kinds = [k for k, _ in self.outbox_items()]
        self.assertEqual(kinds.count("email"), 4)     # one approval request per proposal
        self.assertTrue(all(k in ("email", "slack-owner", "teams-owner", "webhook-approval_requested") for k in kinds), kinds)
        n_before = len(kinds)

        # 1) wrong person, missing reason, large cut without --force
        self.assertEqual(self.run_cli("approve", "tbm-payments/fraud-scorer", actor="ops-alice"), 3)
        self.assertEqual(self.run_cli("disapprove", "tbm-payments/checkout-api", actor="fraud-owner"), 3)
        self.assertEqual(self.run_cli("approve", "shop-prod/api", actor="shop-owner"), 3)
        # 2) owner decisions
        self.assertEqual(self.run_cli("disapprove", "tbm-payments/checkout-api", "--reason", "PCI freeze this week", actor="fraud-owner"), 0)
        self.assertEqual(self.run_cli("approve", "tbm-payments/fraud-scorer", "--reason", "traffic is flat", actor="fraud-owner"), 0)
        self.assertEqual(self.run_cli("approve", "shop-prod/api", "--force", actor="shop-owner"), 0)
        self.assertEqual(self.run_cli("disapprove", "tbm-payments/fraud-scorer", "--reason", "late", actor="fraud-owner"), 3)  # already decided

        new = self.outbox_items()[n_before:]
        self.assertTrue(all(k.startswith(("slack-action", "teams-action", "webhook-")) or k == "email" for k, _ in new))
        emails = [v for k, v in new if k == "email"]
        self.assertTrue(all(v["to"] == ["platform@example.com"] for v in emails), "owners must not get action mail")
        self.assertTrue(any("Work order" in v["subject"] for v in emails))
        self.assertTrue(any("No action needed" in v["subject"] for v in emails))
        wh = [v for k, v in new if k.startswith("webhook-")]
        self.assertTrue(all(v["headers"]["X-KC-Signature"].startswith("sha256=") for v in wh))

        # 3) only the actioning team can apply (direct user or @org/team)
        self.assertEqual(self.run_cli("apply", "tbm-payments/fraud-scorer", actor="fraud-owner"), 3)
        self.assertEqual(self.run_cli("apply", "tbm-payments/checkout-api", actor="ops-alice"), 3)     # rejected
        self.assertEqual(self.run_cli("apply", "tbm-payments/fraud-scorer", actor="ops-alice"), 0)
        self.assertEqual(self.run_cli("apply", "tbm-payments/fraud-scorer", actor="ops-alice"), 3)     # double click
        self.assertEqual(self.run_cli("decline", "shop-prod/api", "--reason", "change freeze", actor="ops-bob"), 0)  # via @acme/platform

        cfgs = mock_kubecost.S.configs
        self.assertEqual(len(cfgs), 1)
        c = cfgs[0]
        self.assertEqual(c["action"], "containerRequestRightSizing")
        self.assertEqual(c["config"]["profile"], "Development")
        self.assertEqual(c["config"]["filter"], 'cluster:"example-cluster"+namespace:"tbm-payments"+controllerName:"fraud-scorer"')

        # 4) watch: Kubecost reports success -> applied, action deleted, issue closed
        self.assertEqual(self.run_cli("watch"), 0)
        st = GitHubState()
        issue = mock_github.G.issues[fraud_issue]
        self.assertEqual(issue["state"], "closed")
        self.assertIn("kcresize:applied", [l["name"] for l in issue["labels"]])
        self.assertEqual(mock_kubecost.S.configs, [])
        rec = GitHubState.decode(issue["body"])
        self.assertEqual(rec["kubecostResult"][0]["to"], "10m/20Mi")
        self.assertEqual([h["event"] for h in rec["history"]], ["planned", "approved", "scheduled", "applied", "action-deleted"])

        # owners' issue comments: only acknowledgements of their own decisions
        owner_comments = [b for n, b in mock_github.G.comments if n == fraud_issue]
        self.assertTrue(all("@fraud-owner" in b for b in owner_comments), owner_comments)

    def test_issue_comment_event(self):
        self.run_cli("plan")
        st = GitHubState()
        n = st.get("tbm-payments/fraud-scorer")["_issue"]
        ev = os.path.join(self.tmp, "event.json")

        def comment(body, user, role):
            with open(ev, "w") as f:
                json.dump({"action": "created", "issue": {"number": n}, "comment": {"body": body, "user": {"login": user}}}, f)
            os.environ.update(GITHUB_EVENT_NAME="issue_comment", GITHUB_EVENT_PATH=ev, KCRESIZE_ROLE=role)
            try:
                return cli.main(["event"])
            finally:
                for k in ("GITHUB_EVENT_NAME", "GITHUB_EVENT_PATH", "KCRESIZE_ROLE"):
                    os.environ.pop(k, None)

        self.assertEqual(comment("just a question", "fraud-owner", "owner"), 0)        # not a command
        self.assertEqual(comment("/apply", "fraud-owner", "owner"), 0)                 # owner workflow ignores /apply
        self.assertEqual(comment("/disapprove", "fraud-owner", "owner"), 3)            # reason required -> denied + comment
        self.assertTrue(any("reason is required" in b for _, b in mock_github.G.comments))
        self.assertEqual(comment("/approve looks good", "fraud-owner", "owner"), 0)
        self.assertEqual(comment("/apply", "fraud-owner", "actioning"), 3)             # owner can't act
        self.assertEqual(comment("/apply", "ops-alice", "actioning"), 0)
        self.assertEqual(GitHubState().get("tbm-payments/fraud-scorer")["status"], "scheduled")

    def test_repository_dispatch_and_failure(self):
        mock_kubecost.S.auto_complete = "failed"
        self.run_cli("plan")
        ev = os.path.join(self.tmp, "event.json")
        for payload in ({"event": "approve", "workload": "tbm-payments/fraud-scorer", "actor": "fraud-owner", "reason": "ok"},
                        {"event": "apply", "workload": "tbm-payments/fraud-scorer", "actor": "ops-alice"}):
            with open(ev, "w") as f:
                json.dump({"action": "kcresize", "client_payload": payload}, f)
            os.environ.update(GITHUB_EVENT_NAME="repository_dispatch", GITHUB_EVENT_PATH=ev)
            try:
                self.assertEqual(cli.main(["event"]), 0)
            finally:
                os.environ.pop("GITHUB_EVENT_NAME")
        self.run_cli("watch")
        rec = GitHubState.decode(next(i["body"] for i in mock_github.G.issues.values() if "fraud-scorer" in i["title"]))
        self.assertEqual(rec["status"], "failed")
        self.assertTrue(any("FAILED" in v.get("subject", "") or "failed" in json.dumps(v).lower() for _, v in self.outbox_items()))


class TestFlowFile(Base):
    def test_file_backend_and_drift(self):
        os.environ["KCRESIZE_STATE"] = "file"
        try:
            self.assertEqual(cli.main(["plan"]), 0)
            os.environ["KCRESIZE_ACTOR"] = "fraud-owner"
            self.assertEqual(cli.main(["approve", "tbm-payments/fraud-scorer"]), 0)
            # Kubecost's recommendation moves a lot before the actioning team gets to it -> stale
            for r in mock_kubecost.S.recs:
                if r["controllerName"] == "fraud-scorer":
                    r["normalizedRecommendedRequest"] = {"cpuInMilliCores": 200, "memoryInMiB": 256}
            os.environ["KCRESIZE_ACTOR"] = "ops-alice"
            self.assertEqual(cli.main(["apply", "tbm-payments/fraud-scorer"]), 3)
            st = FileState(os.environ["KCRESIZE_STATE_FILE"])
            self.assertEqual(st.get("tbm-payments/fraud-scorer")["status"], "stale")
            self.assertEqual(mock_kubecost.S.configs, [])
        finally:
            for k in ("KCRESIZE_STATE", "KCRESIZE_ACTOR"):
                os.environ.pop(k, None)

    def test_expiry(self):
        os.environ["KCRESIZE_STATE"] = "file"
        try:
            cli.main(["plan"])
            os.environ["KCRESIZE_ACTOR"] = "fraud-owner"
            cli.main(["approve", "tbm-payments/fraud-scorer"])
            st = FileState(os.environ["KCRESIZE_STATE_FILE"])
            p = st.get("tbm-payments/fraud-scorer")
            p["decidedAt"] = "2020-01-01T00:00:00Z"
            st.save(p)
            cli.main(["watch"])
            self.assertEqual(st.get("tbm-payments/fraud-scorer")["status"], "expired")
        finally:
            for k in ("KCRESIZE_STATE", "KCRESIZE_ACTOR"):
                os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestGateway(Base):
    """gateway/kcresize-gateway.py: verify Slack signature / HMAC, forward as repository_dispatch."""

    def test_gateway(self):
        import hashlib, hmac, socket, subprocess, time, urllib.parse, urllib.request, urllib.error
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        env = dict(os.environ, GITHUB_REPOSITORY="acme/platform-finops", GITHUB_TOKEN="t", GATEWAY_PORT=str(port),
                   GITHUB_API_URL=f"http://127.0.0.1:{self.gh_srv.server_port}", SLACK_SIGNING_SECRET="slk", GATEWAY_SECRET="hm",
                   GATEWAY_HOST="127.0.0.1")
        gw = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "..", "gateway", "kcresize-gateway.py")],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.time() + 60          # interpreter start is slow on macOS runners
            while True:
                self.assertIsNone(gw.poll(), "gateway exited early")
                try:
                    urllib.request.urlopen(base + "/healthz", timeout=2)
                    break
                except Exception:
                    if time.time() > deadline:
                        self.fail("gateway did not start within 60s")
                    time.sleep(0.25)

            def post(path, body, headers):
                req = urllib.request.Request(base + path, data=body, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code

            def slack(action, reason, user="U0PAYMENTS1", secret="slk"):
                val = "plan-1|tbm-payments/fraud-scorer"
                payload = {"user": {"id": user}, "actions": [{"action_id": action, "value": val}],
                           "state": {"values": {f"reason|{val}": {"reason": {"value": reason}}}}}
                body = ("payload=" + urllib.parse.quote(json.dumps(payload))).encode()
                ts = str(int(time.time()))
                sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
                return post("/slack/interactions", body, {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})

            self.assertEqual(slack("kc_approve", "fine", secret="wrong"), 401)
            self.assertEqual(slack("kc_disapprove", ""), 200)          # acked, but not forwarded (reason required)
            self.assertEqual(slack("kc_approve", "traffic is flat"), 200)
            body = json.dumps({"event": "apply", "workload": "tbm-payments/fraud-scorer", "actor": "ops@example.com"}).encode()
            good = "sha256=" + hmac.new(b"hm", body, hashlib.sha256).hexdigest()
            self.assertEqual(post("/webhook", body, {"X-KC-Signature": "sha256=00"}), 401)
            self.assertEqual(post("/webhook", body, {"X-KC-Signature": good}), 202)
            bad = json.dumps({"event": "disapprove", "workload": "a/b", "actor": "x"}).encode()
            self.assertEqual(post("/webhook", bad, {"X-KC-Signature": "sha256=" + hmac.new(b"hm", bad, hashlib.sha256).hexdigest()}), 400)
            deadline = time.time() + 20            # Slack clicks are forwarded asynchronously
            while len(mock_github.G.dispatches) < 2 and time.time() < deadline:
                time.sleep(0.2)
            d = mock_github.G.dispatches
            self.assertEqual(sorted(x["client_payload"]["event"] for x in d), ["apply", "approve"])   # Slack is async
            self.assertTrue(all(x["event_type"] == "kcresize" for x in d))
            ap = next(x["client_payload"] for x in d if x["client_payload"]["event"] == "approve")
            self.assertEqual((ap["reason"], ap["actor"]), ("traffic is flat", "U0PAYMENTS1"))
        finally:
            gw.terminate()
