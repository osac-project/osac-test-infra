# Install failures from defective cluster snapshots

Status: identified, ongoing as of 2026-11-23. Started 2026-11-05.

Install failures doubled weekly in the osac mono-repo CI from Nov 5 and are
still ongoing. About 25 to 40 percent of full-install runs fail at the
Install OSAC step. No pull request caused this. The e2e clusters boot from
the mutable flavor tag `quay.io/osac-project/cluster-flavors:sno-4-22`, and
new snapshots pushed to that tag in early November carried broken state.

The machine-readable record behind this story is
[`config/ci-observe/incidents.yml`](config/ci-observe/incidents.yml)
(`INC-2026-11-install`): the `incidents` tool of the ci-observe MCP serves it
for any window it overlaps, and the health and failure answers name the
overlap in one line.

## How the broken snapshots reached the runners

The snapshots were rebuilt with larger disks to mitigate runner disk
pressure ([cluster-tool PR #45](https://github.com/osac-project/cluster-tool/pull/45),
created 2026-11-06 14:30, hours before the onset).

The flavor freshness check in the e2e workflows should have moved the
runners to them. It ran skopeo without credentials against the private Quay
repo and failed silently. The runners kept their cached snapshots from July,
so nobody noticed the new snapshots were defective.

## Regime 1: Nov 6 to 15

Runners that lost their local flavor, through disk-pressure cleanups or new
machines, fresh-pulled the new snapshot and failed. Their clusters showed
kubevirt-migration-controller reconcile errors and optimistic-locking
storms. The failure signature shows 30 failed runs and 0 passing ones.
First seen 2026-11-06T16:46:35Z in [run 42255503074](https://github.com/osac-project/osac/actions/runs/42255503074).

## Regime 2: Nov 16 onward

[osac-test-infra PR #797](https://github.com/osac-project/osac-test-infra/pull/797)
(merged 2026-11-16T01:20Z) fixed the digest check. Every runner then
re-pulled the current snapshot, digest
`sha256:e58fe43c367aa8f32c310cbd2a4a2e7b040897abbb7dcea9397e2ad43014ab71`.

Installs began failing differently: cert-manager could not take its
leader-election lease, there was no default-ca ClusterIssuer, no
certificates were issued, and every TLS-mounting pod failed to start. The
signature shows 58 failed runs and 0 passing ones. First seen
2026-11-16T09:37:53Z, in the [osac runs of 2026-11-16](https://github.com/osac-project/osac/actions/runs/43865187352)
(run 43865187352).

## Why the failure is intermittent

The snapshot's defect is a boot-time race, not a constant. A failing run
(43865187352, Nov 16 12:17) and a passing run (44193072179, Nov 17 22:45)
booted the identical flavor digest.

## The separate CaaS configuration issue

A CaaS CI configuration issue had independently caused about half of the
CaaS merge-queue runs to fail for weeks before any of this. It is
post-mortem point 4 and is not part of this incident.

## Follow-ups

1. Rebuild the sno-4-22 snapshot from a known-good cluster. Verify
   cert-manager leader election and the default-ca ClusterIssuer after
   boot.
2. Test new snapshots with the full regression suite before pushing them to
   the flavor tag, and alert when runners are not picking up the current
   digest (from the post-mortem).
3. Move the flavor tag to versioned, immutable references with an explicit
   rollout, instead of one mutable tag.
4. The exact Quay push timestamps are still unverified; the registry API
   requires auth.

## Evidence

- [Post-mortem in #wg-infra (iskornya, 2026-11-16 08:59 UTC)](https://redhat-internal.slack.com/archives/C0B8KR3HK16/p1794819597154149)
- [osac-test-infra PR #797, the flavor-refresh fix that triggered the mass rollout](https://github.com/osac-project/osac-test-infra/pull/797)
- [cluster-tool PR #45, snapshot disk sizes](https://github.com/osac-project/cluster-tool/pull/45)
- [First regime-1 run 42255503074 (2026-11-06 16:46:35Z)](https://github.com/osac-project/osac/actions/runs/42255503074)
- [First regime-2 failure window, osac runs of 2026-11-16](https://github.com/osac-project/osac/actions/runs/43865187352)
- [ci-observe: failure signatures, vmaas merge-queue, Nov 6-15 and Nov 16-23 windows](https://osac-ci.redhat.com:3000/d/ci-observe-failures/ci-observe-e28094-why-runs-fail)
