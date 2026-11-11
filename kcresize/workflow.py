"""The approval state machine.

  pending --owner /approve--> approved --actioning /apply--> scheduled --Kubecost--> applied | failed
     |                           |                              (watch deletes the one-shot action)
     +--owner /disapprove--> rejected   +--actioning /decline--> declined
                                 +--TTL--> expired      +--drift / spec change at apply--> stale

Owners may only approve or disapprove (reason required). Only the actioning team may apply or decline.
Status changes after the owner's decision are written to the record (issue body + labels), not posted
as comments, so owners are not pulled into the action.
"""
import calendar
import os
import time

from . import notify
from .kubecost import maintenance_window_now
from .planner import drift_pct, make_plan, now, spec_changed
from .report import plan_summary_markdown
from .state import OPEN_STATES, is_member

TERMINAL = {"applied", "failed", "rejected", "declined", "stale", "expired"}


class Denied(Exception):
    """A request that is not allowed (wrong person, wrong state, missing reason)."""


def _ts(iso):
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


def _hist(p, event, actor, detail=""):
    p.setdefault("history", []).append({"time": now(), "event": event, "actor": actor, "detail": detail})


def _members(group):
    return list(group.get("github", [])) + list(group.get("ids", []))


def _summary(md):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n\n")


def output(**kw):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for k, v in kw.items():
                f.write(f"{k}={v}\n")


# ---------------------------------------------------------------- plan
def plan(cfg, kc, st):
    plan_id, proposals, skipped = make_plan(cfg, kc)
    links, created = {}, 0
    for p in proposals:
        if p.get("blocked"):
            continue
        existing = st.get(p["id"]) if not cfg.kc["readOnly"] else None
        if existing and existing["status"] in OPEN_STATES:
            links[p["id"]] = st.link(existing)
            p["status"] = existing["status"] + " (open)"
            continue
        if cfg.kc["readOnly"]:
            continue
        st.save(p)
        links[p["id"]] = st.link(p)
        notify.owner_request(cfg, p, links[p["id"]])
        created += 1
    md = plan_summary_markdown(plan_id, proposals, skipped, links)
    if cfg.kc["readOnly"]:
        md += "\n\n> Read-only mode: nothing was written and no owner was notified."
    _summary(md)
    output(plan=plan_id, proposals=created, savings=round(sum(p["monthlySavings"] for p in proposals if not p.get("blocked")), 2))
    print(md)
    return plan_id, proposals


# ---------------------------------------------------------------- owner decision
def decide(cfg, st, p, decision, actor, reason="", force=False, plan_id=None):
    """decision: approve | disapprove. Owners only."""
    reason = (reason or "").strip()
    if not p:
        raise Denied("No such proposal.")
    if plan_id and plan_id != p["planId"]:
        raise Denied(f"This message is for plan {plan_id}, but {p['id']} is now on {p['planId']}. Use the latest request.")
    if p["status"] != "pending":
        raise Denied(f"{p['id']} is {p['status']}; only a pending proposal can be decided.")
    owners = _members(cfg.owner(p["namespace"]))
    if not owners and os.environ.get("KCRESIZE_ALLOW_ANY_OWNER") != "1":
        raise Denied(f"No owners are configured for namespace {p['namespace']} (config owners.{p['namespace']}).")
    if owners and not is_member(st, actor, owners):
        raise Denied(f"{actor} is not an owner of {p['namespace']}.")
    if decision == "disapprove":
        if not reason:
            raise Denied("A reason is required to disapprove: /disapprove <reason>")
        p["status"], p["decidedBy"], p["decidedAt"], p["reason"] = "rejected", actor, now(), reason
        _hist(p, "disapproved", actor, reason)
        st.save(p, comment=f"Thanks @{actor}, your **disapproval** is recorded. No change will be made.")
        notify.actioning(cfg, p, "disapproved", reason, st.link(p))
        return p
    if decision != "approve":
        raise Denied(f"Unknown decision {decision}")
    if p.get("blocked"):
        raise Denied("This proposal is blocked by guardrails and cannot be approved: " + "; ".join(p["blocked"]))
    if p.get("largeCut") and not force:
        raise Denied(f"Large cut for a production workload: approve with `/approve --force {reason}`.")
    p["status"], p["decidedBy"], p["decidedAt"], p["reason"] = "approved", actor, now(), reason
    _hist(p, "approved", actor, reason)
    st.save(p, comment=f"Thanks @{actor}, your **approval** is recorded and passed to the actioning team.")
    notify.actioning(cfg, p, "work_order", reason, st.link(p))
    return p


# ---------------------------------------------------------------- actioning team
def _require_actioner(cfg, st, actor):
    team = _members(cfg.actioning)
    if not team:
        raise Denied("No actioning team is configured (config actioning.github / KCRESIZE_ACTIONERS); nobody can apply.")
    if not is_member(st, actor, team):
        raise Denied(f"{actor} is not on the actioning team.")


def _expired(cfg, p):
    return p["status"] == "approved" and time.time() - _ts(p["decidedAt"]) > cfg.guard["approvalTtlHours"] * 3600


def apply(cfg, kc, st, p, actor):
    if not p:
        raise Denied("No such proposal.")
    _require_actioner(cfg, st, actor)
    if p["status"] != "approved":
        raise Denied(f"{p['id']} is {p['status']}, not approved by its owner.")
    if _expired(cfg, p):
        p["status"] = "expired"
        _hist(p, "expired", "kcresize", f"not actioned within {cfg.guard['approvalTtlHours']}h")
        st.save(p)
        notify.actioning(cfg, p, "expired", "", st.link(p))
        raise Denied(f"The approval for {p['id']} has expired; re-plan.")
    profiles = kc.profiles()
    recs = kc.recommendations(p["cluster"], p["namespace"], cfg.kc["window"], profiles.get(p["profile"]), controller=p["controller"])
    if spec_changed(p, recs):
        p["status"] = "stale"
        _hist(p, "stale", "kcresize", "requests changed since the owner approved")
        st.save(p)
        notify.actioning(cfg, p, "stale", "requests changed since approval", st.link(p))
        raise Denied(f"{p['id']}: requests changed since the owner approved; re-plan.")
    d = drift_pct(p, recs)
    if d > cfg.guard["driftPct"]:
        p["status"] = "stale"
        _hist(p, "stale", "kcresize", f"Kubecost recommendation moved {d}% since approval")
        st.save(p)
        notify.actioning(cfg, p, "stale", f"recommendation drift {d}%", st.link(p))
        raise Denied(f"{p['id']}: Kubecost now recommends {d}% differently from what was approved; re-plan.")
    name = f"kcresize-{p['planId'][5:]}-{p['namespace']}-{p['controller']}"[:120]
    filt = f'cluster:"{p["cluster"]}"+namespace:"{p["namespace"]}"+controllerName:"{p["controller"]}"'
    aid = kc.create_resize_action(name, p["profile"], filt, cfg.kc["window"], maintenance_window_now())
    p.update(status="scheduled", actionId=aid, actionName=name, scheduledAt=now(), actionedBy=actor)
    _hist(p, "scheduled", actor, f"Kubecost action {aid}")
    st.save(p)
    notify.actioning(cfg, p, "scheduled", f"Kubecost action {aid} created by {actor}", st.link(p))
    output(action_id=aid)
    return p


def decline(cfg, st, p, actor, reason=""):
    if not p:
        raise Denied("No such proposal.")
    _require_actioner(cfg, st, actor)
    if p["status"] != "approved":
        raise Denied(f"{p['id']} is {p['status']}; only an approved proposal can be declined.")
    p.update(status="declined", actionedBy=actor, declineReason=reason or "no reason given")
    _hist(p, "declined", actor, reason)
    st.save(p)
    notify.actioning(cfg, p, "declined", reason, st.link(p))
    return p


# ---------------------------------------------------------------- watch
def watch(cfg, kc, st, timeout_hours=None):
    timeout_hours = timeout_hours or float(os.environ.get("KCRESIZE_ACTION_TIMEOUT_H", "6"))
    changed = []
    done = kc.completed("2d") if st.list({"scheduled"}) else []
    for p in st.list({"scheduled"}):
        mine = [a for a in done if a.get("configName") == p.get("actionName")
                and (a.get("parameters") or {}).get("namespace") == p["namespace"]
                and (a.get("parameters") or {}).get("controllerName") == p["controller"]]
        failed = [a for a in mine if "fail" in str(a.get("status", "")).lower() or "error" in str(a.get("status", "")).lower()]
        ok = [a for a in mine if str(a.get("status", "")).lower() == "success"]
        result = None
        if failed:
            result = ("failed", "; ".join(f"{(a.get('parameters') or {}).get('containerName')}: {a.get('status')} {a.get('error') or ''}" for a in failed))
        elif ok and len({(a.get("parameters") or {}).get("containerName") for a in ok}) >= len(p["containers"]):
            p["kubecostResult"] = [{"container": (a.get("parameters") or {}).get("containerName"),
                                    "from": f"{(a.get('parameters') or {}).get('lastCPURequest')}/{(a.get('parameters') or {}).get('lastRAMRequest')}",
                                    "to": f"{(a.get('parameters') or {}).get('targetCPURequest')}/{(a.get('parameters') or {}).get('targetRAMRequest')}",
                                    "completedAt": a.get("completedAt")} for a in ok]
            result = ("applied", ", ".join(f"{r['container']} {r['from']} -> {r['to']}" for r in p["kubecostResult"]))
        elif time.time() - _ts(p["scheduledAt"]) > timeout_hours * 3600:
            result = ("failed", f"Kubecost did not complete the action within {timeout_hours}h")
        if not result:
            continue
        p["status"] = result[0]
        p["completedAt"] = now()
        _hist(p, result[0], "kubecost", result[1])
        if p.get("actionId") and not cfg.kc["readOnly"]:
            try:
                kc.delete_action(p["actionId"])    # weekly window would otherwise re-run it
                _hist(p, "action-deleted", "kcresize", p["actionId"])
            except Exception as e:  # noqa: BLE001 - report, keep going
                _hist(p, "action-delete-failed", "kcresize", str(e)[:200])
        st.save(p)
        notify.actioning(cfg, p, result[0], result[1], st.link(p))
        changed.append((p["id"], result[0], result[1]))
    for p in st.list({"approved"}):
        if _expired(cfg, p):
            p["status"] = "expired"
            _hist(p, "expired", "kcresize", f"not actioned within {cfg.guard['approvalTtlHours']}h")
            st.save(p)
            notify.actioning(cfg, p, "expired", "", st.link(p))
            changed.append((p["id"], "expired", ""))
    if changed:
        _summary("## kcresize watch\n\n" + "\n".join(f"- `{w}`: **{s}** {d}" for w, s, d in changed))
    for w, s, d in changed:
        print(f"{s}: {w} {d}")
    if not changed:
        print("watch: nothing changed")
    return changed
