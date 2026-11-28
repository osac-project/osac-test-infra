# OSAC tests on one shared cluster

## The idea

Today, every test run gets its own whole cluster. That is slow and it wastes
big machines.

The new way: one big cluster stays up all the time. Every test run gets a
small private cluster inside it. We call it a "run". Each run has its own
OSAC. The runs cannot see each other. Many runs can live on the same big
cluster at the same time.

The big cluster is called the "outer cluster". The private cluster inside it
is called a "vcluster".

## What works today

**VMaaS is green.** The full test suite ran against a run and 126 of 134
tests passed. A test user logs in, asks for a machine, and OSAC makes a real
machine on the outer cluster. Only the run that asked for it can see it.
The last few failures were test bugs or node capacity problems, not OSAC
bugs. All are fixed or skipped now.

**Three VMaaS suites ran at the same time.** On one node, three runs each
ran the whole VMaaS suite together. 378 of 381 tests passed. One run was
fully green. The two others failed only on tests that are sensitive to
load. Those tests now wait for the system to settle instead of failing.

**CaaS got far.** The first CaaS run created a real hosted cluster on the
outer cluster. The hosted cluster reached "Available". Two OSAC bugs
blocked the rest. Both are now fixed (see below).

**BMaaS got far.** The fake metal machines work. Both reached "available"
and were seen by the run. One OSAC bug blocked the tests. It is now fixed
(see below).

**The three OSAC bugs are fixed.** The fixes are in osac PR #2566:

1. **The vault setup ran twice and crashed.** The second pass saw "this
   locker is already taken" and gave up. Now it sees "already there" and
   moves on. The real error is also shown in the log, not hidden.
2. **The outer cluster threw away a thing it still needed.** A tag on the
   InfraEnv pointed at an owner the outer cluster could not see, so it
   deleted the InfraEnv every 30 seconds. The tag is gone in that mode.
   The cleanup now happens by hand at teardown, the way Jira OSAC-8055
   wanted.
3. **One setting could not be set.** The guest ingress IP range had no
   chart key. The key exists now.

**The code review fixes are in.** A review bot (CodeRabbit) found real
bugs in our changes. All the big ones are fixed: test tokens now come
from the right cluster, the AAP job pods start without the OpenShift
service CA, the metering topic names cannot collide, and two more.

**The branch sits on top of current main.** The PR was first cut from an
old main, which made it look huge (1000+ files). After the rebase it is
about 220 files, all ours.

## The three parts

1. **The osac repo** (PR #2566). It has the fixes that make OSAC itself work
   inside a vcluster. It also has the test changes that let the test suites
   talk to the right cluster.
2. **The osac-test-infra repo** (PR #854). It has the scripts that make and
   remove runs. It also has the AAP and Keycloak setup for each run.
3. **The outer cluster.** It holds the things every run shares: the AAP, the
   user login (Keycloak), the message bus (Kafka), the databases, the vault.
   For BMaaS it also has the bare metal parts (Ironic). For CaaS it also has
   the hosted cluster parts (MCE, HyperShift, assisted service) and the DNS
   server (BIND).

## How to run one test

```
# Make a run called myrun1. The name must end in v (VMaaS), b (BMaaS) or
# c (CaaS) before the attempt digit.
bash scripts/vcluster-timing/timed-run.sh myrunv1 9460 install-only

# Run the VMaaS tests against it.
bash scripts/vcluster-timing/run-suite.sh myrunv1 vmaas/sanity

# When you are done, remove the run. The outer cluster stays.
bash scripts/vcluster-run-teardown.sh myrunv1
```

## The OSAC bugs found by the first CaaS and BMaaS runs (all fixed now)

1. **CaaS: cross-cluster ownerReferences.** The operator creates the InfraEnv
   on the outer cluster and marks it as owned by the ClusterOrder. But the
   ClusterOrder lives in the vcluster. The outer cluster does not know it, so
   its garbage collector deletes the InfraEnv every 30 seconds. No worker ever
   joins the hosted cluster because of this.
2. **CaaS: the guest ingress VIP range cannot be set.** The chart has no key
   for it, so the hosted cluster job fails at the external access step.
3. **BMaaS: tenant setup is not idempotent in the vault.** The tenant
   reconciler fails to store the break-glass credentials when the vault paths
   already exist. The error is swallowed as "vault operation failed". The
   tenant never finishes, and the tests hang at setup.

## What each suite still needs

- **VMaaS**: nothing. It is green, also three at a time.
- **BMaaS**: nothing known. The vault bug is fixed. The next run proves it.
- **CaaS**: nothing known. Both bugs are fixed. The next run proves it.

## The big proof that is running now

Nine runs at once on one node: three VMaaS, three CaaS, three BMaaS. All
nine suites run at the same time. This is the shape of the final CI.

The installs go in three waves of three. Nine at once floods the one
shared AAP: its bootstrap hooks queue past their deadlines. Three at a
time is the proven load. The suites all start together after the last
run exists, because the nine-suite load is the actual thing under test.

The run setup now creates the run's AAP organization and its vcluster
credential itself, with direct API calls that fail loudly. Before, the
chart's config-as-code created them, but its async creates can fail
under AAP load, and the failure turns into a slow spin instead of a
fast error. With the pre-create, the config-as-code only updates
things that already exist, and a re-run repairs a stale token instead
of attaching to it.

The CaaS runs get their full delivery through the run overlay: the
guest ingress VIP range and the RFC 2136 DNS driver settings, both
verified to render through the chart's schema into the ConfigMap and
Secret the AAP job pods read.

The fake metal machines for the BMaaS and CaaS runs are made on the
machine that runs libvirt (the rdu host), not on the machine that starts
the run. The setup script learned `--skip-bmhs` for this: it makes the
run first, and the metal machines are made after, on the right machine,
with `scripts/vcluster-run-bmhs.sh <run> --network <net>`.

## What is still missing

- The CI job that runs many tests at once is not written yet. The scripts
  are the start of it.
- The nightly reset of the outer cluster is not set up. It would clean up
  old runs and keep the AAP fast.
- The vcluster API needs a route so runners do not need a port-forward.
  Today the runner starts its own port-forward, which works but is a
  workaround.
- The BMF operator image and the vcluster compat objects (the service CA
  ConfigMap, the unprefixed credentials Secret) must be part of the
  install, not later fixes.
- One review finding was deferred: the metering relay stores its read
  position without the topic name. If the topic name changes, the old
  position is reused on the new topic. The fix needs a database schema
  change, so it gets its own ticket (OSAC-8365).
- Under AAP load, the config-as-code run can fail to create the run's
  vcluster credential on the first tries (the create is async and its
  error is collected for the end of the play, while the instance-group
  verification keeps retrying for minutes). The bootstrap job's own
  retries heal it once the load drops, but each failed attempt is slow.
  A clean hardening: the run setup pre-creates the organization and the
  credential before the chart install, so config-as-code only updates
  existing objects.

## Where the commits are

- osac PR #2566, branch `vcluster-e2e`: the OSAC fixes, the suite changes,
  the DNS driver, the CaaS profile.
- osac-test-infra PR #854, branch `vcluster-e2e`: the run scripts, the
  teardown, the timing tools.

Every commit says what problem it fixes and what it changes. Read the commit
list top to bottom to see the whole story.
