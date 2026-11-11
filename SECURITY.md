# Security model

- **No cluster credentials in CI.** kcresize talks only to the Kubecost API. Kubecost's cluster controller makes the change. Protect the Kubecost API as you would anything that can resize workloads: internal network only, or an auth proxy (`KUBECOST_BASIC_USER`/`KUBECOST_AUTH_HEADER`).
- **Who can approve.** Only identities listed under `owners.<namespace>` (GitHub users, `@org/team`, chat IDs) can approve or disapprove. If a namespace has no owners, nobody can decide unless you set `KCRESIZE_ALLOW_ANY_OWNER=1`, which is lab use only.
- **Who can apply.** Only identities under `actioning` can apply or decline. If the list is empty, nobody can. The job also runs in the `kubecost-actioning` environment: add required reviewers there, and restrict the environment to the default branch.
- **Comment commands.** `issue_comment` workflows always run the workflow file from the default branch, so a pull request can't change the rules. Comments from anyone are parsed, but they're rejected unless the author is in the configured lists. GitHub does not start workflows for comments made with `GITHUB_TOKEN`, so the bot's own replies can't trigger anything.
- **Gateway.** It verifies the Slack signature (with a 5-minute replay window), an HMAC-SHA256 body signature, or a shared token, and forwards the request as `repository_dispatch`. Its GitHub token only needs permission to send `repository_dispatch` to this repository (fine-grained token: Contents read and write). Authorization always happens in the workflows.
- **Secrets.** Webhook URLs, SMTP passwords and tokens are read from environment variables only, never from `kcresize.json`. Outgoing webhooks are signed (`X-KC-Signature`).
- **Read-only mode.** `read-only: "true"` (and any `demo.kubecost.io` URL) sends GET requests only: no Kubecost writes, issues or notifications.
- **Overlapping automation.** `doctor` warns when another enabled Kubecost request-sizing action could resize the same workloads without approval. Scope or disable those actions.

Report vulnerabilities privately to the repository maintainers.
