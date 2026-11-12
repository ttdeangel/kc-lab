#!/usr/bin/env bash
# HashiCorp Vault KV v2 -> kcbudget settings. Secret path: kv/kcbudget/<KCBUDGET_ENV>, one field per key.
# Runner auth is yours to choose: VAULT_TOKEN from an agent sink, AppRole, or the Kubernetes auth method.
set -euo pipefail
: "${VAULT_ADDR:?set VAULT_ADDR in the runner environment}"
vault kv get -format=json "kv/kcbudget/${KCBUDGET_ENV:?}" | jq '.data.data'
