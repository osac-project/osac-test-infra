# Flavor registry authentication in CI

The CaaS full-install and full-regression workflows opt in to isolated flavor
credentials in `fetch-and-write-secrets`. The action merges the Vault outputs
directly into a unique directory under `RUNNER_TEMP` (0700), with an `auth.json`
file (0600). It never copies this credential source back from a shared HOME file.
The existing shared auth and stage/prod merge-base consumers are unchanged.
The namespace-scoped `quay.io/osac-project` entry remains valid and unchanged.

`FLAVOR_REGISTRY_AUTH_FILE` identifies only the isolated flavor credentials.
Digest inspection passes that file explicitly to `skopeo inspect --authfile`.
For a pull, the unprivileged helper supplies `REGISTRY_AUTH_FILE` to the existing
allowed command, `sudo -n -u root REGISTRY_AUTH_FILE=<private-path> /usr/bin/python3 /usr/local/bin/cluster-tool pull`.
This explicit assignment is checked against sudoers and rejects a stale restrictive
policy instead of silently dropping the path and falling back to shared credentials.
The provisioning policy in `scripts/machine-init.sh` preserves that variable
only for `CLUSTER_TOOL_PULL`; it does not grant SETENV or allow execution of a
checkout script as root. Runners must have this policy update deployed before
using isolated flavor pulls. Updating source alone does not update installed
sudoers; this PR does not mutate runner configuration. Diagnostics report the
helper's effective UID/GID and sudo's requested target UID, not an observed child UID.
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
and a fixed failure-category hint. They never emit auth contents, registry responses,
raw child stdout/stderr, image names, or credential-bearing headers. Error text
is examined only to select an allowlisted category: auth-denied, TLS, network,
image-not-found, registry-unavailable, permission-denied, execution-policy, or unclassified. Combined
manifest-not-found/404 and auth errors are labeled `image-not-found-or-auth`,
not attributed to credentials alone. Generic "denied" is not an auth signature.
The actual cluster-tool failure message comes from its final `sys.exit` on stderr;
its stdout progress messages are not used to classify registry failures. Categories are hints,
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
CLUSTER_TOOL_SOURCE=/path/to/cluster-tool python3 -I .github/scripts/test_flavor_registry_consumer.py
```

Tests use fake credentials and local fake executables only. They check explicit
selection, the modeled sudo environment boundary and inner shell inheritance,
permissions, malformed/missing credentials, failure categorization, non-disclosure,
workflow wiring, and cleanup. CI checks out a pinned public cluster-tool source
and executes its actual `cmd_pull` and local `ExecutionEnv.run` with fake skopeo,
intercepting filesystem work and retry delays. This checks environment inheritance
and final failure channels without vendoring upstream or executing sudo.
Tests do not prove installed sudo policy, registry access,
or the cluster-tool version installed on a runner. No live registry or cluster
access is needed.

## Merge and lookup precedence

The merge retains the existing jq recursive-object semantics: pull-secret,
snapshot-registry, then the namespace auth field. More-specific repository
entries remain intact and take precedence over namespace auth in containers/image.
The helper reports only `authfile_scope` labels (repository, namespace, registry,
legacy-registry, ambiguous-registry, unmatched, or unresolved-reference), never
arbitrary keys. This is the matching scope in the explicitly selected file, not
proof of a successful registry login or of external credential-helper configuration.
Validation accepts the containers/image auth schema, including empty helper-marker
entries, optional identitytoken strings, and base64 auth with CR/LF. As in the
existing action, only the auths map is merged; top-level credential helpers are
not copied. The image library decides whether an empty entry supplies usable auth.
