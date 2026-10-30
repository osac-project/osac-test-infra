"""Shared context, envelope, timestamp, duration, and identity primitives.

Every conversion is pure: parse one native report, return journal records
plus explicit gaps; the caller decides what to do with them. Native
reports carry no monotonic clock, so converted records always use
mono_ns null and never invent clock domains, phases, workers, or retry
intervals.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, Overflow
from functools import lru_cache
from pathlib import Path
from typing import TypeAlias

JSONValue: TypeAlias = bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"] | None

RECORD_SCHEMA = "osac.ci-observability.journal-record"
RECORD_SCHEMA_VERSION = 1

ADAPTER_NAME = "osac-ci-observability-native-adapter"
ADAPTER_VERSION = "0.1.0"

MAX_NATIVE_FILE_BYTES = 32 * 1024 * 1024
MAX_TEST_ID_BYTES = 512
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
UTC_Z_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")
RFC3339_PATTERN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CALENDAR_MAX = datetime.max.replace(tzinfo=UTC)
# The supported representable domain for native epoch times and
# durations: the span from the epoch to the end of the supported
# four-digit-year calendar. The bound is derived from that domain, not
# chosen arbitrarily, so a small scientific literal in a report cannot
# amplify into a giant integer allocation or a decimal-context
# overflow; values beyond it are refused, never normalized.
MAX_SUPPORTED_NS = int((_CALENDAR_MAX - _EPOCH).total_seconds()) * 1_000_000_000
_MAX_SUPPORTED_MS = Decimal(MAX_SUPPORTED_NS) / Decimal(1_000_000)
_MAX_SUPPORTED_SECONDS = Decimal(MAX_SUPPORTED_NS) / Decimal(1_000_000_000)

_CONTRACTS_V1 = Path(__file__).resolve().parent.parent / "contracts" / "v1"


@lru_cache(maxsize=1)
def _frozen_taxonomy_validators() -> dict[str, object]:
    """Validators built from the owning frozen v1 contract schemas.

    The InvocationContext bounds are the frozen contract's bounds, read
    from the same schema bytes the contract validator uses, so they
    cannot drift into a private mini-schema.
    """

    import jsonschema

    common = json.loads((_CONTRACTS_V1 / "common.schema.json").read_bytes())
    record_schema = json.loads((_CONTRACTS_V1 / "journal-record.schema.json").read_bytes())
    subschemas = {
        "framework": common["$defs"]["framework"],
        "tier": common["$defs"]["tier"],
        "selection": common["$defs"]["selection"],
        "component": record_schema["$defs"]["invocation_started_data"]["properties"]["component"],
        "variant": record_schema["$defs"]["invocation_started_data"]["properties"]["variant"],
    }
    return {name: jsonschema.Draft7Validator(schema) for name, schema in subschemas.items()}


class AdapterError(ValueError):
    """A native report violates a bound or an expected shape.

    Messages are fixed constants with ordinals or counts only; they never
    echo report content, so a rejected secret cannot leak.
    """


@dataclass(frozen=True, kw_only=True)
class InvocationContext:
    """Caller-supplied identity of one native invocation.

    The context is the only source of execution, journal, and invocation
    identity, of the suite taxonomy, and of the child exit code: a native
    report contains none of these, and none are inferred.
    """

    execution_id: str
    journal_id: str
    invocation_id: str
    framework: str
    tier: str
    component: str
    variant: str
    observed_at: str
    child_exit_code: int | None = None
    selection: dict[str, JSONValue] | None = None

    def __post_init__(self) -> None:
        for name in ("execution_id",):
            value = getattr(self, name)
            if not isinstance(value, str) or not EXECUTION_ID_PATTERN.fullmatch(value):
                raise AdapterError("context execution_id must be a bounded identifier")
        for name in ("journal_id", "invocation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
                raise AdapterError(f"context {name} must be a bounded identifier")
        validators = _frozen_taxonomy_validators()
        for name in ("framework", "tier", "component", "variant"):
            value = getattr(self, name)
            if not isinstance(value, str) or not validators[name].is_valid(value):  # type: ignore[attr-defined]
                raise AdapterError(f"context {name} must match the frozen contract taxonomy")
        if self.framework == "mixed":
            # The frozen enum's 'mixed' is a job-level suite value; every
            # native invocation record names its actual framework.
            raise AdapterError("context framework must be a native invocation framework, not the job-level mixed value")
        if not isinstance(self.observed_at, str) or not UTC_Z_PATTERN.fullmatch(self.observed_at):
            raise AdapterError("context observed_at must be UTC Z timestamp text")
        try:
            datetime.strptime(self.observed_at[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            raise AdapterError("context observed_at must be a real calendar date-time") from None
        if self.child_exit_code is not None and (
            isinstance(self.child_exit_code, bool) or not isinstance(self.child_exit_code, int)
        ):
            # The frozen invocation_finished.exit_code admits any plain
            # integer, including the negative code a signalled child
            # reports: the measured value is preserved exactly.
            raise AdapterError("context child_exit_code must be None or a measured integer exit code")
        if self.selection is not None and (
            not isinstance(self.selection, dict) or not validators["selection"].is_valid(self.selection)  # type: ignore[attr-defined]
        ):
            raise AdapterError("context selection must match the frozen contract selection shape")


@dataclass(frozen=True, kw_only=True)
class AdapterGap:
    """One explicit coverage gap: a fixed code plus a bounded count."""

    code: str
    count: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", self.code):
            raise AdapterError("gap code must be a bounded constant code")
        if not isinstance(self.count, int) or self.count < 1:
            raise AdapterError("gap count must be a positive integer")


@dataclass(kw_only=True)
class RecordWriter:
    """Shared journal-record builder for one conversion.

    Replaces the one-line emit/add_gap closures each converter
    previously repeated. The sequence counter is private state; callers
    get records and gaps as plain lists.
    """

    context: InvocationContext
    _records: list[dict[str, JSONValue]] = field(default_factory=list)
    _gaps: list[AdapterGap] = field(default_factory=list)
    _sequence: int = 0

    def emit(self, *, event: str, data: dict[str, JSONValue], source_at: str | None = None) -> None:
        self._sequence += 1
        self._records.append(
            journal_record(context=self.context, sequence=self._sequence, event=event, data=data, source_at=source_at)
        )

    def add_gap(self, *, code: str, count: int = 1) -> None:
        self._gaps.append(AdapterGap(code=code, count=count))

    @property
    def gaps(self) -> list[AdapterGap]:
        """Gaps recorded so far, for the terminal collection state."""
        return self._gaps

    def result(self, *, display_names: dict[str, str]) -> ConversionResult:
        return ConversionResult(records=self._records, gaps=self._gaps, display_names=display_names)


@dataclass(frozen=True, kw_only=True)
class ConversionResult:
    """Journal records for one invocation, its gaps, and its private
    display names. records are frozen-v1 journal envelopes. gaps carry
    constant codes. display_names maps hashed test ids to raw native
    display text: private by contract, never written to a journal."""

    records: list[dict[str, JSONValue]] = field(default_factory=list)
    gaps: list[AdapterGap] = field(default_factory=list)
    display_names: dict[str, str] = field(default_factory=dict)


def check_exit_contradiction(
    *, writer: RecordWriter, framework: str, report_outcome: str, child_exit_code: int | None
) -> str:
    """The invocation outcome a report's own claim cannot override.

    A measured nonzero child exit cannot be labeled a passing
    invocation, even when the report says every test passed: plain Go
    tests in the same binary, a panic after the report, or a wrapper
    can all fail outside the report's view. The native per-test
    outcomes are preserved; the contradiction is a fixed gap, never an
    invented failing test or a cause category.
    """
    if child_exit_code is not None and child_exit_code != 0 and report_outcome == "passed":
        writer.add_gap(code=f"{framework}-exit-contradicts-report", count=1)
        return "failed"
    return report_outcome


def journal_record(
    *, context: InvocationContext, sequence: int, event: str, data: dict[str, JSONValue], source_at: str | None = None
) -> dict[str, JSONValue]:
    """Build one frozen-v1 journal envelope with mono_ns null."""
    record: dict[str, JSONValue] = {
        "schema": RECORD_SCHEMA,
        "schema_version": RECORD_SCHEMA_VERSION,
        "execution_id": context.execution_id,
        "journal_id": context.journal_id,
        "sequence": sequence,
        "recorded_at": context.observed_at,
        "mono_ns": None,
        "event": event,
        "data": data,
        "invocation_id": context.invocation_id,
    }
    if source_at is not None:
        record["source_at"] = source_at
    return record


def to_utc_z(*, native: str) -> str | None:
    """Convert native RFC3339 text to UTC-Z text, preserving up to nine
    fractional digits exactly. Ginkgo's zero time (year 1) is the absent
    sentinel and returns None."""

    if not isinstance(native, str):
        raise AdapterError("timestamp must be RFC3339 text")
    match = RFC3339_PATTERN.fullmatch(native)
    if match is None:
        raise AdapterError("timestamp must be RFC3339 text with an offset and at most nine fractional digits")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    fraction = match.group(7) or ""
    offset = match.group(8)
    if year == 1 and month == 1 and day == 1 and hour == 0 and minute == 0 and second == 0 and not fraction.strip("0"):
        return None
    if offset == "Z":
        delta = timedelta(0)
    else:
        sign = 1 if offset[0] == "+" else -1
        delta = sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
    try:
        moment = datetime(year, month, day, hour, minute, second, tzinfo=timezone(delta)).astimezone(UTC)
    except (ValueError, OverflowError):
        raise AdapterError("timestamp is not a real calendar date-time") from None
    if moment.year < 1000:
        raise AdapterError("converted timestamp must keep a four-digit year")
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if fraction:
        text += "." + fraction
    return text + "Z"


def _integral_nanoseconds(*, value: Decimal) -> int:
    if not value.is_finite():
        raise AdapterError("duration must be a finite value")
    if value < 0 or value != value.to_integral_value():
        raise AdapterError("duration must convert to a non-negative integer number of nanoseconds")
    if value > MAX_SUPPORTED_NS:
        raise AdapterError("duration exceeds the supported representable span")
    return int(value)


def duration_ns_from_seconds_text(*, text: str) -> int:
    """Convert a native seconds literal (JUnit time attribute) to ns."""
    try:
        seconds = Decimal(text)
    except InvalidOperation:
        raise AdapterError("duration must be a decimal seconds literal") from None
    except Overflow:
        # The literal's exponent alone exceeds the decimal context's
        # representable range: far beyond the supported span.
        raise AdapterError("duration exceeds the supported representable span") from None
    if not seconds.is_finite():
        raise AdapterError("duration must be a finite decimal seconds literal")
    # Guard the magnitude before any scaling arithmetic: a huge
    # scientific literal would otherwise overflow the decimal context.
    if seconds.copy_abs() > _MAX_SUPPORTED_SECONDS:
        raise AdapterError("duration exceeds the supported representable span")
    try:
        return _integral_nanoseconds(value=seconds * 1_000_000_000)
    except (InvalidOperation, Overflow, ValueError):
        raise AdapterError("duration exceeds the supported representable span") from None


def duration_ns_from_ms(*, value: float | int) -> int:
    """Convert a native milliseconds value (Vitest duration) to ns.

    The float's shortest repr is read through Decimal so the literal's
    exact decimal expansion is quantized once, half-even, instead of
    losing digits through binary float arithmetic.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdapterError("duration must be a number of milliseconds")
    milliseconds = Decimal(str(value))
    if not milliseconds.is_finite() or milliseconds.copy_abs() > _MAX_SUPPORTED_MS:
        # Guard the magnitude before quantize: a huge literal would
        # otherwise raise decimal.InvalidOperation, and a bigger one
        # would amplify into a giant integer allocation.
        raise AdapterError("duration exceeds the supported representable span")
    try:
        scaled = milliseconds * 1_000_000
        return _integral_nanoseconds(value=scaled.quantize(Decimal(1), rounding="ROUND_HALF_EVEN"))
    except (InvalidOperation, Overflow, ValueError):
        raise AdapterError("duration exceeds the supported representable span") from None


def duration_ns_from_span(*, start: str, end: str) -> int:
    """Exact ns between two native RFC3339 texts, through integer second
    arithmetic; the fraction digits never pass through a float."""

    start_match = RFC3339_PATTERN.fullmatch(start)
    end_match = RFC3339_PATTERN.fullmatch(end)
    if start_match is None or end_match is None:
        raise AdapterError("duration span needs two RFC3339 texts")

    def epoch_ns(match: re.Match[str]) -> int:
        year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
        fraction = match.group(7) or ""
        offset = match.group(8)
        if offset == "Z":
            delta = timedelta(0)
        else:
            sign = 1 if offset[0] == "+" else -1
            delta = sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
        try:
            moment = datetime(year, month, day, hour, minute, second, tzinfo=timezone(delta)).astimezone(UTC)
        except (ValueError, OverflowError):
            raise AdapterError("duration span needs real calendar date-times") from None
        whole = int((moment - _EPOCH).total_seconds())
        return whole * 1_000_000_000 + int((fraction + "000000000")[:9])

    return epoch_ns(end_match) - epoch_ns(start_match)


def epoch_ms_to_utc_z(*, value: float | int) -> str:
    """Convert an epoch-milliseconds value (Vitest suite times) to UTC-Z
    text, keeping exactly the significant sub-second digits of the
    literal."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdapterError("epoch time must be a number of milliseconds")
    nanoseconds = Decimal(str(value)) * 1_000_000
    if not nanoseconds.is_finite():
        raise AdapterError("epoch time must be a finite value")
    if nanoseconds != nanoseconds.to_integral_value():
        raise AdapterError("epoch time must convert to an integer number of nanoseconds")
    if nanoseconds < 0:
        raise AdapterError("epoch time must not be before the epoch")
    if nanoseconds > MAX_SUPPORTED_NS:
        # Guard before the integer conversion and the timedelta: the
        # epoch must fit the supported calendar, and a huge literal must
        # not amplify into a giant allocation or an OverflowError.
        raise AdapterError("epoch time exceeds the supported calendar range")
    total = int(nanoseconds)
    seconds, remainder = divmod(total, 1_000_000_000)
    try:
        moment = _EPOCH + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        raise AdapterError("epoch time exceeds the supported calendar range") from None
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f"{remainder:09d}".rstrip("0")
    if fraction:
        text += "." + fraction
    return text + "Z"


def hashed_test_id(*, framework: str, component: str, native_key: str) -> str:
    """Stable hashed identity in the framework/component namespace.

    Raw native names and parameter text are not intrinsically safe, so
    only the hash enters journal records; the raw text stays in the
    private display mapping.
    """
    if not isinstance(native_key, str) or not native_key:
        raise AdapterError("native identity must be non-empty text")
    digest = hashlib.sha256(native_key.encode("utf-8")).hexdigest()[:16]
    test_id = f"{framework}:{component}:{digest}"
    if len(test_id.encode("utf-8")) > MAX_TEST_ID_BYTES:
        raise AdapterError("hashed identity exceeded the contract bound")
    return test_id


def failure_signature(*, framework: str, failure_class: str, raw_text: str) -> str:
    """Safe failure signature: an allowlisted class plus a content hash.

    The raw message, stack trace, and captured output never enter the
    signature; only their digest does.
    """
    if not re.fullmatch(r"[a-z0-9-]{1,32}", failure_class):
        raise AdapterError("failure class must be an allowlisted constant")
    digest = hashlib.sha256(raw_text.encode("utf-8", errors="strict")).hexdigest()[:16]
    return f"{framework}:{failure_class}:{digest}"


# Loss semantics per gap code. dropped_records is a required, measured
# integer in the frozen v1 schema, so each gap states what its count
# means instead of summing everything:
#   zero drops  -- every fact was delivered; the gap is a representation
#                  limit or attribution nuance, not a lost record;
#   counted     -- the gap's count is exactly how many records are
#                  missing from the output;
#   unknown     -- the loss cannot be measured. The schema forces an
#                  integer, so dropped_records stays 0 and omitted_fields
#                  names it as a placeholder the source could not fill.
GAP_LOSS_COUNTED = frozenset({"ginkgo-catalog-incomplete"})
GAP_LOSS_UNKNOWN = frozenset(
    {"go-test-build-failed", "go-test-no-test-events", "vitest-count-mismatch", "vitest-counter-mismatch"}
)


def collection_state_data(
    *, framework: str, gaps: list[AdapterGap], covered_from: str | None, covered_to: str | None
) -> dict[str, JSONValue]:
    """Terminal collection_state payload persisting the adapter's gaps.

    Gap codes become fixed error labels. dropped_records counts only the
    gaps whose missing records are known; representation limits state
    zero drops; unmeasurable loss keeps the schema-required integer at 0
    and names dropped_records in omitted_fields. A gapless conversion is
    a clean terminal state. No v1 fields are added.
    """
    dropped = sum(gap.count for gap in gaps if gap.code in GAP_LOSS_COUNTED)
    loss_unknown = any(gap.code in GAP_LOSS_UNKNOWN for gap in gaps)
    data: dict[str, JSONValue] = {
        "source": f"{ADAPTER_NAME}:{framework}",
        "status": "degraded" if gaps else "stopped",
        "complete": not gaps,
        "dropped_records": dropped,
    }
    if loss_unknown:
        data["omitted_fields"] = ["dropped_records"]
    if covered_from is not None:
        data["covered_from"] = covered_from
    if covered_to is not None:
        data["covered_to"] = covered_to
    if gaps:
        codes: list[str] = []
        for gap in gaps:
            if gap.code not in codes:
                codes.append(gap.code)
        data["errors"] = [{"message": code} for code in codes[:32]]
    return data
