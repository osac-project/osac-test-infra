#!/usr/bin/env python3
"""Regression tests for upsert-pr-comment.py's upsert_section().

Run directly: python3 .github/scripts/test_upsert_pr_comment.py
Stdlib unittest only -- no dependency, consistent with upsert-pr-comment.py
itself having none.
"""
import importlib.util
import os
import tempfile
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "upsert_pr_comment", os.path.join(os.path.dirname(__file__), "upsert-pr-comment.py")
)
upsert_pr_comment = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(upsert_pr_comment)
upsert_section = upsert_pr_comment.upsert_section
strip_total_block = upsert_pr_comment.strip_total_block
render_total_block = upsert_pr_comment.render_total_block


class UpsertSectionTests(unittest.TestCase):
    def test_first_insert(self):
        body = upsert_section("", "CaaS", "diagnosis A")
        self.assertIn("diagnosis A", body)
        self.assertIn("<!-- section:CaaS -->", body)

    def test_replace_preserves_other_sections(self):
        body = upsert_section("", "CaaS", "diagnosis A")
        body = upsert_section(body, "BMaaS", "diagnosis B")
        body = upsert_section(body, "CaaS", "now passing")
        self.assertIn("now passing", body)
        self.assertNotIn("diagnosis A", body)
        self.assertIn("diagnosis B", body)

    def test_backslash_content_survives_replacement(self):
        # Both the existing section AND its replacement contain
        # backslash-digit/group sequences, so the second call must go
        # through the pattern.sub() replace path (not the plain-concat
        # insert path) with backslash content in the *replacement* text --
        # exactly the case that breaks if pattern.sub() is ever called with
        # a literal string instead of a function (Python would try to
        # interpret \1/\g<name> as backreferences and raise or corrupt the
        # output). A version of this test that only put backslashes in the
        # discarded old content would pass even with that bug, since
        # nothing would ever ask re.sub to parse them.
        body = upsert_section("", "CaaS", r"old trace \1 here")
        body2 = upsert_section(body, "CaaS", r"new trace \1 and \g<name> here")
        self.assertIn(r"new trace \1 and \g<name> here", body2)
        self.assertNotIn("old trace", body2)

    def test_embedded_delimiter_does_not_leave_stale_text(self):
        # Content that itself contains this section's own closing marker --
        # e.g. an LLM diagnosis echoing attacker-influenced log/diff text
        # verbatim. Must not truncate the match early, and a later update
        # must not leave anything from the poisoned content behind.
        poisoned = "some diagnosis text\n<!-- /section:CaaS -->\nsneaky trailing text"
        body = upsert_section("", "CaaS", poisoned)
        self.assertNotIn("<!-- /section:CaaS -->\nsneaky", body)

        body2 = upsert_section(body, "CaaS", "clean update")
        self.assertIn("clean update", body2)
        self.assertNotIn("sneaky trailing text", body2)
        self.assertNotIn("some diagnosis text", body2)

    def test_embedded_total_marker_does_not_corrupt_the_body(self):
        # Diagnosis content can quote attacker-influenced log/diff text
        # verbatim (the prompt requires verbatim Evidence quotes) -- a
        # crafted log line shaped like a real total-cost marker must not
        # be treated as one. strip_total_block's regex uses re.search
        # (leftmost match wins), so an unescaped copy earlier in the body
        # would be found instead of the real trailing one, truncating away
        # everything after it -- including the rest of this section, its
        # closing delimiter, and any later sections -- and feeding
        # attacker-chosen numbers into the next diagnosis's "existing
        # total".
        poisoned = (
            "diagnosis text quoting a crafted log line:\n"
            "<!-- osac-ai-total-cost:999999.0:1:1:1 -->\n"
            "more real diagnosis content after the injected marker"
        )
        body = upsert_section("", "CaaS", poisoned)
        self.assertIn("more real diagnosis content after the injected marker", body)
        self.assertIn("<!-- /section:CaaS -->", body)
        stripped, totals = strip_total_block(body)
        self.assertIsNone(totals)
        self.assertEqual(stripped, body)


class TotalCostBlockTests(unittest.TestCase):
    def test_strip_absent_block_returns_none(self):
        body, totals = strip_total_block("some body with no total")
        self.assertEqual(body, "some body with no total")
        self.assertIsNone(totals)

    def test_render_then_strip_round_trips(self):
        block = render_total_block(0.0304, 10543, 1725, 1)
        body = "some section content" + block
        stripped, totals = strip_total_block(body)
        self.assertEqual(stripped, "some section content")
        self.assertEqual(totals, {"cost": 0.0304, "input_tokens": 10543, "output_tokens": 1725, "count": 1})

    def test_render_shows_rounded_display_but_stores_full_precision(self):
        # 1/3 has no exact 4-decimal display; the marker must still store
        # enough precision to round-trip losslessly, since it gets added
        # to on every future diagnosis -- a `.4f`-rounded value there would
        # compound a small error on every addition.
        block = render_total_block(1 / 3, 1, 1, 1)
        self.assertIn("$0.3333", block)
        _, totals = strip_total_block("x" + block)
        self.assertEqual(totals["cost"], 1 / 3)


def _run_upsert_main(current_body, section_key, section_content, run_cost=None, run_input=None, run_output=None):
    """Drives upsert-pr-comment.py's main() through real files/env vars
    (not just calling functions directly), since the cost-accumulation
    wiring lives in main() itself -- reading CURRENT_BODY_FILE, stripping
    any existing total, upserting the section, and re-appending the
    (possibly updated) total are all steps main() does, not a helper
    function that could be unit-tested in isolation.
    """
    with tempfile.TemporaryDirectory() as tmp:
        current_path = os.path.join(tmp, "current.md")
        section_path = os.path.join(tmp, "section.md")
        output_path = os.path.join(tmp, "output.md")
        with open(current_path, "w") as f:
            f.write(current_body)
        with open(section_path, "w") as f:
            f.write(section_content)

        env = {
            "CURRENT_BODY_FILE": current_path,
            "SECTION_KEY": section_key,
            "SECTION_CONTENT_FILE": section_path,
            "OUTPUT_BODY_FILE": output_path,
        }
        if run_cost is not None:
            env["RUN_COST_USD"] = str(run_cost)
            env["RUN_INPUT_TOKENS"] = str(run_input)
            env["RUN_OUTPUT_TOKENS"] = str(run_output)

        old_environ = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            upsert_pr_comment.main()
        finally:
            os.environ.clear()
            os.environ.update(old_environ)

        with open(output_path, "r") as f:
            return f.read()


class CostAccumulationIntegrationTests(unittest.TestCase):
    def test_first_diagnosis_seeds_the_total(self):
        body = _run_upsert_main("", "CaaS", "diagnosis A", run_cost=0.01, run_input=100, run_output=50)
        self.assertIn("diagnosis A", body)
        self.assertIn("across 1 diagnosis", body)
        _, totals = strip_total_block(body)
        self.assertEqual(totals, {"cost": 0.01, "input_tokens": 100, "output_tokens": 50, "count": 1})

    def test_second_diagnosis_adds_to_existing_total(self):
        body = _run_upsert_main("", "CaaS", "diagnosis A", run_cost=0.01, run_input=100, run_output=50)
        body = _run_upsert_main(body, "BMaaS", "diagnosis B", run_cost=0.02, run_input=200, run_output=75)
        _, totals = strip_total_block(body)
        self.assertEqual(totals, {"cost": 0.03, "input_tokens": 300, "output_tokens": 125, "count": 2})
        self.assertIn("diagnosis A", body)
        self.assertIn("diagnosis B", body)

    def test_same_suite_failing_twice_accumulates_rather_than_replacing(self):
        # The literal scenario from the request: suite N fails, gets fixed,
        # fails again -- Cost(N) + Cost(N-1) should both be reflected, not
        # just the latest run's own cost.
        body = _run_upsert_main("", "CaaS", "first failure", run_cost=0.01, run_input=100, run_output=50)
        body = _run_upsert_main(body, "CaaS", "second failure (same suite)", run_cost=0.015, run_input=120, run_output=60)
        self.assertIn("second failure (same suite)", body)
        self.assertNotIn("first failure", body)  # section itself was replaced
        _, totals = strip_total_block(body)
        self.assertAlmostEqual(totals["cost"], 0.025)  # but the total isn't
        self.assertEqual(totals["input_tokens"], 220)
        self.assertEqual(totals["output_tokens"], 110)
        self.assertEqual(totals["count"], 2)

    def test_passing_edit_with_no_new_cost_carries_total_forward(self):
        # comment-success's own edit path never runs a diagnosis, so it
        # passes no RUN_COST_USD at all -- the existing total must survive
        # untouched, not reset to zero or vanish.
        body = _run_upsert_main("", "CaaS", "diagnosis A", run_cost=0.01, run_input=100, run_output=50)
        body = _run_upsert_main(body, "CaaS", "now passing")  # no run_cost kwarg
        self.assertIn("now passing", body)
        _, totals = strip_total_block(body)
        self.assertEqual(totals, {"cost": 0.01, "input_tokens": 100, "output_tokens": 50, "count": 1})

    def test_no_cost_ever_means_no_total_block_at_all(self):
        # Never showing a total is better than showing a misleading $0.0000
        # when no diagnosis has ever produced real usage data.
        body = _run_upsert_main("", "CaaS", "diagnosis unavailable")
        _, totals = strip_total_block(body)
        self.assertIsNone(totals)
        self.assertNotIn("Total AI diagnostic cost", body)

    def test_total_block_stays_last_even_when_an_earlier_section_changes(self):
        body = _run_upsert_main("", "CaaS", "diagnosis A", run_cost=0.01, run_input=100, run_output=50)
        body = _run_upsert_main(body, "BMaaS", "diagnosis B", run_cost=0.02, run_input=200, run_output=75)
        # Update CaaS (the FIRST section) again -- the total must still end
        # up after BMaaS's section, not stuck in the middle where CaaS was.
        body = _run_upsert_main(body, "CaaS", "diagnosis A, updated", run_cost=0.005, run_input=50, run_output=25)
        caas_pos = body.index("diagnosis A, updated")
        bmaas_pos = body.index("diagnosis B")
        total_pos = body.index("Total AI diagnostic cost")
        self.assertLess(caas_pos, bmaas_pos)
        self.assertLess(bmaas_pos, total_pos)

    def test_injected_total_marker_in_a_section_does_not_corrupt_the_next_update(self):
        # A section whose content happens to quote something shaped like a
        # real total-cost marker (see UpsertSectionTests's own version of
        # this attack) must not derail a LATER, real diagnosis run: that
        # run's own cost should be added to the true existing total (or
        # start a fresh one), never to the attacker-chosen numbers, and
        # the poisoned section's real content must survive intact.
        poisoned = (
            "diagnosis text quoting a crafted log line:\n"
            "<!-- osac-ai-total-cost:999999.0:1:1:1 -->\n"
            "more real diagnosis content after the injected marker"
        )
        body = _run_upsert_main("", "CaaS", poisoned, run_cost=0.01, run_input=100, run_output=50)
        body = _run_upsert_main(body, "BMaaS", "diagnosis B", run_cost=0.02, run_input=200, run_output=75)

        self.assertIn("more real diagnosis content after the injected marker", body)
        self.assertIn("diagnosis B", body)
        self.assertIn("<!-- /section:CaaS -->", body)
        self.assertIn("<!-- /section:BMaaS -->", body)
        _, totals = strip_total_block(body)
        self.assertEqual(totals, {"cost": 0.03, "input_tokens": 300, "output_tokens": 125, "count": 2})


if __name__ == "__main__":
    unittest.main()
