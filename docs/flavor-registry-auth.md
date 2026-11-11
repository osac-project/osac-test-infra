# Flavor registry authentication in CI

The CaaS full-install and full-regression workflows opt in to isolated flavor
credentials in `fetch-and-write-secrets`. The action merges the Vault outputs
directly into a unique directory under `RUNNER_TEMP` (0700), with an `auth.json`
file (0600). It never copies this credential source back from a shared HOME file.
The existing shared auth and stage/prod merge-base consumers are unchanged.
The namespace-scoped `quay.io/osac-project` entry remains valid and unchanged.

`FLAVOR_REGISTRY_AUTH_FILE` identifies only the isolated flavor credentials.
Digest inspection passes that file explicitly to `skopeo inspect --authfile`.
For a pull, sudo preserves only that variable and `FLAVOR_IMAGE`; the helper
checks the root context and supplies `REGISTRY_AUTH_FILE` to cluster-tool.
`cluster-tool pull --server local` uses `ExecutionEnv.run` to launch a local
shell running `skopeo copy`, which inherits that environment. Selecting local
explicitly matches `machine-init.sh` and avoids relying on SSH environment
forwarding. This does not add support for remote cluster-tool servers.

The immediately following `always()` step removes the private authfile and
directory, including on preparation failure or cancellation. Creation failures
also remove partial files. Cleanup does not remove unrelated registry auth.
As with any job cleanup, abrupt runner loss or a forced process kill can prevent
execution; runner temporary-directory cleanup remains the final fallback.

Diagnostics emit a selected-source label, readiness, entry count, effective
UID/GID, numeric tool versions, cluster-tool source hash, operation exit status,
and a fixed failure category. They never emit auth contents, registry responses,
raw child stdout/stderr, image names, or credential-bearing headers. Error text
is examined only to select an allowlisted category: auth-denied, TLS, network,
image-not-found, registry-unavailable, or unclassified. Categories are hints,
not proof of a historical failure's cause. Pull output is intentionally captured
until completion; a long pull/retry can be quiet after `starting=true`.

Missing, malformed, unreadable, or permissively accessible credential files
have distinct preflight diagnostics. A failed digest probe still yields an empty
digest to the existing workflow logic. Existing-flavor skip, missing-marker
seeding, equal-marker skip, and changed-marker replacement behavior is unchanged.

## Offline regression checks

Run the script test as in `.github/workflows/ci.yml`:

```sh
python3 -I .github/scripts/test_flavor_registry.py
```

Tests use fake credentials and local fake executables only. They check explicit
selection, the modeled sudo environment boundary and inner shell inheritance,
permissions, malformed/missing credentials, failure categorization, non-disclosure,
workflow wiring, and cleanup. They do not prove real sudo policy, registry access,
or the cluster-tool version installed on a runner. No live registry or cluster
access is needed.
