"""kcbudget tests against the in-process Kubecost stand-in.  Run: python -m unittest -v"""
import copy
import datetime as dt
import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import mock_kubecost  # noqa: E402
from kcbudget import check, cli, intake, spec, sync  # noqa: E402
from kcbudget.api import BudgetAPI  # noqa: E402

LAB = os.path.join(ROOT, "budgets", "lab.json")
PROD = os.path.join(ROOT, "budgets", "prod.json")
WEBHOOKS = {"slack": {c: f"https://hooks.slack.test/{c}" for c in ("finops-alerts", "commerce-alerts", "platform-alerts", "data-alerts")},
            "teams": {c: f"https://teams.test/{c}" for c in ("finops", "commerce", "platform", "data")}}
UTC = dt.timezone.utc


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = mock_kubecost.serve()

    def setUp(self):
        mock_kubecost.S.__init__()
        self.tmp = tempfile.mkdtemp()
        self.outbox = os.path.join(self.tmp, "outbox")
        self.env = {"KUBECOST_URL": f"http://127.0.0.1:{self.srv.server_port}", "KCBUDGET_WEBHOOKS": json.dumps(WEBHOOKS),
                    "KCRESIZE_NOTIFY_DRY_RUN": "1", "KCRESIZE_OUTBOX": self.outbox, "GITHUB_STEP_SUMMARY": os.path.join(self.tmp, "summary.md"),
                    "GITHUB_OUTPUT": os.path.join(self.tmp, "out.txt")}
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        import kcresize.notify as n
        n.OUTBOX = self.outbox
        self.api = BudgetAPI(self.env["KUBECOST_URL"])

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lab(self):
        return spec.load(LAB)

    def outputs(self):
        with open(self.env["GITHUB_OUTPUT"]) as f:
            return dict(line.strip().split("=", 1) for line in f if "=" in line)

    def outbox_items(self):
        out = []
        for f in sorted(glob.glob(os.path.join(self.outbox, "*.json"))):
            with open(f) as fh:
                out.append((os.path.basename(f).split("-", 1)[-1][:-5], json.load(fh)))
        return out


class TestSpec(Base):
    def test_lab_file_has_50_valid_budgets(self):
        doc = self.lab()
        errors, warnings = spec.validate(doc)
        self.assertEqual(errors, [])
        self.assertEqual(len(doc["budgets"]), 50)
        self.assertEqual(len({b["name"] for b in doc["budgets"]}), 50)
        for p, u in spec.pool_usage(doc).items():
            self.assertLessEqual(u["allocated"], u["monthlyLimit"], p)

    def test_prod_template_valid(self):
        self.assertEqual(spec.validate(spec.load(PROD))[0], [])

    def test_validation_errors(self):
        doc = self.lab()
        bad = copy.deepcopy(doc["budgets"][5])
        bad.update(name="Bad_Name", interval="weekly", intervalDay=7)
        bad["alerts"] = [{"percentage": 80, "emails": ["x@other.org"], "slack": ["https://hooks.slack.com/x"]},
                         {"percentage": 80, "emails": ["a@example.com"]}]
        doc["budgets"].append(bad)
        errors, _ = spec.validate(doc)
        text = "\n".join(errors)
        for frag in ("lowercase", "managed prefix", "weekly intervalDay", "outside allowedEmailDomains", "not URLs", "duplicate alert percentage"):
            self.assertIn(frag, text)

    def test_pool_overflow_blocked(self):
        doc = self.lab()
        b = next(x for x in doc["budgets"] if x.get("pool") == "bu-data")
        b["spendLimit"] = 5000
        self.assertTrue(any("more than its monthlyLimit" in e for e in spec.validate(doc)[0]))

    def test_body_and_webhook_resolution(self):
        doc = self.lab()
        raw = next(b for b in doc["budgets"] if b["name"] == "kcb-fraud-ns-tbm-payments-monthly")
        body = spec.to_kubecost(doc, raw, WEBHOOKS)
        self.assertEqual(body["values"], {"namespace": ["tbm-payments"]})
        self.assertEqual((body["interval"], body["intervalDay"], body["kind"]), ("monthly", 1, "soft"))
        top = body["actions"][-1]
        self.assertEqual(top["percentage"], 100)
        self.assertIn("https://teams.test/commerce", top["msTeamsWebhooks"])
        self.assertIn("https://hooks.slack.test/finops-alerts", top["slackWebhooks"])
        problems = []
        body = spec.to_kubecost(doc, raw, {"slack": {}, "teams": {}}, problems=problems)
        self.assertTrue(problems)
        self.assertNotIn("slackWebhooks", body["actions"][-1])
        with self.assertRaises(spec.SpecError):
            spec.to_kubecost(doc, raw, {"slack": {}, "teams": {}}, strict=True)

    def test_mask(self):
        self.assertEqual(spec.mask("https://hooks.slack.com/services/T/B/secret"), "https://hooks.slack.com/***")


class TestSync(Base):
    def test_apply_idempotent_update_prune(self):
        doc, wh = self.lab(), spec.webhooks_from_env()
        # a budget someone made in the Kubecost UI: never touched
        self.api.create({"name": "team-made-in-ui", "values": {"namespace": ["x"]}, "kind": "soft", "interval": "monthly",
                         "intervalDay": 1, "spendLimit": 5, "actions": []})
        r = sync.plan(doc, self.api, wh)
        self.assertEqual(sum(c["op"] == "create" for c in r["changes"]), 50)
        log = sync.apply(r, self.api)
        self.assertFalse([x for x in log if "FAILED" in x[2]])
        self.assertEqual(len(mock_kubecost.S.budgets), 51)
        r = sync.plan(doc, self.api, wh)
        self.assertEqual({c["op"] for c in r["changes"]}, {"noop"})
        self.assertEqual(r["unmanaged"], 1)
        # change one limit, drop one budget
        doc["budgets"][6]["spendLimit"] = 31
        gone = doc["budgets"].pop(0)["name"]
        r = sync.plan(doc, self.api, wh)
        ops = {c["name"]: c["op"] for c in r["changes"] if c["op"] != "noop"}
        self.assertEqual(ops, {doc["budgets"][5]["name"]: "update", gone: "orphan"})
        r = sync.plan(doc, self.api, wh, prune=True)
        sync.apply(r, self.api)
        names = {b["name"] for b in mock_kubecost.S.budgets}
        self.assertNotIn(gone, names)
        self.assertIn("team-made-in-ui", names)
        self.assertEqual(next(b for b in mock_kubecost.S.budgets if b["name"] == doc["budgets"][5]["name"])["spendLimit"], 31)
        self.assertEqual({c["op"] for c in sync.plan(doc, self.api, wh, prune=True)["changes"]}, {"noop"})

    def test_max_deletes_guard(self):
        doc, wh = self.lab(), spec.webhooks_from_env()
        sync.apply(sync.plan(doc, self.api, wh), self.api)
        doc["budgets"] = doc["budgets"][:10]
        with self.assertRaises(SystemExit):
            sync.apply(sync.plan(doc, self.api, wh, prune=True), self.api, max_deletes=10)

    def test_summary_never_shows_webhook_urls(self):
        r = sync.plan(self.lab(), self.api, spec.webhooks_from_env())
        md = sync.summary_markdown("lab", r, sync.apply(r, self.api), spec.pool_usage(self.lab()))
        self.assertNotIn("hooks.slack.test", md)
        self.assertNotIn("teams.test", md)
        self.assertIn("| 50 | 0 | 0 | 0 |", md)
        exported = json.dumps(sync.export(self.api, "kcb-"))
        self.assertNotIn("/finops-alerts", exported)

    def test_rest_style_api(self):
        mock_kubecost.S.budget_style = "rest"
        api = BudgetAPI(self.env["KUBECOST_URL"])
        self.assertEqual(api.detect(), "rest")
        doc = self.lab()
        sync.apply(sync.plan(doc, api, WEBHOOKS), api)
        doc["budgets"][0]["spendLimit"] = 700
        doc["budgets"].pop(1)
        log = sync.apply(sync.plan(doc, api, WEBHOOKS, prune=True), api)
        self.assertFalse([x for x in log if "FAILED" in x[2]], log)
        self.assertEqual(len(mock_kubecost.S.budgets), 49)

    def test_smoke(self):
        steps = sync.smoke(self.api, run_id="42")
        self.assertTrue(all(ok for _, ok, _ in steps), steps)
        self.assertEqual(mock_kubecost.S.budgets, [])

    def test_cli_plan_apply(self):
        self.assertEqual(cli.main(["apply", "--file", LAB, "--prune"]), 0)
        self.assertEqual(self.outputs()["create"], "50")
        self.assertEqual(cli.main(["plan", "--file", LAB]), 0)
        self.assertEqual(self.outputs()["changed"], "false")
        self.assertEqual(cli.main(["smoke"]), 0)

    def test_cli_invalid_file_exit_2(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            json.dump({"budgets": [{"name": "nope", "values": {}, "spendLimit": -1}]}, f)
        with self.assertRaises(SystemExit) as e:
            cli.main(["validate", "--file", bad])
        self.assertEqual(e.exception.code, 2)


class TestCheck(Base):
    NOW = dt.datetime(2026, 11, 12, 17, 0, tzinfo=UTC)     # Thursday

    def test_period_start(self):
        self.assertEqual(check.period_start("monthly", 1, self.NOW), dt.datetime(2026, 11, 1, tzinfo=UTC))
        self.assertEqual(check.period_start("monthly", 15, self.NOW), dt.datetime(2026, 10, 15, tzinfo=UTC))
        self.assertEqual(check.period_start("weekly", 1, self.NOW), dt.datetime(2026, 11, 9, tzinfo=UTC))    # Monday
        self.assertEqual(check.period_start("weekly", 0, self.NOW), dt.datetime(2026, 11, 8, tzinfo=UTC))    # Sunday
        self.assertEqual(check.period_start("weekly", 4, self.NOW), dt.datetime(2026, 11, 12, tzinfo=UTC))   # today

    def test_tripwires_notify_once_per_period(self):
        sync.apply(sync.plan(self.lab(), self.api, WEBHOOKS), self.api)
        state = os.path.join(self.tmp, "state.json")
        rows = check.run(self.api, state, ["email", "slack", "teams"], env="kcbudget-lab", now=self.NOW)
        fired = {r["name"] for r in rows if r.get("newThresholds")}
        # tbm-payments $3.20, shop-prod $1.10, kubecost $6.00 this period in the mock
        self.assertIn("kcb-tripwire-tbm-payments-weekly", fired)
        self.assertIn("kcb-tripwire-kubecost-weekly", fired)
        self.assertIn("kcb-platform-ns-kubecost-monthly", {r["name"] for r in rows if r.get("pct", 0) >= 10})
        kinds = [k for k, _ in self.outbox_items()]
        self.assertIn("teams-budget", kinds)
        self.assertIn("slack-budget", kinds)
        self.assertIn("email", kinds)
        card = next(p for k, p in self.outbox_items() if k == "teams-budget")
        self.assertEqual(card["payload"]["attachments"][0]["contentType"], "application/vnd.microsoft.card.adaptive")
        n = len(self.outbox_items())
        check.run(self.api, state, ["email", "slack", "teams"], env="kcbudget-lab", now=self.NOW + dt.timedelta(hours=1))
        self.assertEqual(len(self.outbox_items()), n, "no duplicates in the same period")
        check.run(self.api, state, ["email", "slack", "teams"], env="kcbudget-lab", now=self.NOW + dt.timedelta(days=7))
        self.assertGreater(len(self.outbox_items()), n, "new period alerts again")

    def test_channel_filter_and_summary(self):
        sync.apply(sync.plan(self.lab(), self.api, WEBHOOKS), self.api)
        rows = check.run(self.api, os.path.join(self.tmp, "s.json"), ["teams"], now=self.NOW)
        self.assertEqual({k for k, _ in self.outbox_items()}, {"teams-budget"})
        md = check.summary_markdown("lab", rows, ["teams"])
        self.assertIn("over limit", md)
        self.assertNotIn("teams.test", md)


class TestIntake(Base):
    FORM = {"environment": "lab", "requester": "Dana@Example.com", "team": "Search Relevance", "costCenter": "CC-1234",
            "scopeType": "namespace", "scopeValues": "search-relevance-prod", "interval": "Monthly", "intervalDay": "1",
            "amount": "$75", "thresholds": "[\"50%\",\"90%\",\"100%\"]", "emails": "search-leads@example.com; dana@example.com",
            "slackChannel": "commerce-alerts", "teamsChannel": "None", "pool": "bu-commerce", "justification": "New ranking service",
            "responseId": "17"}

    def test_request_becomes_budget(self):
        doc = self.lab()
        b, replaced, md = intake.apply_request(doc, self.FORM, WEBHOOKS)
        self.assertFalse(replaced)
        self.assertEqual(b["name"], "kcb-search-relevance-ns-search-relevance-prod-monthly")
        self.assertEqual(b["spendLimit"], 75)
        self.assertEqual([a["percentage"] for a in b["alerts"]], [50, 90, 100])
        self.assertEqual(b["alerts"][0]["emails"], ["dana@example.com", "search-leads@example.com"])
        self.assertEqual(b["alerts"][0]["slack"], ["commerce-alerts"])
        self.assertNotIn("teams", b["alerts"][0])
        self.assertEqual(len(doc["budgets"]), 51)
        self.assertEqual(spec.validate(doc)[0], [])
        self.assertIn("Pool `bu-commerce` after this", md)
        _, replaced, _ = intake.apply_request(doc, dict(self.FORM, amount="80"), WEBHOOKS)
        self.assertTrue(replaced)
        self.assertEqual(len(doc["budgets"]), 51)

    def test_request_rejected_when_pool_full(self):
        with self.assertRaises(spec.SpecError) as e:
            intake.apply_request(self.lab(), dict(self.FORM, amount="900"))
        self.assertIn("monthlyLimit", str(e.exception))

    def test_request_rejects_foreign_email(self):
        with self.assertRaises(spec.SpecError):
            intake.apply_request(self.lab(), dict(self.FORM, emails="someone@gmail.com"))

    def test_cli_request_writes_file_and_pr_body(self):
        f = os.path.join(self.tmp, "lab.json")
        shutil.copy(LAB, f)
        ev = os.path.join(self.tmp, "event.json")
        with open(ev, "w") as fh:
            json.dump({"action": "kcbudget-request", "client_payload": self.FORM}, fh)
        pr = os.path.join(self.tmp, "pr.md")
        self.assertEqual(cli.main(["request", "--file", f, "--payload", ev, "--pr-body", pr]), 0)
        self.assertEqual(len(spec.load(f)["budgets"]), 51)
        self.assertIn("Microsoft Forms (response 17)", open(pr).read())
        self.assertEqual(self.outputs()["budget"], "kcb-search-relevance-ns-search-relevance-prod-monthly")


if __name__ == "__main__":
    unittest.main()
