"""Ginkgo JSON report parsing and conversion to frozen v1 journal records.

One report file is one suite from one invocation. Retries are not
expanded: Ginkgo reports one SpecReport per spec with NumAttempts, and
per-attempt outcomes or intervals are not fabricated from NumAttempts,
Retry events, or AdditionalFailures (an AdditionalFailure can be cleanup
of the same attempt). Non-It nodes (BeforeSuite, DeferCleanup, BeforeAll)
are hooks, not tests; their failures mark the invocation failed and are
kept as explicit gaps rather than turned into test results.

Stable identity is hierarchy text, leaf text, node type, and the
suite-relative source file. Absolute checkout roots and line numbers are
deliberately excluded: the same suite at two physical roots, or with
comments inserted above an It, keeps its identity (Q7). Two specs that
share every one of those fields are still distinct specs: they are
disambiguated by an occurrence ordinal and reported through a fixed
duplicate-identity gap, never silently merged. The comparison limit is
that the ordinal reflects report order, not a stable property of the
suite.

Pending and filtered specs are both never-attempted skips: filtered is
state "skipped" with zero attempts and an absent end time, pending is
state "pending" (a selection mismatch if the declared deselection counts
disagree). Both map to outcome "skipped" with NumAttempts retained.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .jsoninput import load_json_bytes
from .records import (
    AdapterError,
    ConversionResult,
    InvocationContext,
    JSONValue,
    RecordWriter,
    check_exit_contradiction,
    collection_state_data,
    failure_signature,
    hashed_test_id,
    to_utc_z,
)

MAX_SPECS = 100_000
MAX_SPECIAL_REASONS = 100

# Native SpecState -> contract native_result_outcome.
STATE_OUTCOMES = {
    "passed": "passed",
    "failed": "failed",
    "panicked": "error",
    "timedout": "timeout",
    "aborted": "cancelled",
    "interrupted": "interrupted",
    "skipped": "skipped",
    "pending": "skipped",
}
OUTCOMES_WITH_FAILURE = {"failed", "error", "timeout"}
HOOK_STATES_PASSED = {"passed"}

# Ginkgo emits these failure node types; anything else is a shape we do
# not know and refuse rather than guess.
KNOWN_FAILURE_NODE_TYPES = {
    "",
    "It",
    "BeforeSuite",
    "AfterSuite",
    "BeforeAll",
    "AfterAll",
    "BeforeEach",
    "AfterEach",
    "JustBeforeEach",
    "JustAfterEach",
    "DeferCleanup",
    "Container",
}


@dataclass(frozen=True, kw_only=True)
class GinkgoSpec:
    """One SpecReport. display fields are private, never journaled."""

    leaf_node_type: str
    state: str
    num_attempts: int
    run_time_ns: int
    start_time: str | None
    end_time: str | None
    display_name: str
    native_key: str
    failure_node_type: str | None
    failure_text: str | None

    @property
    def is_test(self) -> bool:
        return self.leaf_node_type == "It"

    @property
    def deselected(self) -> bool:
        # A spec the filter removed was never attempted and never ended.
        # A pending spec is not filtered: it is marked pending, not
        # deselected by a selection expression.
        return self.state == "skipped" and self.num_attempts == 0 and self.end_time is None


@dataclass(frozen=True, kw_only=True)
class GinkgoSuite:
    """One parsed Ginkgo suite report."""

    succeeded: bool
    run_time_ns: int
    start_time: str | None
    end_time: str | None
    pre_run_total: int
    pre_run_will_run: int
    specs: list[GinkgoSpec]
    special_failure_reasons: list[str]
    suite_path: str
    duplicate_identities: int


@dataclass(frozen=True, kw_only=True)
class GinkgoSuiteSplit:
    """One suite from a merged multi-suite capture, with the distinct
    caller-relative suite path the caller should convert it under."""

    suite: GinkgoSuite
    suggested_suite_path: str


def _relative_file_name(*, file_name: str, suite_path: str) -> str:
    """Suite-relative source file: absolute roots differ between
    checkouts, so only the part under the suite's own path is identity."""
    prefix = suite_path.rstrip("/")
    if prefix and file_name.startswith(prefix + "/"):
        return file_name[len(prefix) + 1 :]
    return file_name


def parse_ginkgo_report(*, raw: bytes) -> GinkgoSuite:
    """Parse and shape-check one Ginkgo suite report."""

    suites = parse_ginkgo_suites(raw=raw)
    if len(suites) != 1:
        raise AdapterError("ginkgo report must be a list with exactly one suite")
    return suites[0]


def parse_ginkgo_suites(*, raw: bytes) -> list[GinkgoSuite]:
    """Parse every suite in a Ginkgo report array (one or many)."""

    document = load_json_bytes(raw=raw)
    if not isinstance(document, list) or len(document) < 1:
        raise AdapterError("ginkgo report must be a non-empty list of suites")
    return [_parse_suite(entry) for entry in document]


def _parse_suite(suite: object) -> GinkgoSuite:
    if not isinstance(suite, dict):
        raise AdapterError("ginkgo suite must be a JSON object")
    for name in (
        "SuiteSucceeded",
        "RunTime",
        "StartTime",
        "EndTime",
        "PreRunStats",
        "SpecReports",
        "SuiteDescription",
        "SuitePath",
        "SpecialSuiteFailureReasons",
    ):
        if name not in suite:
            raise AdapterError("ginkgo suite is missing a required field")
    pre_run = suite["PreRunStats"]
    if not isinstance(pre_run, dict):
        raise AdapterError("ginkgo PreRunStats must be an object")
    for name in ("TotalSpecs", "SpecsThatWillRun"):
        if not isinstance(pre_run.get(name), int) or isinstance(pre_run.get(name), bool) or pre_run[name] < 0:
            raise AdapterError("ginkgo PreRunStats counts must be non-negative integers")
    raw_specs = suite["SpecReports"]
    if not isinstance(raw_specs, list):
        raise AdapterError("ginkgo SpecReports must be a list")
    if len(raw_specs) > MAX_SPECS:
        raise AdapterError("ginkgo report exceeds the spec count bound")
    reasons = suite["SpecialSuiteFailureReasons"]
    if reasons is None:
        reasons = []
    if not isinstance(reasons, list) or len(reasons) > MAX_SPECIAL_REASONS:
        raise AdapterError("ginkgo SpecialSuiteFailureReasons must be a bounded list")
    suite_path = suite["SuitePath"]
    if not isinstance(suite_path, str):
        raise AdapterError("ginkgo SuitePath must be a string")

    specs, duplicate_identities = _disambiguate_identities(
        [_parse_spec(spec, suite_path=suite_path) for spec in raw_specs]
    )
    return GinkgoSuite(
        succeeded=_required_bool(suite["SuiteSucceeded"], "SuiteSucceeded"),
        run_time_ns=_required_int(suite["RunTime"], "RunTime"),
        start_time=to_utc_z(native=_required_str(suite["StartTime"], "StartTime")),
        end_time=to_utc_z(native=_required_str(suite["EndTime"], "EndTime")),
        pre_run_total=pre_run["TotalSpecs"],
        pre_run_will_run=pre_run["SpecsThatWillRun"],
        specs=specs,
        special_failure_reasons=[reason if isinstance(reason, str) else "" for reason in reasons],
        suite_path=suite_path,
        duplicate_identities=duplicate_identities,
    )


def _parse_spec(spec: object, *, suite_path: str) -> GinkgoSpec:
    if not isinstance(spec, dict):
        raise AdapterError("ginkgo spec report must be an object")
    for name in (
        "LeafNodeType",
        "State",
        "NumAttempts",
        "RunTime",
        "StartTime",
        "EndTime",
        "ContainerHierarchyTexts",
        "LeafNodeText",
    ):
        if name not in spec:
            raise AdapterError("ginkgo spec report is missing a required field")
    leaf_node_type = spec["LeafNodeType"]
    state = spec["State"]
    if not isinstance(leaf_node_type, str) or not isinstance(state, str):
        raise AdapterError("ginkgo spec report fields must be strings")
    if state not in STATE_OUTCOMES:
        raise AdapterError("ginkgo spec report has an unknown state")
    # Hook nodes carry a null hierarchy: an empty container list.
    hierarchy = spec["ContainerHierarchyTexts"]
    if hierarchy is None:
        hierarchy = []
    leaf_text = spec["LeafNodeText"]
    if not isinstance(hierarchy, list) or not all(isinstance(item, str) for item in hierarchy):
        raise AdapterError("ginkgo spec hierarchy must be a list of strings")
    if not isinstance(leaf_text, str):
        raise AdapterError("ginkgo spec leaf text must be a string")
    location = spec.get("LeafNodeLocation")
    file_key = ""
    if isinstance(location, dict):
        file_name = location.get("FileName")
        if isinstance(file_name, str) and file_name:
            # Line numbers move with comment edits, so only the
            # suite-relative file participates in identity.
            file_key = _relative_file_name(file_name=file_name, suite_path=suite_path)
    failure = spec.get("Failure")
    failure_node_type: str | None = None
    failure_text: str | None = None
    if isinstance(failure, dict):
        failure_node_type = failure.get("FailureNodeType")
        failure_text = failure.get("Message")
        if failure_node_type is not None and not isinstance(failure_node_type, str):
            raise AdapterError("ginkgo failure node type must be a string")
        if failure_text is not None and not isinstance(failure_text, str):
            raise AdapterError("ginkgo failure message must be a string")
        if failure_node_type not in KNOWN_FAILURE_NODE_TYPES:
            raise AdapterError("ginkgo failure node type is not in the known set")
    display_name = "/".join([*hierarchy, leaf_text])
    return GinkgoSpec(
        leaf_node_type=leaf_node_type,
        state=state,
        num_attempts=_required_int(spec["NumAttempts"], "NumAttempts"),
        run_time_ns=_required_int(spec["RunTime"], "RunTime"),
        start_time=to_utc_z(native=_required_str(spec["StartTime"], "StartTime")),
        end_time=to_utc_z(native=_required_str(spec["EndTime"], "EndTime")),
        display_name=display_name,
        native_key="\n".join([display_name, leaf_node_type, file_key]),
        failure_node_type=failure_node_type,
        failure_text=failure_text,
    )


def _disambiguate_identities(specs: list[GinkgoSpec]) -> tuple[list[GinkgoSpec], int]:
    """Occurrence ordinals for specs sharing an identity key.

    Returns the disambiguated specs and how many beyond each group's
    first needed an ordinal. The ordinal reflects report order:
    textually identical specs in one file cannot be told apart more
    precisely.
    """
    seen: dict[str, int] = {}
    duplicates = 0
    result: list[GinkgoSpec] = []
    for spec in specs:
        count = seen.get(spec.native_key, 0)
        seen[spec.native_key] = count + 1
        if count > 0:
            duplicates += 1
            spec = dataclasses.replace(spec, native_key=f"{spec.native_key}\n#occurrence-{count + 1}")
        result.append(spec)
    return result, duplicates


def split_ginkgo_suites(*, raw: bytes, source_root: str) -> list[GinkgoSuiteSplit]:
    """Split a merged multi-suite capture into per-suite conversions.

    Each suite gets a suggested caller-relative suite path derived from
    its native SuitePath relative to the explicit source_root, so two
    packages in one capture convert under distinct namespaces and a
    repeated run of the same suite keeps the same definition IDs. The
    root is never inferred from the report set: a SuitePath outside the
    root is refused, because a wrong root would silently alias
    definitions. The caller gives each split suite its own invocation
    context (journal, invocation id, and a child exit code only when
    that suite's own command was measured); the overall CLI exit of the
    merged command belongs to the caller's composition, not to each
    suite.
    """

    suites = parse_ginkgo_suites(raw=raw)
    root = source_root.rstrip("/")
    if not root:
        raise AdapterError("source_root must be a non-empty path")
    splits: list[GinkgoSuiteSplit] = []
    seen_paths: dict[str, str] = {}
    for suite in suites:
        if not suite.suite_path.startswith(root + "/"):
            raise AdapterError("ginkgo SuitePath is not under the source root")
        relative = suite.suite_path[len(root) + 1 :]
        if not relative:
            raise AdapterError("ginkgo SuitePath equals the source root")
        # Two different native paths must not collapse onto one relative
        # path: that would alias definitions across packages.
        prior = seen_paths.get(relative)
        if prior is not None and prior != suite.suite_path:
            raise AdapterError("two distinct suites collapse onto one relative suite path")
        seen_paths[relative] = suite.suite_path
        splits.append(GinkgoSuiteSplit(suite=suite, suggested_suite_path=relative))
    return splits


def _required_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise AdapterError(f"ginkgo {name} must be a boolean")
    return value


def _required_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdapterError(f"ginkgo {name} must be a non-negative integer")
    return value


def _required_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise AdapterError(f"ginkgo {name} must be a string")
    return value


def ginkgo_journal_records(*, context: InvocationContext, suite: GinkgoSuite, suite_path: str) -> ConversionResult:
    """Convert one parsed suite into journal records plus explicit gaps.

    suite_path (caller-relative) participates in the hashed identity so
    the same spec text in different suites stays distinct, while the
    same spec across phase invocations of one suite stays equal.
    """

    writer = RecordWriter(context=context)
    display_names: dict[str, str] = {}

    started: dict[str, JSONValue] = {
        "invocation_id": context.invocation_id,
        "framework": "ginkgo",
        "tier": context.tier,
        "component": context.component,
        "variant": context.variant,
    }
    if context.selection is not None:
        started["selection"] = context.selection
    writer.emit(event="invocation_started", data=started, source_at=suite.start_time)

    tests = [spec for spec in suite.specs if spec.is_test]
    hooks = [spec for spec in suite.specs if not spec.is_test]

    if suite.duplicate_identities:
        writer.add_gap(code="ginkgo-duplicate-identity", count=suite.duplicate_identities)

    declared_deselected = suite.pre_run_total - suite.pre_run_will_run
    computed_deselected = sum(1 for spec in tests if spec.deselected)
    catalog_complete = len(tests) == suite.pre_run_total
    counts_consistent = computed_deselected == declared_deselected

    if not catalog_complete:
        # An incomplete spec list is not full collection evidence: no
        # collected or collection-finished records, and the declared
        # totals are reported as a gap instead of faked counts.
        writer.add_gap(code="ginkgo-catalog-incomplete", count=suite.pre_run_total - len(tests))
    elif not counts_consistent:
        # Pending-versus-filtered ambiguity or a report contradiction:
        # the per-spec deselection flags stay, the counts do not agree.
        writer.add_gap(code="ginkgo-deselection-mismatch", count=abs(computed_deselected - declared_deselected))
    else:
        for spec in tests:
            test_id = hashed_test_id(
                framework="ginkgo", component=context.component, native_key=f"{suite_path}\n{spec.native_key}"
            )
            display_names[test_id] = spec.display_name
            writer.emit(
                event="test_collected",
                data={"test_id": test_id, "deselected": spec.deselected},
                source_at=suite.start_time,
            )
        writer.emit(
            event="collection_finished",
            data={
                "outcome": "completed",
                "collected_count": suite.pre_run_total,
                "deselected_count": declared_deselected,
            },
            source_at=suite.start_time,
        )

    invocation_failure_signature: str | None = None
    failed_hook_count = 0
    for hook in hooks:
        if hook.state in HOOK_STATES_PASSED:
            continue
        failed_hook_count += 1
        if invocation_failure_signature is None and hook.failure_text is not None:
            invocation_failure_signature = failure_signature(
                framework="ginkgo", failure_class="hook-failure", raw_text=hook.failure_text
            )
    if failed_hook_count:
        writer.add_gap(code="ginkgo-hook-failure", count=failed_hook_count)

    for spec in tests:
        test_id = hashed_test_id(
            framework="ginkgo", component=context.component, native_key=f"{suite_path}\n{spec.native_key}"
        )
        display_names[test_id] = spec.display_name
        outcome = STATE_OUTCOMES[spec.state]
        data: dict[str, JSONValue] = {
            "result_scope": "aggregate",
            "test_id": test_id,
            "attempt_count": spec.num_attempts,
            "outcome": outcome,
        }
        if spec.num_attempts > 0:
            # A never-attempted spec's zero RunTime is a placeholder, not
            # a measured span.
            data["duration_ns"] = spec.run_time_ns
        if spec.start_time is not None:
            data["source_started_at"] = spec.start_time
        if spec.end_time is not None:
            data["source_ended_at"] = spec.end_time
        if outcome in OUTCOMES_WITH_FAILURE and spec.failure_text is not None:
            failure_class = "spec-failure" if spec.failure_node_type in ("", "It", None) else "hook-failure"
            data["failure"] = {
                "signature": failure_signature(
                    framework="ginkgo", failure_class=failure_class, raw_text=spec.failure_text
                )
            }
            if invocation_failure_signature is None:
                invocation_failure_signature = data["failure"]["signature"]
        writer.emit(event="test_result", data=data, source_at=spec.end_time or spec.start_time)

    if not suite.succeeded and invocation_failure_signature is None and suite.special_failure_reasons:
        invocation_failure_signature = failure_signature(
            framework="ginkgo", failure_class="suite-failure", raw_text="\n".join(suite.special_failure_reasons)
        )
        writer.add_gap(code="ginkgo-special-suite-failure", count=len(suite.special_failure_reasons))

    # A measured nonzero child exit cannot label this invocation passed,
    # even when every spec in the report succeeded: plain Go tests in the
    # same binary can fail outside the Ginkgo report. The per-spec
    # outcomes stand; the contradiction is a fixed gap. An unmeasured
    # exit (a split capture) stays null and never copies the global CLI
    # exit into every split.
    report_outcome = "passed" if suite.succeeded else "failed"
    invocation_outcome = check_exit_contradiction(
        writer=writer, framework="ginkgo", report_outcome=report_outcome, child_exit_code=context.child_exit_code
    )
    finished: dict[str, JSONValue] = {
        "invocation_id": context.invocation_id,
        "outcome": invocation_outcome,
        "exit_code": context.child_exit_code,
        "duration_ns": suite.run_time_ns,
    }
    if not suite.succeeded and invocation_failure_signature is not None:
        finished["failure"] = {"signature": invocation_failure_signature}
    writer.emit(event="invocation_finished", data=finished, source_at=suite.end_time)

    # Terminal collection state: the adapter's gaps persist in the journal
    # itself, as fixed labels, not only in the Python return value.
    writer.emit(
        event="collection_state",
        data=collection_state_data(
            framework="ginkgo", gaps=writer.gaps, covered_from=suite.start_time, covered_to=suite.end_time
        ),
        source_at=suite.end_time,
    )
    return writer.result(display_names=display_names)
