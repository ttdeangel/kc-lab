# Kubecost 3.x API notes (verified)

Verified on Kubecost Helm chart **3.3.1** (free tier, Actions enabled with RSA signing keys, kind lab) in November 2026,
partly cross-checked read-only against demo.kubecost.io. All paths are on the frontend service (`:9090`).

| Endpoint | Use | Notes |
|---|---|---|
| `GET /model/savings/requestSizingV2` | Recommendations | `recommendedRequest`, `normalizedRecommendedRequest.{cpuInMilliCores,memoryInMiB}`, `latestKnownRequest`, `normalizedLatestKnownRequest`, `monthlySavings.{cpu,memory,total}`, `maxUsage`, `averageUsage`, `currentEfficiency`. `controllerKind` is lowercase. Filters: `cluster:"x"+namespace:"a","b"+controllerName:"d"`; `(…) \| (…)` also works. **`profile=` is rejected** (400): pass `algorithmCPU/RAM`, `targetCPUUtilization`/`targetRAMUtilization`, `qCPU`/`qRAM`. |
| `GET /model/savings/requestSizingV2/profiles` | Profile definitions | Built in: Development 0.8, Production 0.65, HighAvailability 0.5 (all `max`, min 10m / 20MiB). |
| `GET /model/allocation?aggregate=namespace,controller&accumulate=true` | Cost evidence | Keys `namespace/controller`. Use `minutes`, `totalCost`, `cpuCoreRequestAverage`, `cpuCoreUsageAverage`, `ramByte*Average`, `*Efficiency` and `properties.namespaceLabels` (label keys sanitised: `.` `/` `-` become `_`). |
| `POST /model/action/config` | Create action | `{source:"API", scope:"local", cluster:"all", name, action:"containerRequestRightSizing", config:{profile, filter, rightSizingWindow:"48h", enabled, cpu, memory, maintenanceWindow:"* H * * DOW\|\|…"}}` returns `data.id`. Cron hours are **UTC** and the window **repeats weekly**. |
| `GET` / `DELETE /model/action/config?id=` | Read / delete action | — |
| `GET /model/action/configs[?scope=local]` | List | New configs appeared about 20 minutes after creation. |
| `GET /model/action/heartbeats/latest` | Controller liveness | `{"<cluster>": "<time>"}`. Updates about every 10 to 20 minutes. |
| `GET /model/actions/runs?window=2d&limit=&offset=` | Runs | `{runId, actionConfigName, actionType, start, end, countRecommended, countSuccess, countPending, status}` |
| `GET /model/actions/completed?window=2d&limit=` | Per-container results | `{id, runId, configName, cluster, action, parameters:{namespace, controllerKind, controllerName, containerName, lastCPURequest, lastRAMRequest, targetCPURequest, targetRAMRequest}, status:"success", createdAt, completedAt}` |
| `GET /model/actions/savings?window=<start>,<end>` | Realized savings | `{totalSavings, window, items:[{completedAction, savings \| error}]}`. Needs pod cost history from **before** the resize; otherwise the item returns `error: "no pods found with window entirely before resize time"`. |
| `/model/cluster/requestsizer/planV2`, `/apply`, `/cluster/requestsizer/*` | Old plan/apply API | **404 in 3.3.1**, even though the IBM API directory still lists planV2. Use Actions. |
| `GET /model/v3/alerts` | Kubecost alerts | Types: budget, recurringUpdate, efficiency, spendChange, health, diagnostic, assetBudget. **None fires on right-sizing recommendations or Action runs.** |

## Timing observed in the lab
| Time (UTC) | Event |
|---|---|
| 23:49 | Action created |
| about 00:08 | Visible in `/action/configs` |
| 00:45 | Controller heartbeat |
| 00:47 | Run starts (`pending`) |
| 01:00 | `success`: 500m/512Mi → 10m/20Mi |

So expect **about 1 to 1.5 hours** from apply to resize.

## Caveats
- `latestKnownRequest` lags after a resize (still the old value an hour later). It comes from metrics, so don't use it to verify. kcresize verifies with `/actions/completed`.
- Use `window=2d` or more for runs and completed: `1d` resets at 00:00 UTC.
- Free tier: Actions are limited to 250 cores. Resource-quota right-sizing was Enterprise-only in 3.1.
