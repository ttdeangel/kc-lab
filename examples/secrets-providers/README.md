# Secrets providers (customer carve-out)

kcbudget never reads secrets from GitHub. At runtime it runs `KCBUDGET_SECRETS_COMMAND` (if set) on the
runner and reads its stdout: a JSON object or `KEY=VALUE` lines. Implement it with whatever your
organization already uses. Point the GitHub (environment) variable `KCBUDGET_SECRETS_COMMAND` at it.
The variable holds a path, not a secret.

## Contract
| | |
|---|---|
| Input | Environment variable `KCBUDGET_ENV` (the GitHub Environment name, e.g. `kcbudget-prod`) |
| Output | stdout: `{"KUBECOST_URL": "...", "KCBUDGET_WEBHOOKS": "{...json...}", ...}` or `KEY=VALUE` lines |
| Accepted keys | `KUBECOST_*`, `KCBUDGET_*`, `KCRESIZE_SMTP_*` (others are ignored with a warning) |
| Exit code | non-zero fails the run, and stderr is shown (keep secrets out of stderr) |
| Limits | 60-second timeout; run without a shell (`shlex` split), so wrap pipelines in a script |
| Precedence | runner process env > secrets command > `~/.kcbudget/<env>.env` |
| Masking | in Actions, loaded credential and webhook values are masked in the log |

## Keys you will usually return
| Key | Needed for |
|---|---|
| `KUBECOST_URL` | everything except `validate` |
| `KUBECOST_AUTH_HEADER` or `KUBECOST_BASIC_USER` / `KUBECOST_BASIC_PASSWORD` | Kubecost behind auth |
| `KCBUDGET_WEBHOOKS` | Slack/Teams channel names in budget files → URLs (JSON string) |
| `KCRESIZE_SMTP_*` | `check --channels email` |

Use separate credentials per environment: `kcbudget-prod-plan` (read-only, for plan and check) and
`kcbudget-prod` (write, for apply behind required reviewers).

## Examples (starting points; adapt paths, names and auth)
- [`vault.sh`](vault.sh): HashiCorp Vault KV v2, with runner auth by AppRole or Kubernetes.
- [`aws-secrets-manager.sh`](aws-secrets-manager.sh): one JSON secret per environment (instance role or IRSA).
- [`azure-key-vault.sh`](azure-key-vault.sh): one secret per key (managed identity).
- [`kubernetes-secret.sh`](kubernetes-secret.sh): a Secret in the runner's namespace (ARC runners).

Test locally: `KCBUDGET_ENV=kcbudget-lab ./vault.sh | python3 -c 'import sys,json; print(sorted(json.load(sys.stdin)))'`
(prints key names only).
