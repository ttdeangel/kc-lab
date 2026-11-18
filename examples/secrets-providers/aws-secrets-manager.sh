#!/usr/bin/env bash
# AWS Secrets Manager -> kcbudget settings. One JSON secret per environment named kcbudget/<KCBUDGET_ENV>,
# e.g. {"KUBECOST_URL":"...","KUBECOST_AUTH_HEADER":"...","KCBUDGET_WEBHOOKS":"{\"slack\":{...}}"}.
# Credentials: the runner's instance profile / IRSA role (secretsmanager:GetSecretValue on that ARN only).
set -euo pipefail
aws secretsmanager get-secret-value --secret-id "kcbudget/${KCBUDGET_ENV:?}" --query SecretString --output text
