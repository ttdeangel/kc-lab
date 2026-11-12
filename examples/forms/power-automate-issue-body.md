# Power Automate: GitHub connector "Create an issue"

No token is created or stored in GitHub: the GitHub connector signs in with OAuth as the account you
choose (use a bot/service account with write access to the repo; the workflow only accepts issues from
OWNER / MEMBER / COLLABORATOR authors).

Flow:
1. **When a new response is submitted** (Microsoft Forms) → **Get response details**
2. **Create an issue** (GitHub): Repository owner `ttdeangel`, Repository name `kc-lab`
   - **Title**: `[kcbudget-request] @{outputs('Get_response_details')?['body/<question-id-team>']}`
   - **Body**: the text below. Replace each `<question-id-…>` with the dynamic content from step 1.
     Keep the ```json fence exactly as shown.

````
Budget request submitted through Microsoft Forms. The kcbudget-request workflow validates it and opens a pull request.

```json
{
  "environment": "@{outputs('Get_response_details')?['body/<question-id-environment>']}",
  "requester": "@{outputs('Get_response_details')?['body/responder']}",
  "team": "@{outputs('Get_response_details')?['body/<question-id-team>']}",
  "costCenter": "@{outputs('Get_response_details')?['body/<question-id-cost-center>']}",
  "pool": "@{outputs('Get_response_details')?['body/<question-id-pool>']}",
  "scopeType": "@{outputs('Get_response_details')?['body/<question-id-scope-type>']}",
  "scopeValues": "@{outputs('Get_response_details')?['body/<question-id-scope-values>']}",
  "interval": "@{outputs('Get_response_details')?['body/<question-id-interval>']}",
  "intervalDay": "@{outputs('Get_response_details')?['body/<question-id-reset-day>']}",
  "amount": "@{outputs('Get_response_details')?['body/<question-id-amount>']}",
  "thresholds": @{string(outputs('Get_response_details')?['body/<question-id-thresholds>'])},
  "emails": "@{outputs('Get_response_details')?['body/<question-id-emails>']}",
  "slackChannel": "@{outputs('Get_response_details')?['body/<question-id-slack>']}",
  "teamsChannel": "@{outputs('Get_response_details')?['body/<question-id-teams>']}",
  "justification": "@{replace(outputs('Get_response_details')?['body/<question-id-justification>'], '\"', '''')}",
  "responseId": "@{triggerOutputs()?['body/resourceData/responseId']}"
}
```
````

Notes:
- `thresholds` is a multi-choice answer. Forms returns it as a JSON-array string, and `string()` inserts it as-is. If you
  prefer, quote it like the others; kcbudget accepts both forms.
- Quotes in free text would break the JSON, so the justification expression removes them.
- In a public repository, issue text is public. Use a private repository for production requests.
