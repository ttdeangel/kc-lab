# Notification samples

Every file here was produced by a real dry run (`KCRESIZE_NOTIFY_DRY_RUN=1`) of the test suite, so it
is exactly what kcresize sends. Workload: `tbm-payments/fraud-scorer` (400m/512Mi → 10m/20Mi).

| Audience | Channel | File |
|---|---|---|
| Owner | GitHub issue (the record) | `github-issue-fraud-scorer.md` |
| Owner | Email | `email-owner-approval-request.html` |
| Owner | Slack (with buttons, `KCRESIZE_INTERACTIVE=1`) | `slack-owner.json` |
| Owner | Teams (Adaptive Card with reason box) | `teams-owner.json` |
| Owner | Webhook | `webhook-approval_requested.json` |
| Actioning | Email: work order / disapproval FYI / applied | `email-actioning-*.html` |
| Actioning | Slack: work order (Apply / Decline buttons), FYI, scheduled, applied | `slack-action-*.json` |
| Actioning | Teams | `teams-action-*.json` |
| Actioning | Webhook events | `webhook-*.json` |

**Owners receive only the approval request.** All action messages go to the actioning team.

## Preview

| Channel | How to preview |
|---|---|
| Slack | Paste the JSON into the Block Kit Builder (app.slack.com/block-kit-builder) |
| Teams | Paste `attachments[0].content` into the Adaptive Card Designer (adaptivecards.io/designer) |
| Email | Open the `.html` file in a browser |

## Webhook signature
When `KCRESIZE_WEBHOOK_SECRET` is set, each POST carries `X-KC-Signature: sha256=<hex HMAC-SHA256 of the raw body>`:
```python
hmac.compare_digest(sig[7:], hmac.new(secret, raw_body, hashlib.sha256).hexdigest())
```
The payload has `event`, `audience` (`owner` | `actioning`), `workload`, `plan`, `status`, `link`,
`detail` and the full `proposal`. The events are:

| Audience | Events |
|---|---|
| Owner | `approval_requested` |
| Actioning | `work_order`, `disapproved`, `scheduled`, `applied`, `failed`, `declined`, `stale`, `expired` |

See `slack-app-manifest.yaml` for interactive Slack and `../../docs/notifications.md` for Teams Workflows, email and ServiceNow/Jira.
