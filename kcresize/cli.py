"""Command line: python -m kcresize <command> [...]

  doctor                          check Kubecost API access, controller heartbeat, profiles, overlapping actions
  plan                            create proposals (one GitHub issue each) and notify owners
  status                          list proposals
  approve    <ns/name> [--force] [--reason ...] --actor X     owner
  disapprove <ns/name> --reason ... --actor X                 owner (reason required)
  apply      <ns/name|--all> --actor X                        actioning team
  decline    <ns/name> [--reason ...] --actor X               actioning team
  watch                           confirm resizes via Kubecost, delete one-shot actions, expire approvals
  event                           handle the current GitHub event (issue_comment / repository_dispatch / workflow_dispatch)
  show       <ns/name>            print the proposal as markdown

Exit codes: 0 ok, 1 error, 3 request denied (wrong person / state / missing reason).
"""
import argparse
import json
import os
import re
import sys

from . import config, workflow
from .kubecost import Kubecost, KubecostError
from .report import issue_markdown
from .state import open_state

COMMANDS = {
    "/approve": "approve", "/disapprove": "disapprove", "/reject": "disapprove",
    "/apply": "apply", "/decline": "decline",
}


def _args(argv):
    ap = argparse.ArgumentParser(prog="kcresize", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command")
    ap.add_argument("workload", nargs="?")
    ap.add_argument("--config", default=None)
    ap.add_argument("--actor", default=os.environ.get("KCRESIZE_ACTOR") or os.environ.get("GITHUB_ACTOR", ""))
    ap.add_argument("--reason", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--plan", default=None)
    return ap.parse_args(argv)


def parse_comment(body):
    """'/approve --force looks fine' -> ('approve', True, 'looks fine'). Only the first line counts."""
    line = (body or "").strip().splitlines()[0].strip() if (body or "").strip() else ""
    m = re.match(r"^(/[a-z]+)\b(.*)$", line, re.I)
    if not m or m.group(1).lower() not in COMMANDS:
        return None, False, ""
    rest = m.group(2).strip()
    force = bool(re.match(r"^--force\b", rest))
    if force:
        rest = rest[len("--force"):].strip()
    return COMMANDS[m.group(1).lower()], force, rest


def handle_event(cfg, kc, st):
    """Map the GitHub event that started this run to a decision or an action."""
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        ev = json.load(f)
    if name == "issue_comment":
        if ev.get("action") != "created" or "pull_request" in ev.get("issue", {}):
            return 0
        cmd, force, text = parse_comment(ev["comment"]["body"])
        if not cmd:
            return 0
        actor, p = ev["comment"]["user"]["login"], st.get_by_issue(ev["issue"]["number"])
        plan_id = None
    elif name == "repository_dispatch":          # gateway (Slack / Teams buttons) or any signed webhook relay
        c = ev.get("client_payload") or {}
        cmd = {"approve_force": "approve"}.get(c.get("event"), c.get("event"))
        force, text, actor = c.get("event") == "approve_force" or bool(c.get("force")), c.get("reason", ""), c.get("actor", "")
        p, plan_id = st.get(c.get("workload", "")), c.get("plan")
    elif name == "workflow_dispatch":
        i = ev.get("inputs") or {}
        cmd, force, text = i.get("decision") or i.get("action"), str(i.get("force", "")).lower() == "true", i.get("reason", "")
        actor, p, plan_id = ev.get("sender", {}).get("login") or os.environ.get("GITHUB_ACTOR", ""), st.get(i.get("workload", "")), None
    else:
        print(f"event: nothing to do for {name}")
        return 0
    expected = os.environ.get("KCRESIZE_ROLE", "")   # owner | actioning: lets each workflow ignore the other's commands
    role = "owner" if cmd in ("approve", "disapprove") else "actioning"
    if expected and role != expected:
        print(f"event: {cmd} is handled by the {role} workflow")
        return 0
    workflow.output(command=cmd, actor=actor, workload=(p or {}).get("id", ""))
    return run(cfg, kc, st, cmd, p, actor, text, force, plan_id)


def run(cfg, kc, st, cmd, p, actor, reason="", force=False, plan_id=None):
    try:
        if cmd in ("approve", "disapprove"):
            p = workflow.decide(cfg, st, p, cmd, actor, reason, force, plan_id)
        elif cmd == "apply":
            p = workflow.apply(cfg, kc, st, p, actor)
        elif cmd == "decline":
            p = workflow.decline(cfg, st, p, actor, reason)
        else:
            raise workflow.Denied(f"unknown command {cmd}")
    except workflow.Denied as e:
        print(f"::warning::{e}")
        if p and p.get("_issue") and os.environ.get("GITHUB_EVENT_NAME") == "issue_comment":
            st.comment(p, f"@{actor} {e}")
        workflow.output(result="denied", message=str(e).replace("\n", " "))
        return 3
    print(f"{cmd}: {p['id']} -> {p['status']}")
    workflow.output(result="ok", status=p["status"])
    return 0


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv and os.environ.get("INPUT_COMMAND"):        # GitHub Action entry point
        import shlex
        argv = [os.environ["INPUT_COMMAND"]] + shlex.split(os.environ.get("INPUT_ARGS", ""))
    a = _args(argv)
    cfg = config.load(a.config)
    kc = Kubecost(cfg.kc["url"], read_only=cfg.kc["readOnly"])
    try:
        if a.command == "doctor":
            return doctor(cfg, kc)
        st = open_state(cfg)
        if a.command == "plan":
            workflow.plan(cfg, kc, st)
            return 0
        if a.command == "watch":
            workflow.watch(cfg, kc, st)
            return 0
        if a.command == "status":
            for p in sorted(st.list(), key=lambda x: x["id"]):
                print(f"{p['id']:40} {p['status']:10} {p['env']:8} ${p['monthlySavings']:>8}/mo  {st.link(p)}")
            return 0
        if a.command == "show":
            print(issue_markdown(st.get(a.workload)))
            return 0
        if a.command == "event":
            return handle_event(cfg, kc, st)
        if a.command in ("approve", "disapprove", "apply", "decline"):
            if a.command == "apply" and a.all:
                rc = 0
                for p in st.list({"approved"}):
                    rc = max(rc, run(cfg, kc, st, "apply", p, a.actor))
                return rc
            return run(cfg, kc, st, a.command, st.get(a.workload), a.actor, a.reason, a.force, a.plan)
        print(__doc__)
        return 1
    except KubecostError as e:
        print(f"::error::Kubecost: {e}")
        return 1


def doctor(cfg, kc):
    ok = True
    print(f"Kubecost  {cfg.kc['url']}  cluster {cfg.kc['cluster']}  read-only={cfg.kc['readOnly']}")
    try:
        hb = kc.heartbeat(cfg.kc["cluster"])
        print(f"  controller heartbeat: {hb or 'NONE (Actions will not run)'}")
        ok &= bool(hb)
    except KubecostError as e:
        print(f"  heartbeat: {e}")
        ok = False
    prof = kc.profiles()
    print(f"  profiles: {', '.join(prof) or 'none (falls back to built-in defaults)'}")
    for need in (cfg.profiles["prod"], cfg.profiles["default"]):
        if prof and need not in prof:
            print(f"  WARNING: profile {need} not found in Kubecost")
    try:
        others = [c for c in kc.action_configs() if c.get("action") == "containerRequestRightSizing" and c.get("scope") == "local"
                  and not str(c.get("name", "")).startswith("kcresize-") and (c.get("config") or {}).get("enabled")]
        for c in others:
            print(f"  WARNING: other request-sizing action can resize without approval: {c.get('name')} filter={(c.get('config') or {}).get('filter') or '<all>'}")
    except KubecostError as e:
        print(f"  action configs: {e}")
    print("doctor: OK" if ok else "doctor: problems found")
    return 0 if ok else 1
