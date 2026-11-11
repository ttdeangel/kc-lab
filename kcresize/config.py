"""Configuration: a JSON file (non-secret settings) plus environment variables (secrets, overrides).

Secrets (webhook URLs, SMTP passwords, tokens) are only ever read from the environment,
so the config file can live in the repository.
"""
import json
import os
from dataclasses import dataclass, field

DEFAULTS = {
    "kubecost": {
        "url": "http://localhost:9090",
        "cluster": "",
        "window": "48h",              # recommendation window; also the action's rightSizingWindow
        "allocationWindow": "7d",     # cost evidence window in the approval report
        "readOnly": False,
    },
    "scope": {
        "namespaces": [],             # explicit list, or ...
        "namespaceLabel": "",         # ... "key=value" on the namespace (read from Kubecost namespaceLabels)
        "exclude": ["kube-system", "kube-public", "kube-node-lease", "kubecost", "local-path-storage"],
        "controllerKinds": ["deployment"],
    },
    "profiles": {"envLabel": "env", "prodValues": ["prod", "production"],
                 "prod": "Production", "default": "Development"},
    "guardrails": {
        "minChangePct": 10,           # ignore smaller changes
        "maxUpFactor": 4,             # block increases above Nx (bad-data guard)
        "maxDownPctProd": 80,         # larger prod cuts are flagged; owner must approve_force
        "driftPct": 20,               # re-check at apply; larger drift -> stale
        "approvalTtlHours": 120,      # approved but not actioned -> expired
    },
    "owners": {},                     # "<namespace>": {"github": [...], "email": [...], "slack": "...", "teams": "..."}
    "defaultOwner": {"github": [], "email": []},
    "actioning": {"github": [], "email": []},   # the only people who may apply / decline
    "notify": {"owner": ["email"], "actioning": ["email"]},   # channels per audience
    "state": "github",                # github | file
    "stateFile": ".kcresize/state.json",
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


@dataclass
class Config:
    data: dict = field(default_factory=lambda: json.loads(json.dumps(DEFAULTS)))

    # --- convenience accessors
    @property
    def kc(self):
        return self.data["kubecost"]

    @property
    def scope(self):
        return self.data["scope"]

    @property
    def guard(self):
        return self.data["guardrails"]

    @property
    def profiles(self):
        return self.data["profiles"]

    def owner(self, namespace):
        return self.data["owners"].get(namespace) or self.data["defaultOwner"]

    @property
    def actioning(self):
        return self.data["actioning"]

    def channels(self, audience):
        return self.data["notify"].get(audience, [])


def env_list(name):
    v = os.environ.get(name, "")
    return [x.strip() for x in v.split(",") if x.strip()]


def load(path=None):
    """Load config file (if any) and apply KCRESIZE_* / KUBECOST_* environment overrides."""
    data = json.loads(json.dumps(DEFAULTS))
    path = path or os.environ.get("KCRESIZE_CONFIG", "")
    if path and os.path.exists(path):
        with open(path) as f:
            data = _merge(data, json.load(f))
    e = os.environ
    if e.get("KUBECOST_URL"):
        data["kubecost"]["url"] = e["KUBECOST_URL"]
    if e.get("KUBECOST_CLUSTER"):
        data["kubecost"]["cluster"] = e["KUBECOST_CLUSTER"]
    if e.get("KCRESIZE_WINDOW"):
        data["kubecost"]["window"] = e["KCRESIZE_WINDOW"]
    if e.get("KCRESIZE_NAMESPACES"):
        data["scope"]["namespaces"] = env_list("KCRESIZE_NAMESPACES")
    if e.get("KCRESIZE_NAMESPACE_LABEL"):
        data["scope"]["namespaceLabel"] = e["KCRESIZE_NAMESPACE_LABEL"]
    if e.get("KCRESIZE_ACTIONERS"):
        data["actioning"]["github"] = env_list("KCRESIZE_ACTIONERS")
    if e.get("KCRESIZE_STATE"):
        data["state"] = e["KCRESIZE_STATE"]
    if e.get("KCRESIZE_STATE_FILE"):
        data["stateFile"] = e["KCRESIZE_STATE_FILE"]
    if e.get("KCRESIZE_READ_ONLY", "").lower() in ("1", "true", "yes"):
        data["kubecost"]["readOnly"] = True
    url = data["kubecost"]["url"].rstrip("/")
    data["kubecost"]["url"] = url
    if "demo.kubecost.io" in url:      # public demo: never write to it
        data["kubecost"]["readOnly"] = True
    if not data["kubecost"]["cluster"]:
        raise SystemExit("Kubecost cluster id is required (config kubecost.cluster or KUBECOST_CLUSTER)")
    return Config(data)
