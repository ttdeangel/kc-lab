# Lab: full end-to-end test on a fresh kind + Kubecost

Verified setup:
- Intel Mac mini (6 cores / 32 GB)
- Colima 0.10.3 (4 CPU, 16 GiB)
- kind v0.33.0 with `kindest/node:v1.37.0`
- Kubecost chart 3.3.1, free tier, Actions enabled with RSA signing keys
- SeaweedFS S3 as the federated store (MinIO's public images stopped working in Sept 2026)

| File | What |
|---|---|
| `kc-lab.sh` | Builds the lab: Colima, kind, metrics-server, object store, Kubecost with Actions, UI on `http://localhost:$UI_PORT` |
| `ec2-bootstrap.sh` | Prepares an Amazon Linux / Ubuntu EC2 host for `kc-lab.sh` (run with `LEGACY_WORKLOADS=0`) |
| `workloads.yaml` | Over-requested sample apps in `tbm-payments`, `shop-prod` (env=prod), `shop-dev`, labelled `kcresize.io/approval=required` |
| `lab.env.example` | All lab settings (cluster, port, people, runner). Copy it to `lab/lab.env` |
| `setup.sh` | Connects the lab to your GitHub repo: config, variables, environment, labels, secrets, self-hosted runner |
| `render-config.py` | Writes `.github/kcresize.json` from `lab.env` |
| `e2e-local.sh` | Same flow without GitHub (file state), for a quick check |

## 1. Build the lab (next to an existing one)
All names come from `lab/lab.env`. The defaults keep a new lab separate from any existing kind + Kubecost lab:

| Setting | Default | Why |
|---|---|---|
| `CLUSTER` | `kcresize-lab` | A different kind cluster and Kubecost clusterId; an existing cluster is never reused |
| `UI_PORT` / `KUBECOST_URL` | `9091` / `http://localhost:9091` | An existing lab usually holds 9090 |
| `WORKDIR` | `./.lab/<cluster>` | Its own signing keys, values and credentials |
| `COLIMA_PROFILE` | `default` | Shares the running Colima VM; a second VM only adds overhead |
| `LEGACY_WORKLOADS` | `0` | Skips resource-hog, which burns about 2 cores |

`destroy` deletes only this cluster and never stops Colima while other clusters exist.

```bash
git clone https://github.com/ttdeangel/kc-lab && cd kc-lab
cp lab/lab.env.example lab/lab.env && $EDITOR lab/lab.env     # set OWNER_EMAIL etc.
./lab/kc-lab.sh preflight      # CPU / memory / disk in the Colima VM, port free, other clusters
./lab/kc-lab.sh up             # about 15 min: kind cluster, Kubecost 3.3.1 + Actions, sample workloads
```
After a reboot, run `./lab/kc-lab.sh runtime`.

**Resources:**
- One extra lab adds about 2 to 3.5 cores of real CPU, 3 to 4 GiB of used memory (about 11 GiB requested), and 8 to 12 GiB of disk.
- If the VM is short on CPU, pause the other lab while testing. Its data is kept:
  - Pause: `docker stop <old-cluster>-control-plane`
  - Resume: `docker start <old-cluster>-control-plane`, or `./kc-lab.sh runtime` from that lab.

Wait **at least 2 to 3 hours** so Kubecost has usage data. Recommendations use a 48h window, but are useful after a few hours.

## 2. Connect GitHub
```bash
brew install gh && gh auth login
./lab/setup.sh config      # .github/kcresize.json from lab.env (owner + actioning = you by default)
./lab/setup.sh github      # vars KUBECOST_URL/CLUSTER/RUNS_ON, environment kubecost-actioning, labels
./lab/setup.sh secrets     # optional: SMTP (Gmail app password), Slack, Teams, webhook
./lab/setup.sh runner      # self-hosted runner on this Mac (label = CLUSTER): it can reach localhost:$UI_PORT
./lab/setup.sh doctor
git add .github/kcresize.json && git commit -m "lab config" && git push
```
Optional second sign-off: Settings → Environments → `kubecost-actioning` → Required reviewers.

## 3. Run the end-to-end test
| Step | Do | Expect |
|---|---|---|
| Plan | Actions → **kcresize - plan** → Run workflow (tick read-only first to preview) | Job summary table. One issue per workload, labelled `kcresize:pending` and assigned to you. Approval request email. |
| Owner check | On the `tbm-payments/fraud-scorer` issue, comment `/disapprove` | Bot replies that a reason is required. Status unchanged. |
| Owner decides | Comment `/approve traffic is flat` | Owner workflow runs. Issue becomes `kcresize:approved`. **Work order** email to the actioning address. |
| Separation check | From an account not in `actioning`, comment `/apply` | Actioning workflow fails with "not on the actioning team" |
| Apply | Comment `/apply` (approve the environment if reviewers are set) | Issue becomes `kcresize:scheduled`. Kubecost action id appears in the issue, and in Kubecost UI → Savings → Actions. |
| Kubecost runs it | Wait about 60 to 90 min. **kcresize - watch** runs every 15 min. | Issue becomes `kcresize:applied` and closes. History shows `400m/512Mi -> 10m/20Mi`. **Applied** email. The action is deleted. |
| Verify | `kubectl --context kind-$CLUSTER -n tbm-payments get deploy fraud-scorer -o jsonpath='{.spec.template.spec.containers[0].resources.requests}'` | `{"cpu":"10m","memory":"20Mi"}` |
| Large prod cut | On `shop-prod/api`, `/approve` then `/approve --force` | First is refused, the second is accepted |
| Disapprove | On `tbm-payments/checkout-api`: `/disapprove PCI freeze` | Closed as `rejected`. Actioning team gets "No action needed". |

To check without GitHub: `OWNER_EMAIL=you@example.com ./lab/e2e-local.sh` (export the `KCRESIZE_SMTP_*` variables to send real email).

## 4. Reset
```bash
kubectl --context kind-$CLUSTER apply -f lab/workloads.yaml    # restore the original requests
./lab/kc-lab.sh destroy                                       # remove only this lab
gh issue list -l kcresize -s all       # old issues (close or delete as you like)
```

## Troubleshooting
| Symptom | Check |
|---|---|
| `doctor`: no controller heartbeat | `kubectl -n kubecost get pods`; Actions need `clusterController` plus signing keys on the free tier (`kc-lab.sh` sets this up) |
| Plan finds nothing | Kubecost needs usage history. Check `scope.namespaceLabel` matches the namespace labels; Kubecost reports them sanitised, e.g. `kcresize_io_approval`. |
| Workflows queued forever | The self-hosted runner is offline: `cd $RUNNER_DIR && ./svc.sh status` |
| `preflight`: port in use | Another lab or app holds `UI_PORT`; change it in `lab/lab.env` |
| Scheduled for more than 2h | `curl -s "$KUBECOST_URL/model/actions/runs?window=2d"`. Status `failed` after 6h (`KCRESIZE_ACTION_TIMEOUT_H`). |
| `/actions/savings` error | Expected for workloads with no cost history before the resize |
