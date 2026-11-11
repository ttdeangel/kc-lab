"""Kubecost 3.x HTTP client. Endpoints verified against chart 3.3.1 (see docs/kubecost-api-notes.md)."""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class KubecostError(RuntimeError):
    pass


class Kubecost:
    def __init__(self, url, read_only=False, timeout=120):
        self.url = url.rstrip("/")
        self.read_only = read_only
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        # Optional auth for a Kubecost behind a proxy: KUBECOST_AUTH_HEADER="Authorization: Bearer x"
        # or KUBECOST_BASIC_USER / KUBECOST_BASIC_PASSWORD.
        h = os.environ.get("KUBECOST_AUTH_HEADER", "")
        if ":" in h:
            k, v = h.split(":", 1)
            self.headers[k.strip()] = v.strip()
        if os.environ.get("KUBECOST_BASIC_USER"):
            tok = base64.b64encode(f'{os.environ["KUBECOST_BASIC_USER"]}:{os.environ.get("KUBECOST_BASIC_PASSWORD", "")}'.encode()).decode()
            self.headers["Authorization"] = f"Basic {tok}"

    # ---------- transport
    def _call(self, method, path, params=None, body=None):
        if method != "GET" and self.read_only:
            raise KubecostError(f"read-only mode: refusing {method} {path}")
        q = ("?" + urllib.parse.urlencode(params, safe=",:")) if params else ""
        data = json.dumps(body).encode() if body is not None else None
        hdrs = dict(self.headers)
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path + q, data=data, headers=hdrs, method=method)
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode() or "{}"
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                msg = e.read().decode()[:300]
                if e.code < 500:
                    raise KubecostError(f"{method} {path} -> HTTP {e.code}: {msg}")
                last = f"HTTP {e.code}: {msg}"
            except (urllib.error.URLError, TimeoutError) as e:
                last = str(e)
            time.sleep(2 * (attempt + 1))
        raise KubecostError(f"{method} {path} failed: {last}")

    def get(self, path, **params):
        return self._call("GET", path, params)

    # ---------- recommendations
    def profiles(self):
        try:
            return {p["name"]: p for p in (self.get("/model/savings/requestSizingV2/profiles").get("data") or [])}
        except KubecostError:
            return {}

    @staticmethod
    def profile_params(profile):
        """requestSizingV2 query params equivalent to a named profile (the API rejects profile=)."""
        p = profile or {}
        out = {
            "algorithmCPU": p.get("algorithmCPU", "max"),
            "algorithmRAM": p.get("algorithmRAM", "max"),
            "targetCPUUtilization": p.get("targetUtilizationCPU", 0.8),
            "targetRAMUtilization": p.get("targetUtilizationRAM", 0.8),
        }
        if p.get("quantileCPU"):
            out["qCPU"] = p["quantileCPU"]
        if p.get("quantileRAM"):
            out["qRAM"] = p["quantileRAM"]
        return out

    def recommendations(self, cluster, namespace, window, profile, controller=None, kinds=("deployment",)):
        f = f'cluster:"{cluster}"+namespace:"{namespace}"'
        if kinds:
            f += "+controllerKind:" + ",".join(f'"{k}"' for k in kinds)
        if controller:
            f += f'+controllerName:"{controller}"'
        params = dict(window=window, filter=f, **self.profile_params(profile))
        recs = self.get("/model/savings/requestSizingV2", **params).get("Recommendations") or []
        return [r for r in recs if r.get("clusterID") == cluster and r.get("namespace") == namespace]

    # ---------- cost evidence
    def allocation_by_controller(self, cluster, namespaces, window):
        if not namespaces:
            return {}
        f = f'cluster:"{cluster}"+namespace:' + ",".join(f'"{n}"' for n in namespaces)
        d = self.get("/model/allocation", window=window, aggregate="namespace,controller",
                     accumulate="true", idle="false", filter=f).get("data") or [{}]
        return d[0] or {}

    def namespace_labels(self, cluster, window="2d"):
        d = self.get("/model/allocation", window=window, aggregate="namespace", accumulate="true",
                     idle="false", filter=f'cluster:"{cluster}"').get("data") or [{}]
        return {k: (v.get("properties") or {}).get("namespaceLabels") or {} for k, v in (d[0] or {}).items()}

    # ---------- actions (the only write path)
    def create_resize_action(self, name, profile, filt, window, maintenance_window):
        body = {"source": "API", "scope": "local", "cluster": "all", "name": name,
                "action": "containerRequestRightSizing",
                "config": {"profile": profile, "filter": filt, "rightSizingWindow": window, "enabled": True,
                           "cpu": True, "memory": True, "maintenanceWindow": maintenance_window}}
        r = self._call("POST", "/model/action/config", body=body)
        aid = (r.get("data") or {}).get("id")
        if not aid:
            raise KubecostError(f"Kubecost returned no action id: {r}")
        return aid

    def get_action(self, aid):
        try:
            return self.get("/model/action/config", id=aid).get("data")
        except KubecostError:
            return None

    def delete_action(self, aid):
        self._call("DELETE", "/model/action/config", {"id": aid})

    def action_configs(self):
        return self.get("/model/action/configs").get("data") or []

    def completed(self, window="2d", limit=500):
        d = self.get("/model/actions/completed", window=window, limit=limit).get("data") or {}
        return d.get("actions") or []

    def runs(self, window="2d", limit=50):
        d = self.get("/model/actions/runs", window=window, limit=limit, offset=0).get("data") or {}
        return d.get("runs") or []

    def heartbeat(self, cluster):
        return (self.get("/model/action/heartbeats/latest").get("data") or {}).get(cluster)


def maintenance_window_now():
    """Kubecost cron-style window for the current and next UTC hour: '* H * * DOW||* H2 * * DOW2'.
    Note it repeats weekly, so one-shot actions must be deleted after they run."""
    t = time.gmtime()
    n = time.gmtime(time.time() + 3600)
    dow = lambda x: (x.tm_wday + 1) % 7      # Python Mon=0 -> cron Sun=0
    return f"* {t.tm_hour} * * {dow(t)}||* {n.tm_hour} * * {dow(n)}"


def sanitize_label(key):
    """Kubecost reports label keys with non-alphanumerics replaced by '_'."""
    return "".join(c if c.isalnum() else "_" for c in key)
