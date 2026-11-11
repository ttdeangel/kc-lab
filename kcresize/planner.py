"""Turn Kubecost recommendations + allocation data into approval proposals, with guardrails."""
import time

from .kubecost import sanitize_label

MONTH_MINUTES = 43800  # 730 h


def cpu_m(v):
    if v in (None, ""):
        return None
    v = str(v)
    return float(v[:-1]) if v.endswith("m") else float(v) * 1000


def mem_mi(v):
    if v in (None, ""):
        return None
    v = str(v)
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1048576, "k": 1000 / 1048576, "M": 1e6 / 1048576, "G": 1e9 / 1048576}
    for u in ("Ki", "Mi", "Gi", "Ti", "k", "M", "G"):
        if v.endswith(u):
            return float(v[: -len(u)]) * units[u]
    return float(v) / 1048576


def pct_change(a, b):
    return 100.0 if not a else abs(b - a) / a * 100


def down_pct(a, b):
    return 0.0 if not a else (a - b) / a * 100


def label_value(labels, key):
    return (labels or {}).get(sanitize_label(key)) or (labels or {}).get(key)


def in_scope(cfg, ns, ns_labels):
    if ns in cfg.scope["exclude"]:
        return False
    if cfg.scope["namespaces"]:
        return ns in cfg.scope["namespaces"]
    sel = cfg.scope["namespaceLabel"]
    if sel:
        k, _, v = sel.partition("=")
        return label_value(ns_labels.get(ns), k) == v
    return True


def env_of(cfg, ns_labels):
    v = (label_value(ns_labels, cfg.profiles["envLabel"]) or "").lower()
    return "prod" if v in cfg.profiles["prodValues"] else "nonprod"


def allocation_summary(a):
    if not a:
        return None
    mins = a.get("minutes") or 0
    monthly = (a.get("totalCost", 0) / mins * MONTH_MINUTES) if mins else 0
    return {
        "dataMinutes": mins, "start": a.get("start"), "end": a.get("end"),
        "monthlyCost": round(monthly, 2),
        "cpuRequestM": round((a.get("cpuCoreRequestAverage") or 0) * 1000, 1),
        "cpuUsageM": round((a.get("cpuCoreUsageAverage") or 0) * 1000, 1),
        "ramRequestMi": round((a.get("ramByteRequestAverage") or 0) / 1048576, 1),
        "ramUsageMi": round((a.get("ramByteUsageAverage") or 0) / 1048576, 1),
        "efficiencyPct": round((a.get("totalEfficiency") or 0) * 100, 2),
        "cpuEfficiencyPct": round((a.get("cpuEfficiency") or 0) * 100, 2),
        "ramEfficiencyPct": round((a.get("ramEfficiency") or 0) * 100, 2),
    }


def build_proposal(cfg, ns, controller, recs, alloc, ns_labels, profile_name, plan_id):
    """One proposal per controller (all its containers). Returns None if nothing significant."""
    g = cfg.guard
    env = env_of(cfg, ns_labels)
    containers = []
    for r in recs:
        if r.get("controllerName") != controller:
            continue
        cur = r.get("normalizedLatestKnownRequest") or {}
        rec = r.get("normalizedRecommendedRequest") or {}
        cc, cm = cur.get("cpuInMilliCores"), cur.get("memoryInMiB")
        rc, rm = rec.get("cpuInMilliCores"), rec.get("memoryInMiB")
        if rc is None or rm is None:
            continue
        blocked = []
        if cc and rc > cc * g["maxUpFactor"]:
            blocked.append(f"cpu increase above {g['maxUpFactor']}x ({cc}m -> {rc}m)")
        if cm and rm > cm * g["maxUpFactor"]:
            blocked.append(f"memory increase above {g['maxUpFactor']}x ({cm}Mi -> {rm}Mi)")
        if not cc and not cm:
            blocked.append("no current request to compare against")
        significant = pct_change(cc, rc) >= g["minChangePct"] or pct_change(cm, rm) >= g["minChangePct"]
        if not significant:
            continue
        containers.append({
            "container": r.get("containerName"),
            "current": {"cpu": (r.get("latestKnownRequest") or {}).get("cpu"), "memory": (r.get("latestKnownRequest") or {}).get("memory"),
                        "cpuM": cc, "memMi": cm},
            "proposed": {"cpu": (r.get("recommendedRequest") or {}).get("cpu"), "memory": (r.get("recommendedRequest") or {}).get("memory"),
                         "cpuM": rc, "memMi": rm},
            "maxUsage": r.get("maxUsage"),
            "monthlySavings": round((r.get("monthlySavings") or {}).get("total", 0), 2),
            "blocked": blocked,
            "largeCut": env == "prod" and max(down_pct(cc, rc), down_pct(cm, rm)) > g["maxDownPctProd"],
        })
    if not containers:
        return None
    a = allocation_summary(alloc)
    savings = round(sum(c["monthlySavings"] for c in containers), 2)
    owner = cfg.owner(ns)
    return {
        "id": f"{ns}/{controller}",
        "planId": plan_id,
        "cluster": cfg.kc["cluster"],
        "namespace": ns,
        "controller": controller,
        "kind": "deployment",
        "env": env,
        "profile": profile_name,
        "team": label_value(ns_labels, "team") or label_value(ns_labels, "owner"),
        "owners": {"github": owner.get("github", []), "email": owner.get("email", [])},
        "containers": containers,
        "blocked": [b for c in containers for b in c["blocked"]],
        "largeCut": any(c["largeCut"] for c in containers),
        "monthlySavings": savings,
        "allocation": a,
        "projectedMonthlyCost": round(max((a or {}).get("monthlyCost", 0) - savings, 0), 2) if a else None,
        "status": "pending",
        "history": [{"time": now(), "event": "planned", "actor": "kcresize"}],
    }


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def make_plan(cfg, kc):
    """Query Kubecost and return (plan_id, proposals, skipped)."""
    plan_id = "plan-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    cluster = cfg.kc["cluster"]
    ns_labels = kc.namespace_labels(cluster)
    namespaces = sorted(n for n in (cfg.scope["namespaces"] or ns_labels.keys())
                        if not n.startswith("__") and in_scope(cfg, n, ns_labels))
    profiles = kc.profiles()
    alloc = kc.allocation_by_controller(cluster, namespaces, cfg.kc["allocationWindow"])
    proposals, skipped = [], []
    for ns in namespaces:
        env = env_of(cfg, ns_labels.get(ns))
        pname = cfg.profiles["prod"] if env == "prod" else cfg.profiles["default"]
        recs = kc.recommendations(cluster, ns, cfg.kc["window"], profiles.get(pname), kinds=cfg.scope["controllerKinds"])
        for ctrl in sorted({r.get("controllerName") for r in recs}):
            p = build_proposal(cfg, ns, ctrl, recs, alloc.get(f"{ns}/{ctrl}"), ns_labels.get(ns), pname, plan_id)
            (proposals if p else skipped).append(p or f"{ns}/{ctrl}")
    return plan_id, proposals, skipped


def drift_pct(approved, current_recs):
    """Largest % difference between what was approved and what Kubecost recommends now."""
    worst = 0.0
    by_c = {r.get("containerName"): r.get("normalizedRecommendedRequest") or {} for r in current_recs}
    for c in approved["containers"]:
        now_rec = by_c.get(c["container"])
        if not now_rec:
            return 1000.0
        for a, b in ((c["proposed"]["cpuM"], now_rec.get("cpuInMilliCores")), (c["proposed"]["memMi"], now_rec.get("memoryInMiB"))):
            if a and b is not None:
                worst = max(worst, abs(b - a) / a * 100)
    return round(worst, 1)


def spec_changed(approved, current_recs):
    """Kubecost's latestKnownRequest differs from the request the owner saw."""
    by_c = {r.get("containerName"): r.get("normalizedLatestKnownRequest") or {} for r in current_recs}
    for c in approved["containers"]:
        cur = by_c.get(c["container"]) or {}
        if cur and (cur.get("cpuInMilliCores") != c["current"]["cpuM"] or cur.get("memoryInMiB") != c["current"]["memMi"]):
            return True
    return False
