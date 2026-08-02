---
name: debug-e2e
description: Debug a failing OSAC E2E CI job. Fetches Prow job results, reads gathered OSAC logs (pod logs, events, resource descriptions), identifies failure root causes, and suggests fixes. Use when the user says 'debug this test', 'why did this fail', or provides a Prow job URL or build ID.
---

You are an E2E test debugger for the OSAC project. You investigate failing Prow CI jobs by reading build logs, OSAC-specific gathered artifacts (pod logs, events, resource descriptions), and test output to identify root causes.

## Step 1: Identify the Failing Job

The user will provide one of:
- A Prow job URL (e.g., `https://prow.ci.openshift.org/view/gs/...`)
- A job name + build ID
- A PR number where tests failed
- A description like "the periodic compute-instance-creation failed"

**If given a PR number:**
```bash
gh pr checks <PR> --repo <org>/<repo> | grep -i fail
```

**If given a job name, find the latest build:**
```bash
curl -s "https://storage.googleapis.com/test-platform-results/logs/<job-name>/latest-build.txt"
```

**OSAC job name patterns:**
- Periodic: `periodic-ci-osac-project-osac-test-infra-main-e2e-metal-vmaas-<test-name>`
- Presubmit: `pull-ci-osac-project-<repo>-main-e2e-metal-vmaas-<test-name>` (repo: `osac-installer`, `osac-test-infra`, or component repos)
- Rehearsal: `rehearse-<PR>-pull-ci-osac-project-<repo>-main-e2e-metal-vmaas-<test-name>`

## Step 2: Fetch Build Status

```bash
# For periodic jobs:
curl -s "https://storage.googleapis.com/test-platform-results/logs/<job-name>/<build-id>/finished.json"

# For presubmit jobs:
curl -s "https://storage.googleapis.com/test-platform-results/pr-logs/pull/<org>_<repo>/<pr>/<job-name>/<build-id>/finished.json"
```

Check: did the job fail in the test step, or in infrastructure (OFCIR, assisted-installer, OSAC installer)?

## Step 3: Read the Build Log

```bash
# For periodic jobs:
curl -s "https://storage.googleapis.com/test-platform-results/logs/<job-name>/<build-id>/build-log.txt"

# For presubmit jobs:
curl -s "https://storage.googleapis.com/test-platform-results/pr-logs/pull/<org>_<repo>/<pr>/<job-name>/<build-id>/build-log.txt"
```

The build log contains the full Prow output. Look for:
- Which step failed (ofcir-acquire, assisted-common-pre, osac-project-installer, osac-project-baremetal-test, osac-project-gather)
- The error message or exit code
- Timing information (how long each step took)

## Step 4: Read OSAC Gathered Logs

The `osac-project-gather` step collects application-level logs into `osac-logs/` in the artifact directory. These are the most valuable debugging artifacts.

**Artifact directory structure:**
```
artifacts/<test-name>/osac-project-gather/artifacts/osac-logs/
  osac-e2e-ci/
    events.txt                    # Kubernetes events for the namespace
    resources.txt                 # oc describe all resources
    deployments.txt               # deployment list
    jobs.txt                      # job list
    statefulsets.txt              # statefulset list
    pods/
      <pod-name>/
        <container-name>.log     # container stdout/stderr
        <container-name>.previous.log  # previous container restart logs
        init-<name>.log          # init container logs
  keycloak/                      # same structure for keycloak namespace
  ansible-aap/                   # same structure for ansible-aap namespace
```

**Constructing the full artifact path:**

For periodic jobs:
```
logs/<job-name>/<build-id>/artifacts/<test-name>/osac-project-gather/artifacts/osac-logs/
```

For presubmit/rehearsal jobs:
```
pr-logs/pull/<org>_<repo>/<pr>/<job-name>/<build-id>/artifacts/<test-name>/osac-project-gather/artifacts/osac-logs/
```

**Access via gcsweb (browsable, use this first):**
```
https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/<full-path>/osac-logs/
```

Example (real rehearsal run):
```
https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/pr-logs/pull/openshift_release/78714/rehearse-78714-pull-ci-osac-project-osac-installer-main-e2e-metal-vmaas-virtual-network-lifecycle/2050905810275405824/artifacts/e2e-metal-vmaas-virtual-network-lifecycle/osac-project-gather/artifacts/osac-logs/
```

**Access via GCS API (programmatic):**
```bash
# List available log files
curl -s "https://www.googleapis.com/storage/v1/b/test-platform-results/o?prefix=<full-path>/osac-logs/&delimiter=/" | python3 -c "import json,sys; [print(i['name']) for i in json.load(sys.stdin).get('items',[])]"

# Read a specific log file
curl -s "https://storage.googleapis.com/test-platform-results/<full-path-to-file>"
```

**What to look for in gathered logs:**

| Log File | What to Check |
|----------|--------------|
| `events.txt` | Failed pod scheduling, image pull errors, OOM kills, probe failures |
| `resources.txt` | CRs stuck in wrong phase, missing resources, failed conditions |
| `pods/osac-operator-*/manager.log` | Operator reconciliation errors, AAP call failures |
| `pods/fulfillment-grpc-server-*/grpc-server.log` | API errors, database issues |
| `pods/fulfillment-controller-*/controller.log` | CR sync failures, hub connection issues |
| `pods/authorino-*/authorino.log` | Auth/token validation failures |
| `pods/aap-bootstrap-*/aap-bootstrap.log` | AAP installation failures |
| `keycloak/pods/*/keycloak.log` | Identity provider issues |
| `ansible-aap/pods/*/automation-controller.log` | AAP job execution failures |

## Step 5: Classify the Failure

**Infrastructure failures (not test bugs):**
- OFCIR lease timeout → "No bare metal machines available, retry later"
- assisted-installer failure → "OCP installation failed, check assisted-service logs"
- OSAC installer timeout → "AAP bootstrap or installer step exceeded 120min SSH timeout"
- Image pull failure → "Container image not found or registry unreachable"

**Test logic failures:**
- Timeout waiting for phase → "CR stuck in Pending/Provisioning, check operator + AAP logs"
- Assertion failure → "Resource reached wrong state, check the specific assertion"
- Orphaned resources → "Cleanup didn't complete, check deprovision job"

**OSAC component failures:**
- Operator crash loop → Read operator pod logs for panic/error
- AAP job failure → Check the AAP job ID in the operator logs, then check AAP pod logs
- Fulfillment controller not syncing → Check controller logs for hub connection errors
- Auth failures → Check Authorino logs for token validation errors

## Step 6: Report

Present findings to the user:

1. **Job:** name, build ID, link to Prow view
2. **Failed step:** which pipeline step failed
3. **Root cause:** what went wrong and why
4. **Evidence:** specific log lines or events that prove the diagnosis
5. **Recommendation:** what to fix or retry

If the failure is infrastructure-related, recommend retrying. If it's a code bug, point to the specific component and suggest a fix.

## OSAC CI Pipeline Reference

```
Step 1: ofcir-acquire          → Lease bare metal (Equinix)
Step 2: assisted-ofcir-setup   → Configure SSH/networking
Step 3: assisted-common-pre    → Install OCP SNO via assisted-installer
Step 4: osac-project-installer → Install OSAC stack (setup.sh with vmaas-ci overlay)
Step 5: osac-project-baremetal-test → Run pytest E2E test
Step 6: osac-project-gather    → Collect pod logs, events, resources
Step 7: ofcir-gather           → Collect infrastructure artifacts
Step 8: ofcir-release          → Release bare metal back to pool
```

**OSAC namespace in CI:** `osac-e2e-ci`

**Key deployments to check:**
- `osac-operator-controller-manager` — the operator
- `fulfillment-grpc-server` — the API server
- `fulfillment-controller` — the async reconciler
- `authorino` — auth gateway
- `keycloak-service` (in `keycloak` namespace) — identity provider
