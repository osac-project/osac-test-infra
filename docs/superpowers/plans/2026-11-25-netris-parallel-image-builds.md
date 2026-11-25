# Netris Parallel Image Builds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the OSAC component images in the background while the Netris lab and OCP deploy, so the three Netris E2E jobs finish about 24 minutes sooner.

**Architecture:** One script, `scripts/ec2-runner/build-components.sh`, replaces the build block that is copied into three workflows. A new early step starts the build detached; the existing step (same `id`, same position) waits for it, loads the images onto the node and emits the same `overrides` output.

**Tech Stack:** Bash, jq, podman, `oc debug`, GitHub Actions on a self-hosted EC2 runner.

**Spec:** `docs/superpowers/specs/2026-11-25-netris-parallel-image-builds-design.md`

## Global Constraints

- Only these workflows change: `e2e-vmaas-netris-full-install.yml`, `e2e-bmaas-netris-full-install.yml`, `e2e-stretched-netris-full-install.yml`. The four non-Netris workflows with a similar step are not touched.
- The build must use the commit from "Resolve osac CLI source revision" (`steps.osac-source.outputs.sha`), as today.
- The step that feeds "Deploy OSAC" keeps `id: components` and its `overrides` output (a JSON object, variable name to image tag). "Deploy OSAC" is not edited.
- Image tags stay `localhost/component-<index>:<GITHUB_RUN_ID>`. Override variable names stay `fulfillment_service_image`, `osac_operator_image`, `osac_aap_image`, `bmf_operator_image`.
- Podman flags stay as today: `--env GODEBUG=http2client=0 --unsetenv GODEBUG` for Containerfile builds.
- **The background build must not `pip install` into the system Python.** Today's step runs `/usr/bin/python3 -m pip install --ignore-installed 'ansible-builder~=3.0'`, which rewrites jinja2, PyYAML and cryptography in the site-packages that the concurrently running `ansible-playbook` uses. Install ansible-builder into a venv inside the state directory instead.
- Bash scripts start with `set -euo pipefail` and pass the repo's shellcheck pre-commit hook.
- Commit messages: `NO-ISSUE: <description>`, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.
- Do not start any E2E run without the user's go-ahead.

## Review Focus

1. Build fails while the lab is still deploying: the job must fail at the load step with the build log visible, not hang and not continue to "Deploy OSAC". (Task 2 test)
2. Builder process dies without writing a status (OOM, killed): the load step must fail promptly instead of waiting out its full budget. (Task 2 test)
3. Build still running when OCP is ready: the load step must wait, then proceed; and give up with a clear error after the wait budget. (Task 2 tests)
4. Extra `components` input with an unrecognised `imageKey`: must fail before any image is built, as an error naming the key. (Task 1 test)
5. Concurrent `ansible-playbook` on the same host: no pip writes to system site-packages from the build. (Task 1 test)

---

## File Structure

- Create `scripts/ec2-runner/build-components.sh` — all build/start/load logic.
- Create `.github/scripts/build-components-test.sh` — stubbed tests, same style as `.github/scripts/fabric-snapshot-test.sh` (stubs on `PATH`, a call log, plain bash assertions).
- Modify `.github/workflows/ci.yml` — add the test to "Shell script tests".
- Modify the three Netris workflows — swap the inline block for two script calls.

## Script interface (shared by all tasks)

```
build-components.sh build   # foreground build; used by `start` and by tests
build-components.sh start   # launch `build` detached, return immediately
build-components.sh load    # wait for the build, load images, emit overrides
```

Environment:

| Variable | Modes | Meaning |
|---|---|---|
| `COMPONENT_STATE_DIR` | all, required | directory for the files below |
| `COMPONENTS_JSON_INPUT` | build, start | extra components JSON array; empty or `[]` means core only |
| `OSAC_REPO`, `OSAC_BRANCH` | build, start | repo and pinned commit for the four core images |
| `GITHUB_RUN_ID` | build, start | image tag suffix |
| `KUBECONFIG`, `GITHUB_OUTPUT` | load | node access and step output |
| `COMPONENT_WAIT_SECONDS` | load | wait budget, default `1800` |
| `COMPONENT_POLL_SECONDS` | load | poll interval, default `10` |

Files in `COMPONENT_STATE_DIR`:

| File | Written by | Content |
|---|---|---|
| `build.log` | start | stdout and stderr of `build`, including `::group::` lines |
| `build.pid` | start | PID of the detached builder |
| `status` | start, last, via rename | exit code of `build` |
| `overrides.json` | build | `{"<var_name>": "<tag>", ...}` |
| `images.txt` | build | one tag per line, build order |
| `src/component-<i>/` | build | source checkouts |
| `ansible-builder-venv/` | build | venv holding ansible-builder |

---

### Task 1: `build` mode

**Files:**
- Create: `scripts/ec2-runner/build-components.sh`
- Create: `.github/scripts/build-components-test.sh`
- Modify: `.github/workflows/ci.yml` ("Shell script tests" step)

**Interfaces:**
- Produces: `build` mode, `overrides.json`, `images.txt` as specified above.

The logic to port is the `run:` block of "Build and load component images" in `.github/workflows/e2e-vmaas-netris-full-install.yml` (lines 445-559), minus the `oc`/`podman save` loading and minus the `$GITHUB_OUTPUT` write. Two deliberate differences from that block:

1. Every component's `imageKey` is mapped to its variable name **before** the first build, so an unrecognised key fails immediately.
2. ansible-builder is installed with `python3 -m venv "$COMPONENT_STATE_DIR/ansible-builder-venv"` and that venv's pip, and invoked from the venv.

- [ ] **Step 1: Write the test harness and failing tests**

Stubs on `PATH`, each appending its argv to `$STUB_LOG`: `git` (for `rev-parse HEAD` print `STUB_SHA`, default `abc123`), `podman`, `ansible-builder`, `python3` (for `-m venv <dir>` create `<dir>/bin/pip` and `<dir>/bin/ansible-builder` stubs that log as `venv-pip` / `venv-ansible-builder`). `podman build` exits `${STUB_PODMAN_RC:-0}`.

Tests, with `GITHUB_RUN_ID=777 OSAC_REPO=https://example.test/osac.git OSAC_BRANCH=deadbeef`:

- `test_build_core_only`: `COMPONENTS_JSON_INPUT=[]`. Exit 0. `overrides.json` equals `{"fulfillment_service_image":"localhost/component-0:777","osac_operator_image":"localhost/component-1:777","osac_aap_image":"localhost/component-2:777","bmf_operator_image":"localhost/component-3:777"}`. `images.txt` has those four tags in order. The log has four `podman build` lines; the three Containerfile ones contain `--env GODEBUG=http2client=0 --unsetenv GODEBUG`. Every `git ... fetch` line ends with `deadbeef`.
- `test_build_ignores_overridden_core_keys`: input `[{"repo":"x/y","ref":"main","imageKey":"service.images.service"},{"repo":"x/y","ref":"main","imageKey":"aap.configAsCode.eeImage"}]`. Still exactly four `podman build` lines and the same `overrides.json`.
- `test_build_rejects_unknown_image_key`: input `[{"repo":"x/y","ref":"main","imageKey":"nope.key"}]`. Non-zero exit, output contains `nope.key`, zero `podman build` lines.
- `test_build_uses_venv_for_ansible_builder`: after a core build the log contains `venv-pip` and `venv-ansible-builder` lines and no `python3 -m pip` line.
- `test_build_fails_when_podman_fails`: `STUB_PODMAN_RC=1`. Non-zero exit and no `overrides.json`.
- `test_requires_state_dir`: `COMPONENT_STATE_DIR` unset. Non-zero exit naming the variable.

- [ ] **Step 2: Run the tests and see them fail**

Run: `bash .github/scripts/build-components-test.sh`
Expected: fails because `scripts/ec2-runner/build-components.sh` does not exist.

- [ ] **Step 3: Implement `build` mode**

Write `overrides.json` and `images.txt` only after the last image builds (write to a temp name, then rename).

- [ ] **Step 4: Run the tests and shellcheck**

Run: `bash .github/scripts/build-components-test.sh && pre-commit run shellcheck --files scripts/ec2-runner/build-components.sh .github/scripts/build-components-test.sh`
Expected: all tests pass, shellcheck passes.

- [ ] **Step 5: Wire the test into CI**

Add `bash .github/scripts/build-components-test.sh` to the "Shell script tests" step in `.github/workflows/ci.yml`.

- [ ] **Step 6: Commit**

```bash
git add scripts/ec2-runner/build-components.sh .github/scripts/build-components-test.sh .github/workflows/ci.yml
git commit -m "NO-ISSUE: add a script that builds the Netris component images"
```

---

### Task 2: `start` and `load` modes

**Files:**
- Modify: `scripts/ec2-runner/build-components.sh`
- Modify: `.github/scripts/build-components-test.sh`

**Interfaces:**
- Consumes: `build` mode, `overrides.json`, `images.txt` from Task 1.
- Produces: `start` and `load` modes as specified in "Script interface".

`start` runs `build` in a new session (`setsid`, stdin from `/dev/null`) so it outlives the workflow step, redirects all output to `build.log`, records `build.pid`, and writes `status` by rename once `build` exits. It returns as soon as the builder is launched.

`load`, in order: wait until `status` exists; fail early if `build.pid` names a process that is gone and `status` is still absent (re-check `status` once after noticing, to close the race); fail after `COMPONENT_WAIT_SECONDS`; print `build.log`; exit 1 with `::error::` if `status` is not `0`; resolve the node with `oc get nodes -o jsonpath='{.items[0].metadata.name}'`; for each tag in `images.txt` run `podman save "<tag>" | oc debug "node/<node>" -t=false -- chroot /host podman load`; append `overrides=<compact overrides.json>` to `$GITHUB_OUTPUT`.

- [ ] **Step 1: Write the failing tests**

Add an `oc` stub (`get nodes` prints `node-a`; otherwise logs argv and drains stdin). `podman build` sleeps `${STUB_BUILD_SLEEP:-0}` seconds. Use `COMPONENT_POLL_SECONDS=1` throughout.

- `test_start_returns_before_build_finishes`: `STUB_BUILD_SLEEP=2`; `start` returns in under 2 seconds with `build.pid` present and `status` absent; within 15 seconds `status` contains `0`.
- `test_load_success`: after a finished successful `start`, `load` exits 0; its stdout contains a line from `build.log`; the log has four `oc debug node/node-a` lines; `$GITHUB_OUTPUT` has exactly one line, `overrides=` followed by the Task 1 JSON.
- `test_load_waits_for_running_build`: `STUB_BUILD_SLEEP=1`, call `load` immediately after `start`; exits 0 with four `oc debug` lines.
- `test_load_fails_on_build_failure`: `STUB_PODMAN_RC=1`; `load` exits non-zero, prints the build log, logs zero `oc debug` lines, writes nothing to `$GITHUB_OUTPUT`.
- `test_load_fails_when_builder_died`: state dir with `build.pid` holding the PID of an already-exited process and no `status`; `load` exits non-zero in under 5 seconds with `COMPONENT_WAIT_SECONDS=60`.
- `test_load_times_out`: state dir with `build.pid` of a live `sleep 30` and no `status`; with `COMPONENT_WAIT_SECONDS=2` `load` exits non-zero and the message names the timeout.

- [ ] **Step 2: Run the tests and see the new ones fail**

Run: `bash .github/scripts/build-components-test.sh`
Expected: Task 1 tests pass, the six new tests fail on the unknown mode.

- [ ] **Step 3: Implement `start` and `load`**

- [ ] **Step 4: Run the tests and shellcheck**

Run: `bash .github/scripts/build-components-test.sh && pre-commit run shellcheck --files scripts/ec2-runner/build-components.sh .github/scripts/build-components-test.sh`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/ec2-runner/build-components.sh .github/scripts/build-components-test.sh
git commit -m "NO-ISSUE: start the component image build detached and load it later"
```

---

### Task 3: Switch the three Netris workflows

**Files:**
- Modify: `.github/workflows/e2e-vmaas-netris-full-install.yml`
- Modify: `.github/workflows/e2e-bmaas-netris-full-install.yml`
- Modify: `.github/workflows/e2e-stretched-netris-full-install.yml`

**Interfaces:**
- Consumes: `start` and `load` modes from Task 2.

The same edit in each file.

- [ ] **Step 1: Add the start step directly after "Resolve osac CLI source revision"**

```yaml
      - name: Start component image builds
        shell: bash
        env:
          COMPONENTS_JSON_INPUT: ${{ inputs.components }}
          OSAC_REPO: ${{ inputs.osac-repo || 'https://github.com/osac-project/osac.git' }}
          OSAC_BRANCH: ${{ steps.osac-source.outputs.sha }}
          COMPONENT_STATE_DIR: ${{ runner.temp }}/component-build
        run: ./scripts/ec2-runner/build-components.sh start
```

- [ ] **Step 2: Replace the "Build and load component images" step**

Keep its position (after "Deploy OCP from snapshot") and `id: components`.

```yaml
      - name: Load component images
        id: components
        timeout-minutes: 45
        shell: bash
        env:
          KUBECONFIG: /root/.kube/config
          COMPONENT_STATE_DIR: ${{ runner.temp }}/component-build
        run: ./scripts/ec2-runner/build-components.sh load
```

- [ ] **Step 3: Check nothing else referenced the old step**

Run: `grep -rn "Build and load component images" .github infra docs README.md AGENTS.md scripts monitoring`
Expected: matches only in the four non-Netris workflows. Update any doc or monitoring rule that names the old step in a Netris context to the new step name.

- [ ] **Step 4: Lint**

Run: `pre-commit run --files .github/workflows/e2e-vmaas-netris-full-install.yml .github/workflows/e2e-bmaas-netris-full-install.yml .github/workflows/e2e-stretched-netris-full-install.yml`
Expected: yamllint and actionlint hooks pass.

Run: `for s in vmaas bmaas stretched; do diff <(sed -n '/name: Start component image builds/,/build-components.sh start/p;/name: Load component images/,/build-components.sh load/p' .github/workflows/e2e-vmaas-netris-full-install.yml) <(sed -n '/name: Start component image builds/,/build-components.sh start/p;/name: Load component images/,/build-components.sh load/p' .github/workflows/e2e-$s-netris-full-install.yml); done`
Expected: no output (the three files carry identical steps).

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/e2e-vmaas-netris-full-install.yml .github/workflows/e2e-bmaas-netris-full-install.yml .github/workflows/e2e-stretched-netris-full-install.yml
git commit -m "NO-ISSUE: build Netris component images while the lab and OCP deploy"
```

---

### Task 4: Prove it on real runs (needs the user's go-ahead)

- [ ] **Step 1: Ask the user before dispatching.** Each run holds an EC2 metal instance for about 2.5 hours, and the branch must be pushed first.

- [ ] **Step 2: Dispatch `e2e-pr-vmaas-netris.yml` and `e2e-pr-bmaas-netris.yml` against the branch.** Read each workflow's `workflow_dispatch` inputs for how to point it at a test-infra ref.

- [ ] **Step 3: Compare with the baseline**

Baseline (runs 45549747189 and 45536715224): `e2e` job 165 minutes VMaaS, 143 minutes BMaaS; "Deploy Netris lab" 18-19 minutes; "Deploy OCP from snapshot" 5-9 minutes.

Pass when: tests pass; "Start component image builds" takes seconds; "Load component images" takes about 4-6 minutes; the `e2e` job is at least 20 minutes shorter; "Deploy Netris lab" and "Deploy OCP from snapshot" are not more than about 3 minutes slower than baseline (the builds now compete with them for CPU and disk).

- [ ] **Step 4: Report the before/after step timings to the user.**
