# An early gate job blocked the real E2E test

**Symptom:** No `junit.xml`, no `osac-logs-*` cluster evidence at all --
the extract says "no artifact directory found" -- but the triggering
workflow's overall conclusion is still `failure`.

**Root cause:** A cheap pre-test gate job in the caller workflow (most
often `e2e-readiness`'s lgtm / `/e2e-ready` / CodeRabbit-approval check)
failed, so the actual `e2e-{suite}-full-install` job never ran. No cluster
was ever booted; there is no test evidence to diagnose because no test
ran.

**What to say if you see this:** Don't invent a component-level root
cause from missing evidence. State plainly that no E2E test ran because
an earlier gate job blocked it, name the likely gate, and note the fix is
unblocking the gate (lgtm / `/e2e-ready` / CodeRabbit approval on the PR),
not a code change to any OSAC component.

**Status:** As of OSAC-4741, `ai-diagnostic-e2e.yml`'s own `resolve` job
already detects this specific case (checks whether the triggering run's
`e2e-*-full-install` job was `skipped`) and skips the AI diagnosis
entirely before Gemini is ever invoked -- confirmed live against PR #711
(https://github.com/osac-project/osac/actions/runs/33626328985), where
this exact symptom previously produced a wasted Gemini call and a "no
artifacts found, go check the logs" non-answer. This entry is kept as a
fallback for the rare case where a *different*, not-yet-special-cased
early gate produces the same symptom.
