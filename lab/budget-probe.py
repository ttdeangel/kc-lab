#!/usr/bin/env python3
"""Find the budget request shape this Kubecost accepts.

  python3 lab/budget-probe.py [KUBECOST_URL]      (default http://localhost:9090)

Sends the same small test budget ("kcb-probe-*", $1/week on the kubecost namespace, no alerts) in several
shapes and reports which one Kubecost accepts. Every budget it creates is deleted again. It also prints
the fields of any budget already in Kubecost (one made in the UI is the best reference).
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9090").rstrip("/")


def call(method, path, body=None, params=None):
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path + q, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()[:400]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except Exception as e:
        return 0, str(e)


def budgets():
    st, raw = call("GET", "/model/budgets")
    try:
        d = json.loads(raw)
        return d.get("data", d) or []
    except Exception:
        return []


def cleanup():
    for b in budgets():
        if str(b.get("name", "")).startswith("kcb-probe"):
            for path, params in (("/model/budget", {"id": b.get("id")}), ("/model/deleteBudget", {"id": b.get("id")}),
                                 (f"/model/budget/{b.get('id')}", None)):
                st, _ = call("DELETE", path, params=params)
                if 200 <= st < 300:
                    break


base = {"values": {"namespace": ["kubecost"]}, "kind": "soft", "interval": "weekly", "intervalDay": 1, "spendLimit": 1}
act = {"percentage": 100, "emails": ["probe@example.com"]}
variants = [
    ("A type at top level, no actions", dict(base, name="kcb-probe-a", type="allocations", actions=[])),
    ("B type at top level + action", dict(base, name="kcb-probe-b", type="allocations", actions=[act])),
    ("C type at top level AND in action", dict(base, name="kcb-probe-c", type="allocations", actions=[dict(act, type="allocations")])),
    ("D type only in action", dict(base, name="kcb-probe-d", actions=[dict(act, type="allocations")])),
    ("E budgetType at top level", dict(base, name="kcb-probe-e", budgetType="allocations", actions=[act])),
    ("F type 'allocation' (singular)", dict(base, name="kcb-probe-f", type="allocation", actions=[act])),
    ("G type as query parameter", dict(base, name="kcb-probe-g", actions=[act])),
    ("H wrapped in {budget: ...}", {"budget": dict(base, name="kcb-probe-h", type="allocations", actions=[act])}),
]

print(f"Kubecost: {URL}")
existing = budgets()
print(f"budgets already in Kubecost: {len(existing)}")
for b in existing[:2]:
    print("  example budget as Kubecost returns it:")
    print("  " + json.dumps(b, indent=2)[:1500].replace("\n", "\n  "))
cleanup()
ok = []
for label, body in variants:
    params = {"type": "allocations"} if label.startswith("G") else None
    st, raw = call("POST", "/model/budget", body, params)
    name = body.get("name") or body.get("budget", {}).get("name")
    stored = [b for b in budgets() if b.get("name") == name]
    good = bool(stored)                      # judged by what Kubecost stored, not by the status code
    ok += [label] if good else []
    note = " (stored, though the reply was an error: Kubecost 3.3.1 quirk)" if good and not 200 <= st < 300 else ""
    print(f"{'ACCEPTED' if good else 'rejected'}  {label:38} HTTP {st}{note}  {raw[:160]}")
    if good:
        if stored:
            print("          stored as: " + json.dumps(stored[0])[:600])
    cleanup()
print("\nresult: " + (", ".join(ok) if ok else "no shape accepted - paste this output, and if you can, create one budget in the Kubecost UI and run this again"))
