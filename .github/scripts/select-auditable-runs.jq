# Keep completed runs that may have logs/artifacts.
# GitHub's status=completed also returns action_required (fork/env approval
# wait, 0 jobs, logs API 404) and skipped (never started). Those are N/A, not
# incomplete scans. Cancelled runs pass through: some have jobs+logs.
#
# $repo is the owner/name string for this page's target.
[.workflow_runs[]?
 | select(.conclusion != "action_required" and .conclusion != "skipped")
 | {run_id: (.id | tostring), repo: $repo, event}]
