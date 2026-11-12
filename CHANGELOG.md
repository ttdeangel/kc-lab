# Changelog

## 1.3.0
- `kcbudget`: Kubecost budgets as code. Budget files per environment, synced to the Kubecost Budget API (source of truth).
  - `validate`: names, scopes, alerts, channel names, email domains, pool allocation headroom.
  - `plan` / `apply [--prune]`: only `kcb-` budgets are managed; UI budgets untouched; `--max-deletes`; webhooks masked.
  - `smoke`: create/read/update/delete a throwaway budget. `doctor`: detects documented vs REST budget paths.
  - `check`: reads budgets from Kubecost, measures period spend (Allocation API), alerts once per threshold per period via Teams Adaptive Cards, email or Slack.
  - `request`: Microsoft Forms (Power Automate → `repository_dispatch`) → validated pull request.
- Reusable workflow `kcbudget-deploy.yml`, with lab, prod (environment-gated), hourly check and request callers.
- `budgets/lab.json`: 50 example lab budgets (cluster envelope, tripwires, real namespaces, 20-team example org, 3 pools).

## 1.2.0
- `kcctl pause` / `kcctl resume`: spin the lab down and up without losing data.
  - Pause refuses while a resize is in flight, takes the repo variables off so workflows skip, stops the cluster, and stops Colima if unused.
  - Resume reverses that, waits for Kubecost and runs `doctor`.
- `kcctl env` shows whether the lab is paused.

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
