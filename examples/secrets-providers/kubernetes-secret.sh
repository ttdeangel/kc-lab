#!/usr/bin/env bash
# Kubernetes Secret -> kcbudget settings, for runners in the cluster (Actions Runner Controller).
# kubectl create secret generic kcbudget-prod --from-literal=KUBECOST_URL=... --from-file=KCBUDGET_WEBHOOKS=webhooks.json
# The runner's service account needs get on that one Secret. (Mounting the Secret as env on the runner
# pod also works and needs no script at all: process env wins.)
set -euo pipefail
kubectl get secret "${KCBUDGET_ENV:?}" -o json | jq '.data | map_values(@base64d)'
