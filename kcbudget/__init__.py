"""kcbudget: Kubecost budgets as code.

Budgets are declared in a JSON file per environment (budgets/<env>.json), reviewed in pull requests,
and synced to the Kubecost Budget API, which is the source of truth at run time. Alerts use the
budget's own Kubecost actions (email / Slack / Microsoft Teams), with an optional repo-side checker
for channels Kubecost cannot deliver to directly.
"""
__version__ = "1.3.0"
