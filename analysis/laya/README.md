# OSAC-5641 Laya routing evaluation

This is a retrospective replay of failed OSAC E2E GitHub Actions jobs. The
shadow helper is also wired into the diagnostic workflow; it never skips
Vertex. The inputs in `cases.json` are fixed, bounded descriptions of
recognized failure signals produced by that same helper from failed job logs,
JUnit reports, and diagnostic artifacts. Raw matching lines, CI logs, and
artifact archives are not stored in this repository.

## Corpus and labels

- 59 distinct failed runs from 59 PRs, selected from September 2026 full-install
  workflows: the original 25-run exploratory set plus 34 additional PRs chosen
  to add build, install, test, and authorization failures.
- 37 development cases and 22 held-out cases. All runs with the same observed
  `root_group` are assigned to one split. The new AAP policy, fork approval,
  metering absence, and repeated TypeScript cases were assigned to development
  because they share a root with an original case.
- Labels describe the **directly observed failure route**. `root_group` keeps
  repeated signatures together. A timeout with no supported upstream cause is
  labeled `unknown`. Several E2E timeouts still need product investigation to
  determine the deeper cause. These labels were reviewed for this experiment;
  they are not the independent human approval required for a production bypass.
- 54 runs have diagnostic artifacts. Every run has a failed-job log. The shared
  extractor found direct supporting evidence in 58 runs; the remaining generic
  install timeout has no verified upstream cause. Artifact absence and
  unresolved root causes are distinct measures.

Selection was deliberately diverse, not random. Accuracy on these 59 jobs
cannot estimate production prevalence or future error rates. The holdout
contains no AAP policy case because all 20 observed AAP rejections share one
root signature in development.

The [human review sheet](review-sheet.md) links every source run by root group
and leaves all review decisions pending.

## Repeat the Laya calls

Given local raw downloads with `<PR>/job.log` or
`new_candidates/<run_id>/job.log` plus optional `artifact.zip`, recreate the
canonical states with the production extractor:

```bash
python3 analysis/laya/reextract.py --raw-root /path/to/downloads --output /tmp/cases.json
```

Use `--cases /tmp/cases.json` on the evaluator to score that replay. The
committed `cases.json` is the output of that step for this experiment. The
extractor is `.github/scripts/laya-shadow.py`; each case records its extractor
version. The exact question and model are pinned in `schema.json`. Schema v2
includes source checkout failures in the install question; the evaluator
checks that every reviewed label is available in its selected stage before
calling Laya.

The evaluator requests the `english` model and batches of at most five.
It sends the exact four-field state in `cases.json`; `short` drops pod/JUnit
evidence, `compact` removes source prefixes and clips evidence, and `stage`
uses the failed step to select a smaller question. The `stage` arm routes E2E
and CI authorization directly from the failed step; it calls Laya only for
build and install subtypes. This routing improvement must not be interpreted as
Laya discovering the E2E or CI cause.

```bash
python3 analysis/laya/evaluate.py --split development --arm direct --output /tmp/laya-dev.json
python3 analysis/laya/evaluate.py --split holdout --arm stage --output /tmp/laya-holdout.json
```

The script prints a confusion matrix, per-class false positives/negatives and
abstentions, wrong-route examples, and batch latency. `--endpoint` can point
to another compatible Laya server. The requests contain canonical diagnostic
signals, so use only an approved server. Re-running may change predictions if
the server checkpoint changes; the committed report records the observed run.

## Workflow deployment

The diagnostic job runs on GitHub-hosted `ubuntu-latest`. The Laya endpoint
used for the local replay is on a private network, and reachability from that
runner has not been demonstrated. The workflow therefore defaults shadow
capture to disabled. Set both `OSAC_LAYA_SHADOW_ENABLED=true` and
`OSAC_LAYA_ENDPOINT` to an approved endpoint reachable from the diagnostic
runner only after a connectivity check from that runner succeeds. A disabled
observation records `feature_disabled` or `endpoint_not_configured`; it is not
a Laya classification or a live evaluation sample. A failed Laya request still
abstains and leaves Vertex unchanged.

## Gate under evaluation

No score-only bypass is allowed. A candidate templated response requires an
approved signature, direct current-run evidence from a named source, no later
successful retry that resolves it, agreement between the signature route and
Laya, and a top probability of at least 0.90. Missing evidence, conflicting
sources, a generic timeout, or an unknown route means abstain and call Vertex.
Any wrong-class bypass in a root-separated audit immediately disables bypass.

The project has not provided independent human approval for a known signature,
and the workflow change has not yet produced live capture after merge. The
bypass remains **off** with a projected Vertex-call reduction of **zero**.
`report.md` records candidate opportunities and the remaining Jira acceptance
criteria.
