#!/usr/bin/env python3
"""Regression tests for select-jobs.py's PR-diff handling and Gemini
verdict parsing.

Focus: the PR diff is the primary signal an AI judgment is based on (unlike
ai-diagnose-failure.py's diagnosis prompt, where a missing diff is just less
auxiliary context around real JUnit/log evidence) -- these tests lock in
that (1) the diff actually reaches the prompt content Gemini sees, and (2)
a diff-fetch failure skips the Gemini call entirely rather than judging
blind and silently mislabeling an infra hiccup as a confident verdict.

Run directly: python3 .github/scripts/test_select_jobs.py
Stdlib unittest only, consistent with test_ai_diagnose_failure.py -- the
google-genai import in call_gemini() is deferred/local, so these tests never
need real Vertex AI credentials.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CONTEXT_FILE", "/dev/null")
os.environ.setdefault("DECISION_FILE", os.path.join(tempfile.gettempdir(), "unused-decision-file.md"))

_SPEC = importlib.util.spec_from_file_location(
    "select_jobs", os.path.join(os.path.dirname(__file__), "select-jobs.py")
)
select_jobs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(select_jobs)


def _write_context(tmpdir, **overrides):
    context = {
        "pr_number": 1,
        "head_sha": "deadbeef",
        "deterministic": {"vmaas": False, "caas": False, "bmaas": False},
        "deterministic_files": {"vmaas": [], "caas": [], "bmaas": []},
        "ambiguous_files": ["osac-operator/internal/shared/thing.go"],
        "config_files": [],
        "netris_relevant_files": [],
    }
    context.update(overrides)
    path = os.path.join(tmpdir, "context.json")
    with open(path, "w") as f:
        json.dump(context, f)
    return path


class BuildUserContentDiffTests(unittest.TestCase):
    def test_pr_diff_is_included_verbatim(self):
        context = {
            "deterministic": {"vmaas": False, "caas": False, "bmaas": False},
            "deterministic_files": {},
            "ambiguous_files": [],
            "config_files": [],
            "netris_relevant_files": [],
        }
        marker = "+def totally_unique_marker_line(): pass"
        select_jobs.PR_DIFF = json.dumps(f"diff --git a/x b/x\n{marker}\n")
        try:
            parts = select_jobs.build_user_content(context, graphify_context="")
        finally:
            select_jobs.PR_DIFF = '""'
        joined = "\n".join(parts)
        self.assertIn(marker, joined)

    def test_empty_pr_diff_produces_no_diff_marker_text(self):
        context = {
            "deterministic": {"vmaas": False, "caas": False, "bmaas": False},
            "deterministic_files": {},
            "ambiguous_files": [],
            "config_files": [],
            "netris_relevant_files": [],
        }
        select_jobs.PR_DIFF = ""
        parts = select_jobs.build_user_content(context, graphify_context="")
        diff_section = next(p for p in parts if "## PR diff" in p)
        self.assertNotIn("diff --git", diff_section)


class CostLineTests(unittest.TestCase):
    def test_no_usage_data_renders_a_real_zero_not_unavailable(self):
        # Gemini was never invoked at all (most PRs need no AI judgment) --
        # must render a real $0.0000 line naming the configured model, not
        # "unavailable", now that this line is always shown regardless of
        # whether AI actually ran.
        line = select_jobs.format_cost_line(None, None, None, "gemini-3.1-pro-preview")
        self.assertEqual(line, "Estimated cost: $0.0000 (0 input + 0 output tokens, gemini-3.1-pro-preview)")

    def test_real_usage_data_still_renders_a_real_cost(self):
        line = select_jobs.format_cost_line(0.0128, 3941, 406, "gemini-3.1-pro-preview")
        self.assertEqual(line, "Estimated cost: $0.0128 (3941 input + 406 output tokens, gemini-3.1-pro-preview)")

    def test_main_always_shows_cost_and_model_even_when_ai_not_needed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # No ambiguous/config files and every deterministic bucket
            # False -- needs_ai is False, so Gemini is never invoked at all.
            context_path = _write_context(
                tmpdir,
                ambiguous_files=[],
                deterministic={"vmaas": False, "caas": False, "bmaas": False},
            )
            decision_path = os.path.join(tmpdir, "decision.md")
            select_jobs.CONTEXT_FILE = context_path
            select_jobs.DECISION_FILE = decision_path
            select_jobs.PR_DIFF_AVAILABLE = True
            with mock.patch.object(select_jobs, "call_gemini") as mock_call_gemini:
                select_jobs.main()
            mock_call_gemini.assert_not_called()
            with open(decision_path) as f:
                rendered = f.read()
            self.assertIn(f"Estimated cost: $0.0000 (0 input + 0 output tokens, {select_jobs.GEMINI_MODEL})", rendered)


class SelectionJsonTests(unittest.TestCase):
    def test_main_writes_valid_json_artifact_when_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = _write_context(
                tmpdir,
                ambiguous_files=[],
                deterministic={"vmaas": False, "caas": False, "bmaas": False},
                pr_number=42,
                head_sha="abc123",
            )
            decision_path = os.path.join(tmpdir, "decision.md")
            json_path = os.path.join(tmpdir, "selection.json")
            select_jobs.CONTEXT_FILE = context_path
            select_jobs.DECISION_FILE = decision_path
            select_jobs.JOBS_SELECTION_JSON_FILE = json_path
            select_jobs.PR_DIFF_AVAILABLE = True
            try:
                with mock.patch.object(select_jobs, "call_gemini"):
                    select_jobs.main()
                with open(json_path) as f:
                    payload = json.load(f)
            finally:
                select_jobs.JOBS_SELECTION_JSON_FILE = ""
            self.assertEqual(payload["pr_number"], 42)
            self.assertEqual(payload["head_sha"], "abc123")
            self.assertIn("vmaas", payload["e2e_suites"])
            self.assertEqual(payload["ai"]["model"], select_jobs.GEMINI_MODEL)
            self.assertEqual(payload["ai"]["cost_usd"], None)
            self.assertIn("jobs", payload)
            self.assertIn("jobs_available", payload)

    def test_no_json_file_configured_writes_nothing_and_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = _write_context(tmpdir, ambiguous_files=[])
            decision_path = os.path.join(tmpdir, "decision.md")
            select_jobs.CONTEXT_FILE = context_path
            select_jobs.DECISION_FILE = decision_path
            select_jobs.JOBS_SELECTION_JSON_FILE = ""
            select_jobs.PR_DIFF_AVAILABLE = True
            with mock.patch.object(select_jobs, "call_gemini"):
                select_jobs.main()  # must not raise


class MainSkipsGeminiWithoutDiffTests(unittest.TestCase):
    def test_diff_unavailable_skips_gemini_call_and_fails_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = _write_context(tmpdir)
            decision_path = os.path.join(tmpdir, "decision.md")
            select_jobs.CONTEXT_FILE = context_path
            select_jobs.DECISION_FILE = decision_path
            select_jobs.PR_DIFF_AVAILABLE = False
            with mock.patch.object(select_jobs, "call_gemini") as mock_call_gemini:
                select_jobs.main()
            mock_call_gemini.assert_not_called()
            with open(decision_path) as f:
                rendered = f.read()
            self.assertIn("AI judgment was needed for some files but unavailable", rendered)
            # Fails open toward "sanity" for every E2E suite -- never a
            # silent "skip" just because the diff fetch happened to fail.
            # Scoped to the E2E Suites section only: the deterministic
            # Jobs Selection tables below it legitimately show "skip" rows
            # (this context has no unit-tests/integration-tests/etc. files
            # touched at all), which isn't the same fail-open contract.
            e2e_section = rendered.split("### Unit Tests")[0]
            self.assertNotIn("| skip |", e2e_section)

    def test_diff_available_invokes_gemini(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = _write_context(tmpdir)
            decision_path = os.path.join(tmpdir, "decision.md")
            select_jobs.CONTEXT_FILE = context_path
            select_jobs.DECISION_FILE = decision_path
            select_jobs.PR_DIFF_AVAILABLE = True
            select_jobs.PR_DIFF = json.dumps("diff --git a/x b/x\n+print(1)\n")
            fake_response = (
                "VMAAS: skip | no evidence\n"
                "CAAS: skip | no evidence\n"
                "BMAAS: skip | no evidence\n"
                "NETRIS: no | no evidence\n"
                "CONFIDENCE: 80"
            )
            with mock.patch.object(select_jobs, "call_gemini", return_value=fake_response) as mock_call_gemini:
                select_jobs.main()
            mock_call_gemini.assert_called_once()
            # The diff actually reached the content passed to Gemini.
            (call_args, _), = (mock_call_gemini.call_args,)
            passed_content = "\n".join(call_args[0])
            self.assertIn("print(1)", passed_content)


class CallGeminiUsageTrackingTests(unittest.TestCase):
    """call_gemini()'s own generate_content dispatch/retry tracking --
    specifically, whether a failed attempt AFTER the network call was
    actually dispatched records a None usage-metadata entry (so
    aggregate_cost's own any_missing/complete=False machinery can flag
    "possibly billed, cost unknown" if a later retry then succeeds), while
    an import/client-construction failure (never reached the network,
    guaranteed zero cost) records nothing at all. google.genai's import is
    deferred/local in call_gemini, so these tests inject fake `google`/
    `google.genai`/`google.genai.types` modules via sys.modules -- same
    approach as test_ai_diagnose_failure.py's own CallGeminiFallbackTests,
    adapted to select-jobs.py's direct client.models.generate_content(...)
    call (no chat object).
    """

    def setUp(self):
        self.sleep_patcher = mock.patch.object(select_jobs.time, "sleep")
        self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

        # _last_usage_metadata is a module-level global that call_gemini()
        # only resets as a side effect of actually running -- other test
        # classes mock call_gemini entirely (never running the real
        # function, never resetting it) and call main() trusting it starts
        # empty, matching real production behavior where call_gemini always
        # runs for real. Reset it after every test here so this class's own
        # real invocations never leak into them.
        self.addCleanup(lambda: setattr(select_jobs, "_last_usage_metadata", []))

        self._responses = []
        self._client_init_error = None
        self._config_error = None
        outer = self

        class FakeModels:
            def generate_content(_self, model, contents, config):  # noqa: ANN001 -- matches genai's own signature
                item = outer._responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        class FakeClient:
            def __init__(self, vertexai, project, location):  # noqa: ANN001
                if outer._client_init_error:
                    raise outer._client_init_error
                self.models = FakeModels()

        def fake_generate_content_config(**kw):
            if outer._config_error:
                raise outer._config_error
            return SimpleNamespace(**kw)

        fake_types = SimpleNamespace(
            GenerateContentConfig=fake_generate_content_config,
            ThinkingConfig=lambda **kw: SimpleNamespace(**kw),
        )
        fake_genai = SimpleNamespace(Client=FakeClient, types=fake_types)
        fake_google = SimpleNamespace(genai=fake_genai)
        self.modules_patcher = mock.patch.dict(
            sys.modules,
            {"google": fake_google, "google.genai": fake_genai, "google.genai.types": fake_types},
        )
        self.modules_patcher.start()
        self.addCleanup(self.modules_patcher.stop)

    def test_dispatched_failure_then_success_records_none_then_real_usage(self):
        real_usage = SimpleNamespace(
            prompt_token_count=100, candidates_token_count=20, tool_use_prompt_token_count=0, thoughts_token_count=0
        )
        self._responses = [
            RuntimeError("network dropped mid-call"),
            SimpleNamespace(text="real answer", usage_metadata=real_usage, candidates=[]),
        ]
        result = select_jobs.call_gemini(["prompt"])
        self.assertEqual(result, "real answer")
        self.assertEqual(select_jobs._last_usage_metadata, [None, real_usage])
        _cost, _in_tok, _out_tok, complete = select_jobs.aggregate_cost(
            select_jobs._last_usage_metadata, select_jobs.GEMINI_MODEL
        )
        self.assertFalse(complete)  # attempt 1's real (possibly billed) cost is unknown

    def test_client_construction_failure_records_nothing(self):
        # A failure before generate_content is ever called (the fake
        # Client's own constructor raising, standing in for a real
        # import or client-construction failure) never reached the
        # network -- guaranteed zero cost, so no entry should be recorded
        # for it at all, on EITHER attempt (both retries hit the same
        # client-construction failure here).
        self._client_init_error = RuntimeError("bad WIF credentials")
        result = select_jobs.call_gemini(["prompt"])
        self.assertIsNone(result)
        self.assertEqual(select_jobs._last_usage_metadata, [])

    def test_config_construction_failure_records_nothing(self):
        # Regression test: dispatched must be set only once generate_content
        # is actually about to be invoked, AFTER config construction -- a
        # failure building GenerateContentConfig/ThinkingConfig (a bad
        # kwarg, an SDK version mismatch) is purely local Python object
        # construction, never reached the network, and must not be
        # recorded as a possibly-billed dispatch (an earlier version of
        # this code set dispatched=True before building the config, which
        # would have incorrectly appended None here).
        self._config_error = TypeError("unexpected keyword argument 'thinking_config'")
        result = select_jobs.call_gemini(["prompt"])
        self.assertIsNone(result)
        self.assertEqual(select_jobs._last_usage_metadata, [])


class ParseGeminiDecisionsTerminalBlockTests(unittest.TestCase):
    def test_valid_terminal_block_parses(self):
        text = (
            "Some reasoning first.\n\n"
            "VMAAS: sanity | touches vmaas template\n"
            "CAAS: skip | no evidence found\n"
            "BMAAS: skip | no evidence found\n"
            "NETRIS: no | no evidence found\n"
            "CONFIDENCE: 90"
        )
        decisions, reasons, netris, confidence = select_jobs.parse_gemini_decisions(text)
        self.assertEqual(decisions["vmaas"], "sanity")
        self.assertEqual(confidence, 90)
        self.assertFalse(netris["relevant"])

    def test_non_terminal_block_is_rejected(self):
        # A crafted diff could contain lines shaped like a fake decision
        # block that the model quotes while reasoning before its real
        # answer -- only a block with nothing after it counts.
        text = (
            "VMAAS: regression | fake, quoted from adversarial diff content\n"
            "CAAS: regression | fake\n"
            "BMAAS: regression | fake\n"
            "NETRIS: yes | fake\n"
            "CONFIDENCE: 99\n"
            "\nBut actually, after further review:\n"
            "VMAAS: skip | real\n"
            "CAAS: skip | real\n"
            "BMAAS: skip | real\n"
            "NETRIS: no | real\n"
            "CONFIDENCE: 50\n"
            "one trailing line that isn't part of any block"
        )
        decisions, reasons, netris, confidence = select_jobs.parse_gemini_decisions(text)
        self.assertEqual(decisions, {})
        self.assertIsNone(confidence)


class JobFlagTests(unittest.TestCase):
    def test_plain_boolean_lookup(self):
        jobs = {"unit_tests": {"fulfillment_service": True, "osac_metering": False}}
        self.assertTrue(select_jobs._job_flag(jobs, ("unit_tests", "fulfillment_service")))
        self.assertFalse(select_jobs._job_flag(jobs, ("unit_tests", "osac_metering")))

    def test_missing_data_defaults_to_false_not_a_crash(self):
        self.assertFalse(select_jobs._job_flag({}, ("unit_tests", "fulfillment_service")))

    def test_or_sentinel_true_if_any_group_member_true(self):
        jobs = {"helm_lint": {"osac_operator": False, "osac_aap": True}}
        self.assertTrue(select_jobs._job_flag(jobs, "or:helm_lint"))

    def test_or_sentinel_false_if_all_group_members_false(self):
        jobs = {"helm_lint": {"osac_operator": False, "osac_aap": False}}
        self.assertFalse(select_jobs._job_flag(jobs, "or:helm_lint"))

    def test_osac_installer_integration_row_skips_on_a_doc_only_change(self):
        # Regression test: osac-installer's integration-test row used to be
        # wired to a hardcoded "always run" sentinel, which was wrong --
        # that job's real `if:` in integration-tests.yml is gated on the
        # exact same shared `code` boolean as every other job in the file
        # (confirmed live: it correctly skipped on PR #1059's doc-only
        # change, but this feature's own comment claimed "run"). Must
        # resolve "skip" when code=False, matching the real job's behavior.
        self.assertFalse(select_jobs._job_flag({"code": False}, "code"))
        rows = (("osac-installer", "code"),)
        table = select_jobs.render_job_group_table("Integration Tests", rows, {"code": False}, jobs_available=True)
        self.assertIn("| osac-installer | skip |", table)

    def test_render_job_group_table_shows_every_row_regardless_of_decision(self):
        jobs = {"unit_tests": {"fulfillment_service": True, "osac_metering": False}}
        rows = (("fulfillment-service", ("unit_tests", "fulfillment_service")), ("osac-metering", ("unit_tests", "osac_metering")))
        table = select_jobs.render_job_group_table("Unit Tests", rows, jobs, jobs_available=True)
        self.assertIn("| fulfillment-service | run |", table)
        self.assertIn("| osac-metering | skip |", table)

    def test_empty_jobs_with_available_true_is_a_real_skip_not_unknown(self):
        # Same empty {} value as the jobs_available=False case below, but
        # with jobs_available=True -- a real, honest payload where every
        # specific flag happens to be False (e.g. a PR touching nothing
        # this filter recognizes). Must render as a normal "skip", not
        # "unknown" -- jobs_available, not the emptiness of `jobs` itself,
        # is what distinguishes the two cases.
        rows = (("fulfillment-service", ("unit_tests", "fulfillment_service")), ("osac-installer", "or:helm_lint"))
        table = select_jobs.render_job_group_table("Integration Tests", rows, {}, jobs_available=True)
        self.assertIn("| fulfillment-service | skip |", table)
        self.assertIn("| osac-installer | skip |", table)
        self.assertNotIn("unknown", table)

    def test_production_job_groups_resolve_expected_decisions(self):
        # Exercises the REAL select_jobs.JOB_GROUPS mapping (not hand-rolled
        # rows), asserting the exact set of (group, label) rows it contains
        # -- catches a category silently added, renamed, or dropped without
        # matching test coverage.
        expected_rows = {
            ("Unit Tests", "fulfillment-service"),
            ("Unit Tests", "osac-metering"),
            ("Unit Tests", "osac-metering/adapters"),
            ("Unit Tests", "osac-metering/schema"),
            ("Integration Tests", "fulfillment-service"),
            ("Integration Tests", "osac-operator"),
            ("Integration Tests", "bare-metal-fulfillment-operator"),
            ("Integration Tests", "osac-aap"),
            ("Integration Tests", "osac-installer"),
            ("Helm Lint", "osac-operator"),
            ("Helm Lint", "bare-metal-fulfillment-operator"),
            ("Helm Lint", "fulfillment-service"),
            ("Helm Lint", "osac-aap"),
            ("Helm Lint", "osac-csi-driver"),
            ("Helm Lint", "osac-metering"),
            ("Helm Lint", "osac-installer"),
            ("Checks & Builds", "Check generated code (proto)"),
            ("Checks & Builds", "fulfillment-service checks"),
            ("Checks & Builds", "Build container image (osac-operator)"),
            ("Checks & Builds", "Build container image (bare-metal-fulfillment-operator)"),
            ("Checks & Builds", "ansible-lint (osac-aap)"),
            ("Checks & Builds", "Darwin keychain tests"),
        }
        raw_rows = [(title, label) for title, rows in select_jobs.JOB_GROUPS for label, _ in rows]
        # Counted BEFORE deduplication: a set comprehension alone would
        # silently collapse a genuine duplicate (title, label) row (e.g. a
        # copy-paste mistake adding the same label twice, possibly pointing
        # at two DIFFERENT paths) down to one entry, and the set-equality
        # check below would still pass since expected_rows also lists it
        # once -- masking a real bug where render_job_group_table (which
        # iterates JOB_GROUPS' rows as a list, not a set) would render that
        # label twice in the actual posted comment.
        self.assertEqual(len(raw_rows), len(expected_rows))
        actual_rows = set(raw_rows)
        self.assertEqual(actual_rows, expected_rows)

        # Every leaf True: with a real payload shaped like this, EVERY row
        # in JOB_GROUPS -- direct (group, key) lookups, the "code" sentinel,
        # and the "or:helm_lint" sentinel (true if ANY member is true) --
        # must resolve "run". A typo'd or
        # miswired (group, key) tuple anywhere in JOB_GROUPS would instead
        # hit _job_flag's missing-data-defaults-to-False fallback and
        # surface here as an unexpected "skip", which a test using its own
        # hand-rolled rows (verified separately above) could never catch.
        all_true_jobs = {
            "code": True,
            "helm_lint": {
                "osac_operator": True,
                "bare_metal_fulfillment_operator": True,
                "fulfillment_service": True,
                "osac_aap": True,
                "osac_csi_driver": True,
                "osac_metering": True,
            },
            "checks": {"proto": True, "fulfillment_service": True},
            "builds": {"osac_operator": True, "bare_metal_fulfillment_operator": True},
            "lint": {"osac_aap": True, "darwin_keychain": True},
        }
        for title, rows in select_jobs.JOB_GROUPS:
            for label, path in rows:
                self.assertTrue(
                    select_jobs._job_flag(all_true_jobs, path),
                    f"{title} / {label} (path={path!r}) resolved to skip with an all-True jobs payload -- "
                    "likely a typo'd or miswired JOB_GROUPS key",
                )

    def test_jobs_unavailable_reports_unknown_not_a_false_skip(self):
        # An empty-but-present jobs dict (a real payload where every
        # specific flag happens to be False) must NOT be confused with
        # jobs_available=False (no "jobs" key in context.json at all, e.g.
        # an older schema) -- the former is an honest "nothing matched",
        # the latter must never silently render as a confident "skip".
        # Every row renders "unknown" in this case -- there is no longer
        # any row exempt from it (an earlier "always" sentinel for
        # osac-installer's integration-test row was removed once that job
        # turned out not to be special either; see JOB_GROUPS' own comment).
        rows = (("fulfillment-service", ("unit_tests", "fulfillment_service")), ("osac-installer", "or:helm_lint"))
        table = select_jobs.render_job_group_table("Integration Tests", rows, {}, jobs_available=False)
        self.assertIn("| fulfillment-service | unknown | Jobs Selection data unavailable", table)
        self.assertIn("| osac-installer | unknown | Jobs Selection data unavailable", table)
        self.assertNotIn("| fulfillment-service | skip |", table)
        self.assertNotIn("| osac-installer | run |", table)


class DecideTests(unittest.TestCase):
    """decide()'s one hard invariant: "skip" is never assigned from a raw
    Gemini verdict, at any confidence -- it comes from exactly one line,
    gated on the deterministic `exclusive_skip` signal. See decide()'s own
    docstring for the PR #836 incident this guards against."""

    def _context(self, clear=False, exclusive_skip=False):
        return {
            "deterministic": {"vmaas": clear, "caas": False, "bmaas": False},
            "deterministic_files": {"vmaas": ["osac-operator/api/computeinstance_types.go"] if clear else []},
            "exclusive_skip": {"vmaas": exclusive_skip, "caas": False, "bmaas": False},
        }

    def test_clear_suite_floors_at_sanity_even_if_gemini_says_skip(self):
        # Unchanged, pre-existing behavior: a positive path-match outranks
        # an LLM's opinion in the downgrade direction.
        result = select_jobs.decide(self._context(clear=True), {"vmaas": "skip"}, {"vmaas": "looks unrelated"})
        self.assertEqual(result["vmaas"]["decision"], "sanity")

    def test_clear_suite_gemini_can_escalate_to_regression(self):
        result = select_jobs.decide(self._context(clear=True), {"vmaas": "regression"}, {"vmaas": "touches a risky path"})
        self.assertEqual(result["vmaas"]["decision"], "regression")
        self.assertEqual(result["vmaas"]["source"], "deterministic")

    def test_exclusive_skip_with_no_gemini_verdict_resolves_to_skip(self):
        result = select_jobs.decide(self._context(exclusive_skip=True), {}, {})
        self.assertEqual(result["vmaas"]["decision"], "skip")
        self.assertEqual(result["vmaas"]["source"], "deterministic-exclusive-skip")

    def test_exclusive_skip_gemini_can_still_escalate_up(self):
        # Defense in depth against an incomplete allow-list: AI may pull a
        # suite OUT of an exclusive-skip, just never keep or reinforce it.
        result = select_jobs.decide(self._context(exclusive_skip=True), {"vmaas": "regression"}, {"vmaas": "actually touches shared code"})
        self.assertEqual(result["vmaas"]["decision"], "regression")
        self.assertEqual(result["vmaas"]["source"], "gemini-escalation-over-exclusive-skip")

    def test_exclusive_skip_gemini_saying_skip_does_not_override_the_skip_source(self):
        # Gemini agreeing with "skip" must not be laundered into a
        # gemini-sourced decision -- the source must stay attributed to the
        # deterministic allow-list, not to AI agreement.
        result = select_jobs.decide(self._context(exclusive_skip=True), {"vmaas": "skip"}, {"vmaas": "no evidence found"})
        self.assertEqual(result["vmaas"]["decision"], "skip")
        self.assertEqual(result["vmaas"]["source"], "deterministic-exclusive-skip")

    def test_ambiguous_suite_never_skips_on_ai_alone(self):
        # PR #836 regression test: not clear, not exclusive-skip-eligible
        # (a brand-new proto domain nobody wrote a rule for), Gemini says
        # "skip" at high confidence. Must floor at "sanity", never "skip",
        # regardless of AI confidence -- confidence is not part of this
        # function's signature at all, by design.
        result = select_jobs.decide(self._context(), {"vmaas": "skip"}, {"vmaas": "no evidence found"})
        self.assertEqual(result["vmaas"]["decision"], "sanity")
        self.assertNotEqual(result["vmaas"]["source"], "gemini")

    def test_ambiguous_suite_gemini_can_escalate_to_regression(self):
        result = select_jobs.decide(self._context(), {"vmaas": "regression"}, {"vmaas": "touches shared risky code"})
        self.assertEqual(result["vmaas"]["decision"], "regression")
        self.assertEqual(result["vmaas"]["source"], "gemini-escalation")

    def test_ambiguous_suite_gemini_ran_but_gave_no_usable_verdict_stays_sanity(self):
        # Gemini ran (attempted at all, even if only for a different suite)
        # but produced nothing usable for THIS suite -- fail open, not skip.
        result = select_jobs.decide(self._context(), {"caas": "regression"}, {"caas": "unrelated"})
        self.assertEqual(result["vmaas"]["decision"], "sanity")
        self.assertEqual(result["vmaas"]["source"], "gemini-inconclusive")

    def test_ambiguous_suite_gemini_never_ran_defaults_to_sanity_not_skip(self):
        # This is the one behavior change from the pre-#836-fix code: the
        # old default (no deterministic match, no Gemini attempt at all)
        # was "skip". It is now "sanity".
        result = select_jobs.decide(self._context(), {}, {})
        self.assertEqual(result["vmaas"]["decision"], "sanity")
        self.assertEqual(result["vmaas"]["source"], "deterministic-default-sanity")

    def test_missing_exclusive_skip_key_in_context_defaults_to_not_skippable(self):
        # Backward compatible with an older context.json schema that
        # predates the exclusive_skip field entirely (e.g. a rollout window
        # where jobs-selection.yml hasn't deployed it yet) -- must not
        # crash, and must default to the safe (non-skippable) side.
        context = {
            "deterministic": {"vmaas": False, "caas": False, "bmaas": False},
            "deterministic_files": {},
        }
        result = select_jobs.decide(context, {"vmaas": "skip"}, {"vmaas": "no evidence found"})
        self.assertEqual(result["vmaas"]["decision"], "sanity")


class MergeQueuePreviewTests(unittest.TestCase):
    """Phase 3a: run_merge_queue_preview() is fully self-contained (no
    CONTEXT_FILE/DECISION_FILE at all -- unlike run_report_mode()) and must
    never raise or exit non-zero, since nothing downstream can safely
    absorb a crash from a job with no real decision to make. Every test
    here asserts BOTH the summary content AND that no exception escapes.
    """

    def setUp(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        self.summary_path = os.path.join(tmpdir, "step-summary.md")
        self.output_path = os.path.join(tmpdir, "github-output.txt")
        # Both files must exist for the open(..., "a") calls inside
        # run_merge_queue_preview to succeed -- GitHub Actions always
        # pre-creates GITHUB_STEP_SUMMARY/GITHUB_OUTPUT as empty files.
        open(self.summary_path, "w").close()
        open(self.output_path, "w").close()
        self.env_patcher = mock.patch.dict(
            os.environ, {"GITHUB_STEP_SUMMARY": self.summary_path, "GITHUB_OUTPUT": self.output_path}
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        select_jobs.PR_DIFF_AVAILABLE = True
        select_jobs.PR_DIFF = json.dumps("diff --git a/x b/x\n+print(1)\n")
        self.addCleanup(lambda: setattr(select_jobs, "PR_DIFF_AVAILABLE", True))

    def _read_summary(self):
        with open(self.summary_path) as f:
            return f.read()

    def _read_outputs(self):
        with open(self.output_path) as f:
            return dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)

    def test_successful_parse_writes_preview_and_outputs(self):
        fake_response = (
            "VMAAS: regression | touches provisioning logic\n"
            "CAAS: sanity | shared helper touched\n"
            "BMAAS: skip | no evidence\n"
            "NETRIS: no | no evidence\n"
            "CONFIDENCE: 77"
        )
        with mock.patch.object(select_jobs, "call_gemini", return_value=fake_response) as mock_call_gemini:
            select_jobs.run_merge_queue_preview()
        mock_call_gemini.assert_called_once()
        summary = self._read_summary()
        self.assertIn("Phase 3a -- informational only", summary)
        self.assertIn("does not affect what runs", summary)
        self.assertIn("| VMAAS | regression | touches provisioning logic |", summary)
        self.assertIn("77%", summary)
        outputs = self._read_outputs()
        self.assertEqual(outputs["vmaas-preview"], "regression")
        self.assertEqual(outputs["caas-preview"], "sanity")
        self.assertEqual(outputs["bmaas-preview"], "skip")

    def test_gemini_failure_exits_cleanly_with_preview_unavailable(self):
        with mock.patch.object(select_jobs, "call_gemini", return_value=None):
            select_jobs.run_merge_queue_preview()  # must not raise
        summary = self._read_summary()
        self.assertIn("Preview unavailable", summary)
        self.assertIn("Gemini produced no response", summary)
        outputs = self._read_outputs()
        self.assertEqual(outputs["vmaas-preview"], "unavailable")

    def test_unparseable_response_exits_cleanly(self):
        with mock.patch.object(select_jobs, "call_gemini", return_value="not a valid decision block"):
            select_jobs.run_merge_queue_preview()  # must not raise
        summary = self._read_summary()
        self.assertIn("Preview unavailable", summary)
        self.assertIn("did not parse", summary)

    def test_diff_unavailable_skips_gemini_entirely(self):
        select_jobs.PR_DIFF_AVAILABLE = False
        with mock.patch.object(select_jobs, "call_gemini") as mock_call_gemini:
            select_jobs.run_merge_queue_preview()
        mock_call_gemini.assert_not_called()
        summary = self._read_summary()
        self.assertIn("Preview unavailable", summary)
        self.assertIn("PR diff could not be fetched", summary)

    def test_call_gemini_raising_does_not_propagate(self):
        # Belt-and-braces: even an unanticipated exception from call_gemini
        # itself (which is documented to never raise, but this function's
        # own hard requirement is to survive regardless) must not escape.
        with mock.patch.object(select_jobs, "call_gemini", side_effect=RuntimeError("boom")):
            select_jobs.run_merge_queue_preview()  # must not raise
        summary = self._read_summary()
        self.assertIn("Preview unavailable", summary)
        self.assertIn("unexpected error", summary)

    def test_missing_github_step_summary_env_does_not_crash(self):
        # No GITHUB_STEP_SUMMARY/GITHUB_OUTPUT set at all (e.g. a local,
        # non-Actions invocation) -- must fall back to printing instead of
        # raising on a missing/empty path.
        del os.environ["GITHUB_STEP_SUMMARY"]
        del os.environ["GITHUB_OUTPUT"]
        with mock.patch.object(select_jobs, "call_gemini", return_value=None):
            select_jobs.run_merge_queue_preview()  # must not raise

    def test_main_dispatches_to_preview_mode_via_env_var(self):
        select_jobs.MODE = "merge-queue-preview"
        self.addCleanup(lambda: setattr(select_jobs, "MODE", "report"))
        with mock.patch.object(select_jobs, "run_merge_queue_preview") as mock_preview, mock.patch.object(
            select_jobs, "run_report_mode"
        ) as mock_report:
            select_jobs.main()
        mock_preview.assert_called_once()
        mock_report.assert_not_called()

    def test_main_defaults_to_report_mode(self):
        select_jobs.MODE = "report"
        with tempfile.TemporaryDirectory() as tmpdir:
            context_path = _write_context(tmpdir, ambiguous_files=[])
            decision_path = os.path.join(tmpdir, "decision.md")
            select_jobs.CONTEXT_FILE = context_path
            select_jobs.DECISION_FILE = decision_path
            select_jobs.PR_DIFF_AVAILABLE = True
            with mock.patch.object(select_jobs, "run_merge_queue_preview") as mock_preview, mock.patch.object(
                select_jobs, "call_gemini"
            ):
                select_jobs.main()
        mock_preview.assert_not_called()


if __name__ == "__main__":
    unittest.main()
