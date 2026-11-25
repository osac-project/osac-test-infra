# Build Netris component images in parallel with the lab deploy

## Intent

The Netris E2E jobs take about 170 minutes (VMaaS) and 150 minutes (BMaaS).
Every phase runs one after another on a single EC2 metal box. The component
image builds take about 28 minutes and wait for the lab and OCP even though
they need neither. Running them in the background while the lab and OCP come
up removes about 24 minutes per job without changing what is tested.

Measured on runs 45549747189 (VMaaS) and 45536715224 (BMaaS): builds about
24 minutes, node loads about 4 minutes, "Deploy Netris lab" plus "Deploy OCP
from snapshot" about 27 minutes.

## Goals

- The four OSAC images (Fulfillment Service, OSAC Operator, AAP execution
  environment, Bare-metal Fulfillment Operator) and any extra `components`
  input are built while the lab and OCP deploy.
- The same images, built from the same pinned commit, are loaded onto the
  node and passed to "Deploy OSAC" exactly as today.
- One copy of the build logic instead of three.

## Non-goals

- The four non-Netris workflows that carry a similar step
  (`e2e-vmaas-full-install`, `e2e-bmaas-full-install`, `e2e-caas-full-install`,
  `e2e-full-regression`).
- Building the images concurrently with each other, caching the AAP execution
  environment, baked AMIs, test-suite changes.

## Anti-goals

- A build failure that goes unnoticed, or that fails later than it does today.
- Images built from a different commit than the osac CLI checkout.
- Build output missing from the job log.

## Approaches

1. **Start the builds after `setup-infra`, load after OCP (chosen).** The
   pinned commit comes from the checkout `setup-infra` makes, so nothing about
   revision handling changes. The builds fit inside the lab-plus-OCP window.
2. **Start the builds at job start.** Would need the commit resolved before
   `setup-infra` and passed as `osac_branch`, which also feeds the AAP project
   branch and the "branch-built images" check. More risk, no extra saving.
3. **Bake the whole lab into an AMI.** Much larger saving, but needs identity,
   secret and DNS refresh on boot and an image-refresh pipeline. Set aside as
   too complicated for a first step.

## Design

`scripts/ec2-runner/build-components.sh` with two modes, called from
`e2e-vmaas-netris-full-install.yml`, `e2e-bmaas-netris-full-install.yml` and
`e2e-stretched-netris-full-install.yml`.

- **`build`** — a new step right after "Resolve osac CLI source revision"
  launches it in the background. It computes the component list (core plus
  filtered extras), clones and builds each image as today, and writes under a
  state directory in `$RUNNER_TEMP`: the full log, a results file mapping each
  override variable to its image tag, and an exit status file written last.
- **`load`** — replaces the "Build and load component images" step, keeping
  its `id: components` and position after "Deploy OCP from snapshot". It waits
  for the status file (bounded by the step timeout), prints the build log,
  fails if the build failed, loads each image onto the node with
  `podman save | oc debug ... podman load`, and writes the same `overrides`
  output. "Deploy OSAC" is unchanged.

Inputs stay environment variables with the same names as today
(`COMPONENTS_JSON_INPUT`, `OSAC_REPO`, `OSAC_BRANCH`, `KUBECONFIG`).

## Left to the builder

- How the background process is detached so it survives the end of its step.
- The state file names and the wait/poll interval.
- A stubbed test for the script in `.github/scripts/`, wired into `ci.yml`
  like `fabric-snapshot-test.sh`.

## Verification

Shellcheck and the `ci.yml` checks locally. Then one dispatched run each of
`e2e-pr-vmaas-netris.yml` and `e2e-pr-bmaas-netris.yml` against the branch:
tests pass, and the job is about 24 minutes shorter with the build step gone
from the critical path. Those runs need the user's go-ahead.
