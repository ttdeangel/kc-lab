#!/usr/bin/env bash
# Azure Key Vault -> kcbudget settings. Secret names can't contain '_', so store e.g.
# "kcbudget-prod--KUBECOST-URL" and map '--' to the env prefix and '-' to '_'.
# Credentials: the runner's managed identity (az login --identity), Key Vault Secrets User role.
set -euo pipefail
VAULT="${KCBUDGET_KEYVAULT:?set KCBUDGET_KEYVAULT in the runner environment}"
az login --identity --allow-no-subscriptions >/dev/null
prefix="${KCBUDGET_ENV:?}--"
for id in $(az keyvault secret list --vault-name "$VAULT" --query "[?starts_with(name, '$prefix')].name" -o tsv); do
  key="${id#"$prefix"}"; key="${key//-/_}"
  printf '%s=%s\n' "$key" "$(az keyvault secret show --vault-name "$VAULT" --name "$id" --query value -o tsv)"
done
