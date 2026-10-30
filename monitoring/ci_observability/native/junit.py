"""JUnit XML parsing and conversion to frozen v1 journal records.

JUnit XML carries no case start or end times and no collection catalog,
so converted records carry neither source timestamps nor collection
events. A case's setup-versus-teardown role is not represented, and a
non-strict unexpected pass is indistinguishable from a plain pass: both
limitations are documented rather than papered over.

Duplicate (classname, name) cases are one identity, never invented
retries: the identity emits a single aggregate whose outcome is the
common outcome when the cases agree and "unknown" when they disagree,
always with attempt_count null and no duration, plus an explicit
duplicate-identity gap.

The XML is untrusted input. DTDs and entity declarations are rejected
with expat's declaration handlers, which fire after encoding detection:
a UTF-16 or UTF-32 document cannot smuggle a DOCTYPE past the raw byte
scan. Node, depth, and text bounds are enforced while parsing, before
an unbounded tree can be allocated, and no external entity is ever
fetched.
"""

from __future__ import annotations

import xml.parsers.expat
from dataclasses import dataclass, field

from .records import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    AdapterError,
    ConversionResult,
    InvocationContext,
    JSONValue,
    RecordWriter,
    check_exit_contradiction,
    collection_state_data,
    duration_ns_from_seconds_text,
    failure_signature,
    hashed_test_id,
)

MAX_XML_BYTES = 32 * 1024 * 1024
MAX_XML_NODES = 200_000
MAX_XML_DEPTH = 64
MAX_XML_TEXT_BYTES = 16 * 1024 * 1024

CASE_CHILD_OUTCOMES = {"failure": "failed", "error": "error"}
SKIPPED_OUTCOME = "skipped"
XFAIL_TYPE = "pytest.xfail"


@dataclass(kw_only=True)
class _XmlNode:
    """A bounded element built during parsing. Mutable only while the
    parser's handlers fill it in."""

    tag: str
    attrs: dict[str, str]
    children: list[_XmlNode] = field(default_factory=list)
    text: str = ""
    # Character data accumulates here during parsing and is joined into
    # text once after the document ends: per-node string concatenation
    # is quadratic in the number of expat callbacks.
    chunks: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class JunitCase:
    """One testcase element. Raw text fields are private, never journaled."""

    classname: str
    name: str
    outcome: str
    duration_ns: int | None
    failure_class: str | None
    failure_text: str | None
    xfail: bool


@dataclass(frozen=True, kw_only=True)
class JunitReport:
    """One parsed JUnit document."""

    cases: list[JunitCase]


def parse_junit_xml(*, raw: bytes) -> JunitReport:
    """Parse and shape-check one JUnit XML document."""

    if not isinstance(raw, (bytes, bytearray)):
        raise AdapterError("junit report must be bytes")
    if len(raw) > MAX_XML_BYTES:
        raise AdapterError("junit report exceeds the file byte bound")
    # Extra check only: the authoritative rejection lives in the expat
    # declaration handlers, which run after encoding detection.
    lowered = bytes(raw).lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise AdapterError("junit report must not contain a DTD or entity declarations")

    root = _parse_bounded(bytes(raw))
    if root.tag not in ("testsuites", "testsuite"):
        raise AdapterError("junit report root must be testsuites or testsuite")
    cases: list[JunitCase] = []
    for element in _iter(root):
        if element.tag != "testcase":
            continue
        cases.append(_parse_case(element))
    return JunitReport(cases=cases)


def _parse_bounded(raw: bytes) -> _XmlNode:
    """Parse with expat, rejecting declarations and enforcing bounds
    during parsing, before any unbounded tree exists."""

    parser = xml.parsers.expat.ParserCreate()
    state = {"nodes": 0, "text": 0, "depth": 0}
    stack: list[_XmlNode] = []
    root_holder: list[_XmlNode] = []

    def reject_doctype(*args: object) -> None:
        raise AdapterError("junit report must not contain a DTD or entity declarations")

    def reject_entity(*args: object) -> None:
        raise AdapterError("junit report must not contain a DTD or entity declarations")

    def start(tag: str, attrs: dict[str, object]) -> None:
        state["nodes"] += 1
        if state["nodes"] > MAX_XML_NODES:
            raise AdapterError("junit report exceeds the node count bound")
        state["depth"] += 1
        if state["depth"] > MAX_XML_DEPTH:
            raise AdapterError("junit report exceeds the nesting depth bound")
        safe_attrs = {key: value for key, value in attrs.items() if isinstance(value, str)}
        node = _XmlNode(tag=tag, attrs=safe_attrs)
        if stack:
            stack[-1].children.append(node)
        else:
            root_holder.append(node)
        stack.append(node)

    def end(tag: str) -> None:
        state["depth"] -= 1
        if stack:
            stack.pop()

    def chars(data: str) -> None:
        state["text"] += len(data.encode("utf-8"))
        if state["text"] > MAX_XML_TEXT_BYTES:
            raise AdapterError("junit report exceeds the text byte bound")
        if stack:
            stack[-1].chunks.append(data)

    def finalize(node: _XmlNode) -> None:
        node.text = "".join(node.chunks)
        node.chunks = []
        for child in node.children:
            finalize(child)

    parser.StartDoctypeDeclHandler = reject_doctype
    parser.EntityDeclHandler = reject_entity
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    # No ExternalEntityRefHandler: external entities are never fetched;
    # a reference to an undefined entity is a parse error.
    try:
        parser.Parse(raw, True)
    except AdapterError:
        raise
    except xml.parsers.expat.ExpatError:
        raise AdapterError("junit report is not well-formed XML") from None
    if not root_holder:
        raise AdapterError("junit report has no root element")
    finalize(root_holder[0])
    return root_holder[0]


def _iter(node: _XmlNode) -> object:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _parse_case(element: _XmlNode) -> JunitCase:
    classname = element.attrs.get("classname")
    name = element.attrs.get("name")
    if not classname or not name:
        raise AdapterError("junit testcase needs a classname and a name")
    if len(element.children) > 8:
        raise AdapterError("junit testcase exceeds the child element bound")
    time_text = element.attrs.get("time")
    duration_ns = duration_ns_from_seconds_text(text=time_text) if time_text is not None else None
    outcome = "passed"
    failure_class: str | None = None
    failure_text: str | None = None
    xfail = False
    for child in element.children:
        if child.tag in CASE_CHILD_OUTCOMES:
            outcome = CASE_CHILD_OUTCOMES[child.tag]
            failure_class = child.tag
            failure_text = child.attrs.get("message") or child.text or ""
        elif child.tag == "skipped":
            outcome = SKIPPED_OUTCOME
            if child.attrs.get("type") == XFAIL_TYPE:
                xfail = True
    if xfail:
        outcome = "expected_failure"
    return JunitCase(
        classname=classname,
        name=name,
        outcome=outcome,
        duration_ns=duration_ns,
        failure_class=failure_class,
        failure_text=failure_text,
        xfail=xfail,
    )


def junit_journal_records(*, context: InvocationContext, report: JunitReport) -> ConversionResult:
    """Convert one parsed JUnit document into journal records plus gaps."""

    writer = RecordWriter(context=context)
    display_names: dict[str, str] = {}

    started: dict[str, JSONValue] = {
        "invocation_id": context.invocation_id,
        "framework": context.framework,
        "tier": context.tier,
        "component": context.component,
        "variant": context.variant,
    }
    if context.selection is not None:
        started["selection"] = context.selection
    writer.emit(event="invocation_started", data=started)

    # One identity per (classname, name): duplicates collapse into a
    # single aggregate, never invented retries.
    grouped: dict[str, list[JunitCase]] = {}
    order: list[str] = []
    for case in report.cases:
        native_key = f"{case.classname}::{case.name}"
        if native_key not in grouped:
            grouped[native_key] = []
            order.append(native_key)
        grouped[native_key].append(case)

    duplicated_cases = len(report.cases) - len(order)
    if duplicated_cases:
        writer.add_gap(code="junit-duplicate-identity", count=duplicated_cases)
    if not report.cases:
        writer.add_gap(code="junit-no-test-cases")

    invocation_failed = False
    for native_key in order:
        cases = grouped[native_key]
        test_id = hashed_test_id(framework=context.framework, component=context.component, native_key=native_key)
        display_names[test_id] = native_key
        outcomes = {case.outcome for case in cases}
        outcome = outcomes.pop() if len(outcomes) == 1 else "unknown"
        data: dict[str, JSONValue] = {
            "result_scope": "aggregate",
            "test_id": test_id,
            # JUnit has no retry fields: the attempt count is unknown,
            # never invented as one.
            "attempt_count": None,
            "outcome": outcome,
        }
        if len(cases) == 1 and cases[0].duration_ns is not None:
            # With duplicates the durations disagree or repeat; none of
            # them is a measured span of this single aggregate.
            data["duration_ns"] = cases[0].duration_ns
        single = cases[0]
        if len(cases) == 1 and single.failure_class is not None and single.failure_text is not None:
            data["failure"] = {
                "signature": failure_signature(
                    framework=context.framework, failure_class=single.failure_class, raw_text=single.failure_text
                )
            }
        if outcome in ("failed", "error"):
            invocation_failed = True
        writer.emit(event="test_result", data=data)

    report_outcome = "failed" if invocation_failed else ("passed" if report.cases else "unknown")
    invocation_outcome = check_exit_contradiction(
        writer=writer,
        framework=context.framework,
        report_outcome=report_outcome,
        child_exit_code=context.child_exit_code,
    )
    writer.emit(
        event="invocation_finished",
        data={
            "invocation_id": context.invocation_id,
            "outcome": invocation_outcome,
            "exit_code": context.child_exit_code,
        },
    )
    writer.emit(
        event="collection_state",
        data=collection_state_data(framework=context.framework, gaps=writer.gaps, covered_from=None, covered_to=None),
    )
    return writer.result(display_names=display_names)


BUNDLE_PRODUCER = {"name": ADAPTER_NAME, "version": ADAPTER_VERSION}
