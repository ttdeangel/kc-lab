# Notification channels

Set the audiences per channel in `.github/kcresize.json`:
```json
"notify": {"owner": ["email", "slack"], "actioning": ["email", "slack", "webhook"]}
```
Secrets always come from repository secrets (environment variables), never from the config file.

## Email (SMTP)
| Secret | Example |
|---|---|
| `KCRESIZE_SMTP_HOST` | `smtp.gmail.com` · `email-smtp.us-east-1.amazonaws.com` · `smtp.office365.com` |
| `KCRESIZE_SMTP_PORT` | `587` (STARTTLS, the default) or `465` with `KCRESIZE_SMTP_TLS=ssl` |
| `KCRESIZE_SMTP_USER` / `KCRESIZE_SMTP_PASSWORD` | Gmail: your address plus a 16-character **app password** (Google Account → Security → App passwords; needs 2-Step Verification) |
| `KCRESIZE_SMTP_FROM` | `Kubecost Resize <finops@example.com>` |

Recipients come from `owners.<ns>.email` and `actioning.email`.

Even without SMTP, GitHub emails the owners it assigns to an issue, according to their own notification settings.

## Slack
- **Plain incoming webhooks** (no app):
  - Owner card: a "Review and decide" button that opens the issue.
  - Work orders: an "Open work order" button.
  - Secrets: `KCRESIZE_SLACK_OWNER_WEBHOOK`, `KCRESIZE_SLACK_ACTION_WEBHOOK`.
  - Per-namespace owner channel: put `"slack": "KCRESIZE_SLACK_PAYMENTS_WEBHOOK"` under `owners.<ns>` (the name of a secret), and pass that secret in the workflow `env:`.
- **Interactive buttons:**
  - Owner card: reason box plus Approve / Disapprove.
  - Work order: Apply via Kubecost / Decline.
  - Set up:
    1. Deploy `gateway/` behind HTTPS.
    2. Create the app from `examples/notifications/slack-app-manifest.yaml`.
    3. Set `SLACK_SIGNING_SECRET` on the gateway.
    4. Set repo variable `KCRESIZE_INTERACTIVE=1`.
    5. List Slack member IDs under `owners.<ns>.ids` / `actioning.ids` (Slack actors arrive as user IDs).

## Microsoft Teams (Workflows)
Office 365 connectors (classic incoming webhooks) were switched off in May 2026, so use **Workflows**:
1. In the channel: **⋯ → Workflows → "Post to a channel when a webhook request is received"**. Copy the URL into `KCRESIZE_TEAMS_OWNER_WEBHOOK` or `KCRESIZE_TEAMS_ACTION_WEBHOOK`.
2. kcresize posts `{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","content":{…}}]}`, which that template renders as-is.
3. **Interactive approvals** (optional, `KCRESIZE_INTERACTIVE=1`): build a Workflow with **"When a Teams webhook request is received" → "Post card in a chat or channel and wait for a response"** (card = `attachments[0].content`) → **HTTP POST** to `https://<gateway>/webhook`:
   - Header: `X-KC-Token: <GATEWAY_TOKEN>`
   - Body: `{"event": "@{body('Post_card')?['data']?['event']}", "workload": "...", "plan": "...", "actor": "@{body('Post_card')?['responder']?['email']}", "reason": "@{body('Post_card')?['data']?['reason']}"}`
   - The expression paths depend on how you name the actions in your flow, so adjust them.
   - List responder emails under `owners.<ns>.ids`. The HTTP action is a premium connector.

## Webhook (ServiceNow, Jira, PagerDuty, n8n, …)
- Set `KCRESIZE_WEBHOOK_URL` and optionally `KCRESIZE_WEBHOOK_SECRET`.
- Each event is a POST of JSON with `X-KC-Signature: sha256=<HMAC>`. See `examples/notifications/webhook-*.json`.
- To act back from those systems, POST to the gateway's `/webhook`:
  - Body: `{"event":"apply","workload":"ns/name","actor":"ops@example.com"}`
  - Auth: an HMAC signature or `X-KC-Token`.
  - Or call GitHub `repository_dispatch` directly: `event_type: kcresize`, with the same fields as `client_payload`.

## Testing channels
`KCRESIZE_NOTIFY_DRY_RUN=1` writes every payload to `.kcresize/outbox/` instead of sending it.
