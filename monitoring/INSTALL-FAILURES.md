# Install failures: the AAP readiness race after the snapshot refresh

Status: identified, ongoing as of 2026-11-23 (PR-suite retest bursts). Started
2026-11-05.

Install failures in the osac mono-repo CI rose sharply from Nov 5, peaked
Nov 16 to 20 (merge-queue pass rate 57 percent) and continue in bursts. No
pull request caused this. The install fails when the AAP stack is not ready
yet, and a fleet-wide snapshot refresh made every run take the same path into
that race at the same time.

The machine-readable record behind this story is
[`config/ci-observe/incidents.yml`](config/ci-observe/incidents.yml)
(`INC-2026-11-install`): the `incidents` tool of the ci-observe MCP serves it
for any window it overlaps, and the health and failure answers name the
overlap in one line.

## What fails

The `Install OSAC` step runs `osac-aap-bootstrap`, a Job that talks to the
AAP API through the AAP gateway. The gateway pod has two containers: an
envoy proxy and the gateway behind it. The proxy becomes healthy first, and
until the gateway is up, envoy answers every request with `HTTP 503, "no
healthy upstream"`.

The bootstrap Job retries (backoffLimit 15) and helm waits 40 minutes. The
run passes when the AAP rollout finishes inside that budget and fails when
it does not.

This was reproduced live: a clone of the exact CI flavor, the exact CI
install sequence, and the bootstrap job failing three times with the same
503 before succeeding on the fourth attempt and completing the playbook.

## Why it is intermittent

The AAP rollout time is dominated by image pulls. The cluster flavor
pre-caches the OpenShift payload and the recert image, but not the AAP
images, so every install pulls multiple gigabytes from the registry. Pull
time varies with registry and network conditions and with how many installs
run at once.

That is the whole pattern:

- Failures come in tight bursts that match retest waves; the same minute has
  failures and successes on different runners, and the same pull request
  fails and passes inside one burst.
- During the worst window (Nov 16 to 21) the serial, off-peak scheduled runs
  passed 83 percent with install failures not even their top cause, while
  the concurrent merge queue passed 57 percent with install failures at 66
  percent of its failures.
- The passing run
  [44967627752](https://github.com/osac-project/osac/actions/runs/44967627752)
  contains four failed `osac-aap-bootstrap` pods with the same 503. The race
  strikes passing runs too; only the retry budget separates the outcomes.
- A failing run and a passing run booted the identical flavor digest.

## How the fleet got here

The e2e clusters boot from one mutable flavor tag,
`quay.io/osac-project/cluster-flavors:sno-4-22`, cached per runner behind a
digest file. The freshness check ran `skopeo inspect` without credentials
against the private Quay repo, failed silently, and the workflow then always
chose "could not check remote digest, skipping pull". Runners kept July-era
snapshots, which carried a 250-pod limit (OSAC-5886).

New snapshots were published in early November: an OCP 4.22.14 rebuild, image
built 2026-11-09T15:07Z, digest
`sha256:e58fe43c367aa8f32c310cbd2a4a2e7b040897abbb7dcea9397e2ad43014ab71`.
They sat in Quay, unadopted.

[osac-test-infra PR #797](https://github.com/osac-project/osac-test-infra/pull/797)
(OSAC-5886) fixed the digest check and merged the night of Nov 15-16. Within
hours every runner compared digests, re-pulled, and booted the new snapshot
at once, into a merge-queue backlog. Install failures jumped on Nov 16,
first at 09:37 UTC. Two changes are candidates for the jump: the snapshot
switch itself, and an AAP operator channel promotion
(`aap-operator.v2.6.0-0.1787258256`, already present in the Nov 16 failing
bundles, promotion date not discoverable from Quay).

From Nov 16 to 20 the AAP failure also took a second form: the AAP project
update ran but its `git checkout` of the pull request's head SHA failed with
`fatal: unable to read tree`, an AAP-side refspec problem with fork SHAs.
The failures subsided on Nov 20 18:29 UTC. Nothing was fixed that day; the
timing converged.

## What the snapshot actually is

The Nov 9 snapshot is healthy. Two clones of the exact digest booted on the
r450 server: a bare OCP 4.22.14 SNO, no cert-manager, no OSAC components,
no leftover state, recert clean. The full install against it succeeded.

It carries one real, latent defect, confirmed on both clones: OVN's `br-ex`
lands on the secondary network with `net.ipv4.ip_forward = 0`, while the
MetalLB VIPs sit on the primary network. That breaks externally-facing VIP
traffic, which is the CaaS guest API failure the team's post-mortem
described. It does not break pod egress or the API path CI uses. The CI
workflows mitigate it with forwarding configuration
([#828](https://github.com/osac-project/osac-test-infra/pull/828),
[#832](https://github.com/osac-project/osac-test-infra/pull/832)); the
placement itself is still wrong in the snapshot.

## The other failures that week

Three more causes overlapped the same window, all fixed:

- The CaaS ingress coin flip (OSAC-7419, present since July 15): the wildcard
  DNS pointed at the first Agent's IP while the single ingress router
  replica could land on either of two workers. Fixed by
  [osac PR #2407](https://github.com/osac-project/osac/pull/2407) on Nov 16
  21:52 UTC.
- The AWS-credentials rotation in Vault broke the CaaS DNS steps on Nov 15.
  Fixed by [#825](https://github.com/osac-project/osac-test-infra/pull/825).
- The OVN forwarding defect above.

## Why the telemetry pointed at cert-manager

For a week, the evidence looked like cert-manager dying: terminated
containers, leader-election errors, missing CA secrets, in 58 failed runs
and 0 passing ones. It was an artifact.

Full log gathers exist only for failed runs, so everything the gatherer
itself emits appears in 100 percent of failed runs and 0 percent of passing
ones. The gatherer's own boilerplate scored as a perfect failure
discriminator: `oc logs --previous` on pods that never restarted produces
"previous terminated container not found" in every bundle, and every
`oc describe pod` prints the `node.kubernetes.io/unreachable` toleration
line. Cert-manager's 20 to 30 second leader-election RBAC lag, during which
its pods start before their ServiceAccounts and lease Role exist, is a
normal transient of every fresh install; it was reproduced on a healthy
clone that completed a fully successful install while logging the same
lines.

The lesson is now a follow-up: the signature ranking must not treat
failure-only gather output as signal.

## Follow-ups

1. Pre-cache the AAP images in the cluster flavor. The readiness race window
   is the registry pull time.
2. Make `osac-aap-bootstrap` wait for the AAP gateway's actual health
   instead of in-cluster pings, or raise its retry budget.
3. Fix the AAP project's git fetch so fork PR-head SHAs resolve.
4. Fix the ci-observe signature selection bias: filter gatherer boilerplate
   or gather passing runs too.
5. Fix the snapshot's OVN `br-ex` placement, or clean the ovnk interface
   hints in cluster-tool's restore.
6. Test new snapshots with the full regression suite before pushing them to
   the flavor tag, and alert when runners are not picking up the current
   digest (from the post-mortem).
7. Determine whether the `aap-operator.v2.6.0-0.1787258256` promotion
   contributed to the Nov 16 jump.
8. Move the flavor tag to versioned, immutable references with an explicit
   rollout.

## Evidence

- [Post-mortem in #wg-infra (iskornya, 2026-11-16 08:59 UTC)](https://redhat-internal.slack.com/archives/C0B8KR3HK16/p1794819597154149)
- [osac-test-infra PR #797, the flavor-refresh fix](https://github.com/osac-project/osac-test-infra/pull/797)
- [cluster-tool PR #45, snapshot disk sizes](https://github.com/osac-project/cluster-tool/pull/45)
- [Passing run 44967627752 with four failed osac-aap-bootstrap pods](https://github.com/osac-project/osac/actions/runs/44967627752)
- [Failing run 43865187352](https://github.com/osac-project/osac/actions/runs/43865187352) and
  [passing run 44193072179](https://github.com/osac-project/osac/actions/runs/44193072179),
  identical flavor digest
- [osac PR #2407, the OSAC-7419 fix](https://github.com/osac-project/osac/pull/2407)
- [osac-test-infra PR #828, the OVN forwarding mitigation](https://github.com/osac-project/osac-test-infra/pull/828)
- [osac-test-infra PR #825, the AWS-credentials fix](https://github.com/osac-project/osac-test-infra/pull/825)
- [ci-observe: Why runs fail](https://osac-ci.redhat.com:3000/d/ci-observe-failures/ci-observe-e28094-why-runs-fail)

The diagnosis was verified on 2026-11-23 by booting two clones of the exact
flavor digest on the r450 cluster-tool server, replaying the CI install
sequence, and reading the failing runs' log bundles. The r450 server was
left in its original state.
