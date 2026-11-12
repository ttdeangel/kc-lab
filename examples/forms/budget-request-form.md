# Microsoft Forms: "Kubecost budget request"

Settings: **Only people in my organization can respond**, **Record name** on (gives the requester email).
Use the question titles below; the Power Automate body maps them by name.

| # | Question | Type | Choices / validation | Payload field |
|---|---|---|---|---|
| 1 | Environment | Choice (required) | `lab`, `prod` | `environment` |
| 2 | Team | Text (required) | short team name, e.g. `search-relevance` | `team` |
| 3 | Cost center | Text (required) | e.g. `CC-1234` | `costCenter` |
| 4 | Budget pool | Choice (required) | `bu-commerce`, `bu-platform`, `bu-data` (match `pools` in the budget file) | `pool` |
| 5 | Scope type | Choice (required) | `namespace`, `cluster` | `scopeType` |
| 6 | Namespaces or clusters | Text (required) | comma-separated, e.g. `search-prod, search-dev` | `scopeValues` |
| 7 | Reset interval | Choice (required) | `Monthly`, `Weekly` | `interval` |
| 8 | Reset day | Number | Monthly 1-28; Weekly 0-6 (0 = Sunday). Default 1 | `intervalDay` |
| 9 | Budget amount (USD per interval) | Number (required) | > 0 | `amount` |
| 10 | Alert thresholds | Choice, multiple answers (required) | `50%`, `75%`, `80%`, `90%`, `100%`, `120%` | `thresholds` |
| 11 | Who should get alert emails? | Text | `;`-separated addresses (the requester is always added) | `emails` |
| 12 | Slack channel | Choice | `None`, `finops-alerts`, `commerce-alerts`, `platform-alerts`, `data-alerts` (keys of `KCBUDGET_WEBHOOKS.slack`) | `slackChannel` |
| 13 | Teams channel | Choice | `None`, `finops`, `commerce`, `platform`, `data` (keys of `KCBUDGET_WEBHOOKS.teams`) | `teamsChannel` |
| 14 | Justification | Long text (required) | | `justification` |

The budget name is generated: `kcb-<team>-<ns|cl>-<first scope value>-<interval>`. Submitting again with
the same team, scope and interval updates that budget (the PR says "replaces existing").
