# GitHub App integration

GitHub support is opt-in per connected Harnessa repository. App configuration stores only public IDs and opaque secret references. Private key and webhook secret values must remain in deployment-managed environment variables; the worker and API resolve a reference through `GITHUB_SECRET_REFERENCES_JSON` and never return the resolved value.

## Configure the deployment

Set `GITHUB_SECRET_REFERENCES_JSON` to a JSON object mapping opaque references to the deployment environment variable names `GITHUB_APP_PRIVATE_KEY` and `GITHUB_WEBHOOK_SECRET`, for example `{"harnessa-github-private-key":"GITHUB_APP_PRIVATE_KEY","harnessa-github-webhook":"GITHUB_WEBHOOK_SECRET"}`. Set those variables through the API and worker secret stores; the Compose file forwards only these GitHub secrets into those trusted services. Set `GITHUB_API_URL` only to an HTTPS GitHub API origin/prefix. The webhook body limit defaults to 256 KiB and the retry limit to 12.

Create a GitHub App with **Checks: write**, **Contents: write**, **Issues: write**, **Pull requests: write**, and **Commit statuses: write** permissions. Subscribe to `installation`, `installation_repositories`, `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `check_run`, and `check_suite`. Configure the webhook URL as `/webhooks/github` and use the deployment secret mapped above. The app must be installed with repository selection enabled; Harnessa still requires an explicit repository opt-in.

Register its App ID, webhook ID, and secret references as an organization administrator through `POST /github/apps`. Register the installation through `POST /github/installations`, then verify it with `POST /github/installations/{installation_uuid}/refresh`. The refresh reports missing permissions and suspension. Opt a connected Harnessa repository into one matching GitHub repository with `POST /projects/{project_id}/repositories/{repository_id}/github`; the request must include the installation UUID, GitHub numeric repository ID, one or more required check names, and the issue topic label. GitHub repository name and ID are checked before the link is stored.

Map GitHub numeric user IDs to active organization users through `POST /github/actors`. A mapping does not grant additional Harnessa roles: interview creation and answers still pass through the existing project authorization checks. Topic issues need the configured label. Clarification comments must use `/harnessa answer <question-uuid> r<revision>: <answer>` and be authored by a mapped user with the existing project operator permission.

## Delivery and repair

The webhook route verifies `X-Hub-Signature-256` against the raw request bytes, then stores the payload, payload digest, delivery identity, and processing intent in one PostgreSQL transaction. PostgreSQL uniqueness on App configuration and delivery ID is authoritative. The worker claims durable processing intents with leases and retries; Redis is not the source of webhook durability. The authenticated `GET /github/status` endpoint reports connection state, delivery counters, and recent delivery errors without returning webhook payloads or secret values. Organization administrators can retry failed or ignored deliveries with `POST /github/deliveries/{delivery-row-uuid}/retry` and a reason.

Repository opt-out disables further inbound processing for that link. Installation removal or suspension revokes linked repository setup state. Refresh permissions and explicitly opt the repository in again after recovering installation access.

## Current implementation boundary

Issue opening, structured clarification answers, issue edit conflict detection, webhook deduplication, delivery repair, durable review-branch/PR publication, and current-head check-run capture are implemented. A bounded five-minute poll refreshes tracked PR state and required checks, including exact-head merge verification and recoverable closed-unmerged/head-drift handling. Review-change polling, installation permission/removal reconciliation, default-branch drift, durable archive/learning/cleanup execution, and outbound issue/status effects remain pending. Live GitHub App setup and disposable-repository acceptance require an operator-provisioned App, installation, secrets, and explicit repository authorization; do not treat a webhook receipt or issue flow as full M10 acceptance.
