"""In-process stand-in for the Kubecost 3.x API (response shapes captured from a real 3.3.1 lab).

Run standalone for local demos:  python tests/mock_kubecost.py 9099
"""
import json
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CLUSTER = "example-cluster"


def rec(ns, dep, container, cur, new, savings, maxu=("2m", "1Mi")):
    return {"clusterID": CLUSTER, "namespace": ns, "controllerKind": "deployment", "controllerName": dep, "containerName": container,
            "recommendedRequest": {"cpu": f"{new[0]}m", "memory": f"{new[1]}Mi"},
            "normalizedRecommendedRequest": {"cpuInMilliCores": new[0], "memoryInMiB": new[1]},
            "monthlySavings": {"cpu": savings * 0.85, "memory": savings * 0.15, "total": savings},
            "latestKnownRequest": {"cpu": f"{cur[0]}m", "memory": f"{cur[1]}Mi"},
            "normalizedLatestKnownRequest": {"cpuInMilliCores": cur[0], "memoryInMiB": cur[1]},
            "maxUsage": {"cpu": maxu[0], "memory": maxu[1]}, "averageUsage": {"cpu": "1m", "memory": "1Mi"}}


class State:
    def __init__(self):
        self.recs = [
            rec("tbm-payments", "fraud-scorer", "app", (400, 512), (10, 20), 12.43),
            rec("tbm-payments", "checkout-api", "app", (200, 256), (10, 20), 10.03),
            rec("shop-prod", "api", "api", (300, 512), (40, 150), 22.5),
            rec("shop-prod", "worker", "worker", (300, 384), (10, 30), 11.0),
            rec("shop-prod", "worker", "log-shipper", (50, 128), (10, 20), 2.0),
            rec("kc-turndown-test", "resource-hog", "stress", (500, 512), (302117, 486), -4252.0),   # real bad-data case
            rec("shop-dev", "worker", "worker", (300, 384), (290, 380), 0.1),                       # below min change
            rec("kubecost", "kubecost-frontend", "frontend", (21, 110), (59, 20), -1.5),             # excluded ns
        ]
        self.ns_labels = {
            "tbm-payments": {"purpose": "kubecost-action-qa", "team": "fraud"},
            "shop-prod": {"env": "prod", "team": "shop", "kc_lab_io_resize_approval": "required"},
            "shop-dev": {"env": "dev", "team": "shop", "kc_lab_io_resize_approval": "required"},
            "kc-turndown-test": {"purpose": "kubecost-action-qa"},
            "kubecost": {},
        }
        self.configs, self.completed, self.log = [], [], []
        self.auto_complete = "success"     # success | failed | None (never)
        self.lock = threading.Lock()

    def alloc(self, key, cost=0.46, cpu=0.35, ram=470124624):
        return {"name": key, "minutes": 1333, "start": "2026-11-10T02:21:00Z", "end": "2026-11-11T00:34:00Z", "totalCost": cost,
                "cpuCoreRequestAverage": cpu, "cpuCoreUsageAverage": cpu * 0.0006, "ramByteRequestAverage": ram,
                "ramByteUsageAverage": ram * 0.0008, "cpuEfficiency": 0.00055, "ramEfficiency": 0.00084, "totalEfficiency": 0.0006,
                "properties": {"namespaceLabels": self.ns_labels.get(key.split("/")[0], {})}}

    def complete_action(self, cfg):
        """Simulate the cluster controller executing an action: one completed entry per matching container."""
        for ns, dep in re.findall(r'namespace:"([^"]+)"\+controllerName:"([^"]+)"', cfg["config"]["filter"]):
            for r in self.recs:
                if r["namespace"] == ns and r["controllerName"] == dep:
                    self.completed.append({"id": str(uuid.uuid4()), "runId": "run-1", "configName": cfg["name"], "cluster": CLUSTER,
                                           "action": "containerRequestRightSizing", "status": self.auto_complete,
                                           "parameters": {"cluster": CLUSTER, "namespace": ns, "controllerKind": "deployment",
                                                          "controllerName": dep, "containerName": r["containerName"],
                                                          "lastCPURequest": r["latestKnownRequest"]["cpu"], "lastRAMRequest": r["latestKnownRequest"]["memory"],
                                                          "targetCPURequest": r["recommendedRequest"]["cpu"], "targetRAMRequest": r["recommendedRequest"]["memory"]},
                                           "createdAt": "2026-11-11T00:47:13Z", "completedAt": "2026-11-11T01:00:00Z"})


S = State()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        S.log.append(("GET", u.path, q))
        f = q.get("filter", "")
        if u.path == "/model/savings/requestSizingV2":
            if "profile" in q:
                return self.send(400, {"message": "invalid HTTP parameters present: [profile]"})
            out = [r for r in S.recs if f'namespace:"{r["namespace"]}"' in f and ("controllerName" not in f or f'controllerName:"{r["controllerName"]}"' in f)]
            return self.send(200, {"TotalMonthlySavings": sum(r["monthlySavings"]["total"] for r in out), "Count": len(out), "Recommendations": out})
        if u.path == "/model/savings/requestSizingV2/profiles":
            return self.send(200, {"code": 200, "data": [
                {"name": "Development", "algorithmCPU": "max", "algorithmRAM": "max", "targetUtilizationCPU": 0.8, "targetUtilizationRAM": 0.8, "createdBy": "Default"},
                {"name": "Production", "algorithmCPU": "max", "algorithmRAM": "max", "targetUtilizationCPU": 0.65, "targetUtilizationRAM": 0.65, "createdBy": "Default"}]})
        if u.path == "/model/allocation":
            if q.get("aggregate") == "namespace":
                return self.send(200, {"code": 200, "data": [{n: {"name": n, "properties": {"namespaceLabels": l}} for n, l in S.ns_labels.items()}]})
            keys = {f'{r["namespace"]}/{r["controllerName"]}' for r in S.recs if f'"{r["namespace"]}"' in f}
            return self.send(200, {"code": 200, "data": [{k: S.alloc(k) for k in keys}]})
        if u.path == "/model/action/config":
            c = [x for x in S.configs if x["id"] == q.get("id")]
            return self.send(200, {"code": 200, "data": c[0]}) if c else self.send(404, {"code": 404, "message": "Not Found"})
        if u.path == "/model/action/configs":
            return self.send(200, {"code": 200, "data": S.configs})
        if u.path == "/model/actions/completed":
            return self.send(200, {"code": 200, "data": {"totalActionsCount": len(S.completed), "actions": S.completed}})
        if u.path == "/model/actions/runs":
            return self.send(200, {"code": 200, "data": {"itemCount": 0, "runs": None}})
        if u.path == "/model/action/heartbeats/latest":
            return self.send(200, {"code": 200, "data": {CLUSTER: "2026-11-11T01:24:59Z"}})
        return self.send(404, {"message": "404 page not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        S.log.append(("POST", self.path, body))
        if self.path == "/model/action/config":
            body["id"] = str(uuid.uuid4())
            with S.lock:
                S.configs.append(body)
                if S.auto_complete:
                    S.complete_action(body)
            return self.send(200, {"code": 200, "data": body})
        return self.send(404, {})

    def do_DELETE(self):
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        S.log.append(("DELETE", urlparse(self.path).path, q))
        S.configs = [c for c in S.configs if c["id"] != q.get("id")]
        return self.send(200, {"code": 200, "data": None})


def serve(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    print(f"mock Kubecost on :{sys.argv[1] if len(sys.argv) > 1 else 9099}")
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 9099), H).serve_forever()
