# Changelog

## 1.1.0
- `kcctl`: one CLI for the lab (`init`, `preflight`, `up`, `destroy`, `ui`, …), GitHub wiring (`config`, `secrets`, `runner`, `doctor`), approvals (`plan`, `approve`, `disapprove`, `apply`, `decline`, `watch --wait`, with `--as` and `--github`), Actions (`run plan|watch`, `runs`, `issues`), `e2e`, `teardown`, `install` and shell completion. Works with macOS bash 3.2.
- `lab/setup.sh`: new `runner-remove` and `unconfigure` steps.

## 1.0.0 (initial release)
- Reusable composite Action `kubecost-resize-approvals`: `doctor`, `plan`, `event`, `approve`, `disapprove`, `apply`, `decline`, `watch`, `status`.
- Kubecost 3.x API client:
  - Recommendations: `requestSizingV2` with profile translation.
  - Cost evidence: Allocation API.
  - Changes: Actions (`/model/action/config`), with results from `/model/actions/completed`.
- Approval flow on GitHub Issues:
  - Owners use `/approve [--force]` or `/disapprove <reason>`.
  - The actioning team uses `/apply` or `/decline`, gated by the `kubecost-actioning` environment.
- Guardrails: minimum change, block large increases, `--force` for large prod cuts, drift and spec-change checks at apply, approval expiry, one-shot action cleanup, overlap warning.
- Notifications: email (SMTP), Slack (incoming webhooks plus interactive buttons), Microsoft Teams (Workflows plus Adaptive Cards), HMAC-signed webhook. Owners receive only the approval request.
- `gateway/`: Slack / Teams / webhook to `repository_dispatch` relay.
- `lab/`: kind + Kubecost 3.3.1 lab, sample workloads, GitHub setup with a self-hosted runner, local e2e script.
- Tests: unit and end-to-end tests against in-process Kubecost and GitHub stand-ins, plus a CI smoke test of `action.yml`.
