# Private local holdout evaluator

Harnessa includes an optional local evaluator for narrow, owner-authored acceptance
suites. It is a separate process and container. It accepts a finalized immutable
Harness² manifest, reads only its content-addressed composed patch artifacts, and
returns a manifest-bound pass/fail receipt. It makes no model or external service
calls. The private suite and verifier files stay outside this repository.

This evaluator is suitable for inert-content tasks such as the disposable M13
acceptance repository's README changes. Its verifier scripts must treat candidate
files as data and must not import or execute code from the patched repository. Use
a separately permissioned benchmark evaluator with its own task isolation for
general program-execution grading. Do not describe this owner-authored suite as an
independent benchmark or claim broad agent improvement from it.

## Data and trust boundary

Create the private dataset directory on the operator host with mode `0700`; make
`suite.json`, source archives, and verifier files mode `0600`. It contains:

```text
private-holdout/
├── suite.json
├── bases/<opaque-name>.tar
└── verifiers/<verifier-id>.py
```

The suite is versioned locally and has an explicit dataset ID, revision, and
license. Use a valid SPDX identifier for a published dataset. Use a project-owned
`LicenseRef-*` identifier for private, owner-authored fixtures whose terms are
limited to the operator. Each case binds one Harnessa task and project to the
exact base commit, source archive digest, and verifier ID:

```json
{
  "schema_version": 1,
  "dataset_id": "m13-acceptance-holdout",
  "dataset_revision": "1",
  "dataset_license": "LicenseRef-Harnessa-Private-OwnerAuthored",
  "cases": [
    {
      "task_id": "<Harnessa task UUID>",
      "project_id": "<Harnessa project UUID>",
      "base_sha": "<40 or 64 character commit ID>",
      "source_archive": "bases/<opaque-name>.tar",
      "source_archive_sha256": "<SHA-256>",
      "verifier_id": "readme-safety-v1"
    }
  ]
}
```

The evaluator verifies the canonical manifest digest, exact task/project/base
identity, archive checksum, content-addressed patch digest, and archive paths.
It rejects symlinks and path traversal. At startup it verifies each configured
source archive checksum, requires bounded source and verifier files, preflights
tar structure, member paths/types, decompressed size, and file data without
executing code, and loads the exact bytes into an immutable in-memory snapshot.
Readiness is unavailable
when startup validation fails. Host-side changes after startup do not alter the
active evaluator snapshot; restart the service to load a new dataset revision.
Each evaluation checks the captured bytes, extracts the captured archive, and
runs the captured verifier from a private temporary copy. Receipts contain only
`status`, the manifest digest, and a judge revision bound to the suite and
validated asset fingerprints. It suppresses
verifier stdout/stderr and never returns individual test outcomes or expected
values. The Harnessa worker reserves the configured evaluator maximum in the
shared spend ledger before dispatch; the local evaluator itself makes no paid
request.

The service mounts the Harnessa artifact volume and private dataset read-only. It
runs on a private, no-egress network with a loopback-only host port, no Linux
capabilities, a read-only root filesystem, a bounded temporary filesystem, and
container CPU/memory/process ceilings. It receives no database, Redis, GitHub App,
LiteLLM, or provider credentials. Keep its bearer token in a mode-`0600` local
secret file; the same token is passed only to the local host worker and evaluator
process. Do not mount the dataset or evaluator network into ACP/agent containers.

## Local setup

Add the following values to a private `.env` file at the repository root. Generate
a fresh random bearer token; do not use a provider or GitHub credential:

```dotenv
HARNESS_HOLDOUT_DATASET_DIR=/absolute/path/to/private-holdout
HARNESS_HOLDOUT_EVALUATOR_PORT=19094
HARNESS_HOLDOUT_EVALUATOR_URL=http://127.0.0.1:19094/evaluate
HARNESS_HOLDOUT_EVALUATOR_TOKEN=<random bearer token>
HARNESS_HOLDOUT_EVALUATOR_MAX_COST_USD=0.01
```

Start only the optional grader profile and verify readiness:

```sh
podman compose --env-file .env -f infra/compose.yaml \
  --profile holdout-evaluator up -d holdout-evaluator
curl --fail http://127.0.0.1:19094/health/ready
```

The default Compose worker cannot reach a loopback host service. For this local
profile, use the trusted host worker required for Podman ACP and give it the same
URL/token through its protected environment. Keep the Compose worker stopped for
this local evaluator endpoint. A production evaluator must use an explicitly
managed private endpoint and its own one-way network policy.

Keep the dataset directory in operator backup/restore scope, separately from
Harnessa's task database, artifact volume, and secret references. Record its
revision, license, case count, source archive checksums, verifier revision, and
the resulting evaluator receipt digest in the acceptance report; do not copy
expected values or verifier output into the report.
