"""What a ci-observe E2E run spent its time on, and why it failed.

One small vocabulary, shared by the rollups and the read side, so every page
names a phase and a failure cause the same way:

- a *suite run* is one VMaaS, CaaS or BMaaS E2E execution: a full-install
  run, a pull-request suite run, or one suite of a merge-queue run;
- a *phase* is a group of workflow steps (queue, CI setup, cluster and
  hosts, build, install OSAC, tests, logs and teardown);
- a *cause* is the phase of the first step that failed, plus three causes no
  step can name (timed out, runner lost, blocked request).

Only the standard VMaaS, CaaS and BMaaS suites are in scope. Netris,
stretched, agentless-net, full-regression and the named VMaaS installs are
other scenarios on other infrastructure, so they are not part of the
population and `suite_key` returns "" for them.

Pure functions only: no I/O, no clock. The step names come from the
e2e-*-full-install.yml and e2e-vmaas.yml workflows; a test pins that every
step of those files lands in a named phase, so a new step cannot silently
fall into "other".
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

# -- pages --------------------------------------------------------------------

# Grafana paths of the ci-observe pages, /d/<uid>/<slug>. The slug is
# Grafana's slug of the title (the em dash becomes e28094). Grafana opens a
# page by its uid, so an old slug still works; a test pins these against the
# provisioned dashboards.
OVERVIEW_PAGE = "/d/ci-observe-home/ci-observe-e28094-overview"
TIME_PAGE = "/d/ci-observe-time/ci-observe-e28094-where-the-time-goes"
FAILURES_PAGE = "/d/ci-observe-failures/ci-observe-e28094-why-runs-fail"
TEST_PAGE = "/d/ci-observe-test/ci-observe-e28094-test-history"
PR_PAGE = "/d/ci-observe-pull-requests/ci-observe-e28094-pull-requests"
RUNS_PAGE = "/d/ci-observe-runs-home/ci-observe-e28094-runs"
RUN_PAGE = "/d/ci-observe-runs/ci-observe-e28094-e2e-run"
# The windows the pages offer, in days.
WINDOW_DAYS = (7, 14, 30, 90)


def window_days(days) -> int:
    """The page window that covers `days`."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return 30
    return next((option for option in WINDOW_DAYS if option >= days), WINDOW_DAYS[-1])

# -- suites -------------------------------------------------------------------

SUITES = ("vmaas", "caas", "bmaas")
SUITE_LABELS = {"vmaas": "VMaaS", "caas": "CaaS", "bmaas": "BMaaS", "all": "All suites"}

# The stored workflow names that are one in-scope suite. The exporter stores
# the run name GitHub reports, so a pull-request suite run appears as
# "E2E suite · vmaas · #123 · <sha>" and a merge-queue suite as
# "E2E vmaas (merge queue)". Lowercased, exact.
SUITE_WORKFLOWS = {
    "vmaas": (
        "e2e vmaas",
        "e2e vmaas full install",
        "e2e vmaas pr suite",
        "e2e vmaas (pull requests)",
        "e2e vmaas (merge queue)",
    ),
    "caas": (
        "e2e caas full install",
        "e2e caas pr suite",
        "e2e caas (pull requests)",
        "e2e caas (merge queue)",
    ),
    "bmaas": (
        "e2e bmaas full install",
        "e2e bmaas pr suite",
        "e2e bmaas (pull requests)",
        "e2e bmaas (merge queue)",
    ),
}
SUITE_PREFIXES = {suite: (f"e2e suite · {suite} · ",) for suite in SUITES}
_WORKFLOW_SUITE = {name: suite for suite, names in SUITE_WORKFLOWS.items() for name in names}

PR_IN_NAME_RE = re.compile(r"#(\d+)")
SHA_IN_NAME_RE = re.compile(r" · ([0-9a-f]{7,40})$")


def suite_key(workflow: str | None) -> str:
    """"vmaas", "caas" or "bmaas" for an in-scope suite workflow, "" otherwise."""
    name = (workflow or "").strip().lower()
    if name in _WORKFLOW_SUITE:
        return _WORKFLOW_SUITE[name]
    for suite, prefixes in SUITE_PREFIXES.items():
        if name.startswith(prefixes):
            return suite
    return ""


def suite_label(suite: str) -> str:
    return SUITE_LABELS.get(suite, suite or "Unknown")


# -- triggers -----------------------------------------------------------------

TRIGGERS = ("pr", "merge_queue", "scheduled", "manual", "push", "other")
TRIGGER_LABELS = {
    "pr": "Pull request",
    "merge_queue": "Merge queue",
    "scheduled": "Scheduled",
    "manual": "Manual",
    "push": "Push",
    "other": "Other",
}


def trigger_key(event: str | None, workflow: str | None) -> str:
    """Why the suite ran. A pull-request suite is started by the decision with
    workflow_dispatch, so its name, not its event, says it is a PR run."""
    event = (event or "").strip().lower()
    name = (workflow or "").strip().lower()
    if event == "merge_group" or name.endswith("(merge queue)"):
        return "merge_queue"
    if event == "pull_request" or name.startswith("e2e suite · ") or name.endswith((" pr suite", "(pull requests)")):
        return "pr"
    if event == "schedule":
        return "scheduled"
    if event == "workflow_dispatch":
        return "manual"
    if event == "push":
        return "push"
    return "other"


def pr_number(pr_display: str | None, workflow: str | None) -> str:
    """The pull request a suite run tested, as digits, or ""."""
    match = re.fullmatch(r"#?(\d+)", (pr_display or "").strip())
    if match:
        return match.group(1)
    if (workflow or "").lower().startswith("e2e suite · "):
        match = PR_IN_NAME_RE.search(workflow or "")
        if match:
            return match.group(1)
    return ""


def tested_sha(workflow: str | None, head_sha: str | None) -> str:
    """The commit the suite tested. A pull-request suite run starts from the
    default branch, so its head_sha is main; the commit is in its name."""
    match = SHA_IN_NAME_RE.search((workflow or "").strip())
    if match and (workflow or "").lower().startswith("e2e suite · "):
        return match.group(1)
    return (head_sha or "").strip()


# -- test identity ------------------------------------------------------------

# A test is named three ways: a pytest node id in the trace
# ("tests/e2e/vmaas/sanity/test_vm.py::TestVm::test_create[x]"), and a
# JUnit classname plus name in the artifact, dotted
# ("tests.e2e.vmaas.sanity.test_vm.TestVm" + "test_create[x]") or, from some
# plugins, slashed. All three become one canonical key:
# "tests.e2e.vmaas.sanity.test_vm::TestVm::test_create[x]".


def canonical_test_key(test_id: str | None) -> str:
    """The canonical key of a test, from a node id or a JUnit "classname::name"."""
    value = (test_id or "").strip()
    if not value:
        return ""
    head, sep, tail = value.partition("::")
    if not sep:
        return value
    if head.endswith(".py"):
        module = head[:-3].replace("/", ".")
        return f"{module}::{tail}"
    # A JUnit classname: the module path, then any test classes. Modules are
    # lowercase by convention and test classes are CamelCase, so the first
    # capitalized component starts the classes.
    components = [part for part in re.split(r"[./]", head) if part]
    split = next((index for index, part in enumerate(components) if part[:1].isupper()), len(components))
    module = ".".join(components[:split])
    classes = components[split:]
    return "::".join([module, *classes, tail]) if module else "::".join([*classes, tail])


def describe_test(key: str) -> dict:
    """{name, group, suite} for a canonical key: the short name a reader
    recognizes ("test_vm::TestVm::test_create"), the directory under
    tests/e2e ("vmaas/sanity") and the suite directory it lives in."""
    module, _, rest = (key or "").partition("::")
    parts = module.split(".")
    leaf = parts[-1] if parts else module
    inner = parts[2:-1] if parts[:2] == ["tests", "e2e"] else parts[:-1]
    group = "/".join(inner)
    suite = inner[0] if inner and inner[0] in SUITES else ""
    return {"name": f"{leaf}::{rest}" if rest else leaf, "group": group, "suite": suite}


def link_to_test(test_id: str | None, days=30, suite: str = "") -> str:
    """The Test history page of one test, from any of its three names."""
    key = canonical_test_key(test_id)
    if suite not in SUITES:
        suite = describe_test(key)["suite"] or "all"
    return f"{TEST_PAGE}?var-test={quote(key, safe='')}&var-days={window_days(days)}&var-suite={suite}"


def link_to_suite(page: str, suite: str = "all", days=7, repo: str = "", trigger: str = "all") -> str:
    """A page filtered to one suite (or all), keeping the reader's filters."""
    suite = suite if suite in SUITES else "all"
    return (
        f"{page}?var-suite={suite}&var-days={window_days(days)}"
        f"&var-repo={quote(repo or 'all', safe='')}&var-trigger={quote(trigger or 'all', safe='')}"
    )


# -- phases -------------------------------------------------------------------

# The eight categorical colors, in their fixed order. A chart gives its
# series slot 1, 2, 3... in order, never skipping. The order keeps neighbors
# apart for color-blind readers: validated on Grafana's dark (#181b1f) and
# light (#ffffff) panels, worst adjacent CVD delta E 8.4, normal vision 19.3.
PALETTE = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")

# (key, label, color). Ordered as a run executes them.
PHASES = (
    ("queue", "Waiting for a runner", PALETTE[0]),
    ("prepare", "CI setup", PALETTE[1]),
    ("cluster", "Cluster and hosts", PALETTE[2]),
    ("build", "Build", PALETTE[3]),
    ("install", "Install OSAC", PALETTE[4]),
    ("test", "Tests", PALETTE[5]),
    ("collect", "Logs and teardown", PALETTE[6]),
    ("other", "Other steps", PALETTE[7]),
)
PHASE_KEYS = tuple(key for key, _, _ in PHASES)
PHASE_LABELS = {key: label for key, label, _ in PHASES}
STEP_PHASES = tuple(key for key in PHASE_KEYS if key != "queue")

# Exact step names first: they are the workflows' own words.
_STEP_PHASE = {
    # CI setup: the runner, the code, credentials and tools.
    "set up job": "prepare",
    "checkout repository": "prepare",
    "overlay tests from osac mono-repo": "prepare",
    "validate and bootstrap": "prepare",
    "resolve and validate components input": "prepare",
    "reject stamped-image overrides in oci mode": "prepare",
    "authorize fork pr": "prepare",
    "fetch and write secrets": "prepare",
    "fetch caas secrets": "prepare",
    "fetch and merge stage registry credential": "prepare",
    "fetch and merge prod registry credential": "prepare",
    "fetch konflux dev registry credential": "prepare",
    "ensure helm and yq are available": "prepare",
    "install grpcurl": "prepare",
    "read approle credentials": "prepare",
    "retrieve secrets from vault": "prepare",
    "write secrets": "prepare",
    # The test cluster and its virtual hosts.
    "set clone name": "cluster",
    "prepare cluster environment": "cluster",
    "boot cluster clone": "cluster",
    "prepare cluster prerequisites": "cluster",
    "setup virtual baremetalhosts": "cluster",
    "setup caas agent": "cluster",
    # Build what the run installs and tests with.
    "log in to the chart's oci registry": "build",
    "resolve chart version to install": "build",
    "pull resolved chart and read its stamped image refs": "build",
    "extract osac cli from resolved staged image": "build",
    "build and load component images": "build",
    "extract osac cli from published image (fallback)": "build",
    "extract osac cli": "build",
    "build test image": "build",
    # Install OSAC and what it needs.
    "install infrastructure operators": "install",
    "install osac": "install",
    "deploy osac": "install",
    "resolve deployed fulfillment-service image": "install",
    "create test adapter route for metering tests": "install",
    # Run the suite.
    "resolve effective test suite (fallback if tier dir is empty)": "test",
    "resolve effective test suite (fail if requested tier is empty)": "test",
    "resolve effective test selection": "test",
    "run e2e tests": "test",
    "run e2e tests (ci-observe capture)": "test",
    "run caas e2e tests": "test",
    # Afterwards: evidence, cleanup and notifications.
    "gather compact boot diagnostics": "collect",
    "gather cluster logs": "collect",
    "gather cluster logs (job cancelled, short cap)": "collect",
    "teardown": "collect",
    "scan and upload artifacts": "collect",
    "scan and upload artifacts (job cancelled)": "collect",
    "authenticate for ai diagnosis": "collect",
    "run ai failure diagnosis": "collect",
    "notify slack on failure": "collect",
    "prepare ai diagnosis for slack": "collect",
    "post ai diagnosis to slack thread": "collect",
    "ping chai-bot to verify failure": "collect",
    "complete job": "collect",
}

# Fallbacks for a renamed or new step, most specific first.
_STEP_PATTERNS = (
    (re.compile(r"^post |^(gather|collect|teardown|scan and upload|upload|destroy|clean ?up)\b"), "collect"),
    (re.compile(r"\b(e2e|pytest)\b.*\btests?\b|^run .*tests?\b|^resolve effective test"), "test"),
    (re.compile(r"slack|ai (failure )?diagnosis|chai-bot|notify"), "collect"),
    (re.compile(r"^(install|deploy) (osac|infrastructure operators)|^setup osac|metering"), "install"),
    (re.compile(r"cluster|clone|baremetalhost|virtual host|caas agent"), "cluster"),
    (re.compile(r"^build |image|chart|^extract osac cli"), "build"),
    (re.compile(r"checkout|secret|credential|vault|approle|token|authorize|validate|bootstrap|^fetch |^ensure |"
                r"^install (grpcurl|helm|yq|tools?)"), "prepare"),
    # The gate and queue bookkeeping a suite run does before it starts: its
    # check run, readiness, superseded runs, the pull request and its labels,
    # and the shared suite-inputs action.
    (re.compile(r"\bgate\b|readiness|superseded|^read the |/suite-inputs@"), "prepare"),
)

# Steps whose failure means the request itself was refused: an unapproved
# fork, or inputs the workflow rejects. Nothing was installed or tested.
BLOCKED_STEPS = frozenset({
    "authorize fork pr",
    "reject stamped-image overrides in oci mode",
    "resolve and validate components input",
})


def step_phase(step: str | None, job: str | None = "") -> str:
    """The phase a workflow step belongs to."""
    name = (step or "").strip().lower()
    if name in _STEP_PHASE:
        return _STEP_PHASE[name]
    job_leaf = (job or "").strip().lower().rsplit(" / ", 1)[-1]
    if job_leaf == "verify":
        # The pull-request suite's input check, a job of its own.
        return "prepare"
    for pattern, phase in _STEP_PATTERNS:
        if pattern.search(name):
            return phase
    return "other"


# -- failure causes -----------------------------------------------------------

# (key, label, area, color). The area is the one-word answer to "did my code
# break, or did CI?".
# The areas a chart shows, one color each; a cause wears its area's color.
AREAS = ("Tests", "Build", "OSAC install", "CI infrastructure", "Blocked")
AREA_COLORS = dict(zip(AREAS, PALETTE, strict=False))
# The field a document counts an area's failures in.
AREA_KEYS = {
    "Tests": "area_tests",
    "Build": "area_build",
    "OSAC install": "area_install",
    "CI infrastructure": "area_infra",
    "Blocked": "area_blocked",
}
CAUSES = (
    ("test", "Tests failed", "Tests", AREA_COLORS["Tests"]),
    ("build", "Build failed", "Build", AREA_COLORS["Build"]),
    ("install", "OSAC install failed", "OSAC install", AREA_COLORS["OSAC install"]),
    ("cluster", "Cluster or host setup failed", "CI infrastructure", AREA_COLORS["CI infrastructure"]),
    ("prepare", "CI setup failed", "CI infrastructure", AREA_COLORS["CI infrastructure"]),
    ("collect", "Logs or teardown failed", "CI infrastructure", AREA_COLORS["CI infrastructure"]),
    ("timeout", "Timed out", "CI infrastructure", AREA_COLORS["CI infrastructure"]),
    ("runner", "Runner lost", "CI infrastructure", AREA_COLORS["CI infrastructure"]),
    ("blocked", "Blocked request", "Blocked", AREA_COLORS["Blocked"]),
    ("other", "Other step failed", "CI infrastructure", AREA_COLORS["CI infrastructure"]),
)
CAUSE_KEYS = tuple(key for key, *_ in CAUSES)
CAUSE_LABELS = {key: label for key, label, _, _ in CAUSES}
CAUSE_AREAS = {key: area for key, _, area, _ in CAUSES}

FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
# A failure's root cause is the earliest phase that failed: a teardown error
# after a failed test is not why the run failed.
_PHASE_ORDER = {key: index for index, key in enumerate(PHASE_KEYS)}


def result_key(conclusion: str | None) -> str:
    """"passed", "failed", "cancelled" or "other" for a GitHub conclusion."""
    value = (conclusion or "").strip().lower()
    if value == "success":
        return "passed"
    if value in FAILED_CONCLUSIONS:
        return "failed"
    if value == "cancelled":
        return "cancelled"
    return "other"


def failed_steps(failed_step_text: str | None) -> list[tuple[str, str]]:
    """[(job, step), ...] from the exporter's "job → step; job → step" text."""
    entries = []
    for entry in (failed_step_text or "").split("; "):
        if not entry.strip():
            continue
        job, _, step = entry.rpartition(" → ")
        entries.append((job.strip(), step.strip()))
    return entries


def failure_cause(conclusion: str | None, failed_step_text: str | None, timeline: list | None = None) -> dict:
    """Why a failed suite run failed: {cause, label, area, step, phase}.

    The first failed step in run order decides; when the exporter stored the
    run's timeline, a step GitHub cancelled at the time limit names a timeout,
    and a failure with no failed or cancelled step is a lost runner. Returns
    the empty cause for a run that did not fail.
    """
    if result_key(conclusion) != "failed":
        return {"cause": "", "label": "", "area": "", "step": "", "phase": ""}
    steps = failed_steps(failed_step_text)
    if steps:
        job, step = min(
            steps, key=lambda entry: (_PHASE_ORDER.get(step_phase(entry[1], entry[0]), 99))
        )
        phase = step_phase(step, job)
        if step.strip().lower() in BLOCKED_STEPS:
            cause = "blocked"
        elif (conclusion or "").lower() == "timed_out":
            cause = "timeout"
        else:
            cause = phase if phase in CAUSE_LABELS else "other"
        return _cause(cause, step, phase)
    cancelled = _cancelled_step(timeline)
    if (conclusion or "").lower() == "timed_out" or cancelled:
        step = cancelled[1] if cancelled else ""
        return _cause("timeout", step, step_phase(step, cancelled[0]) if cancelled else "")
    return _cause("runner", "", "")


def _cause(cause: str, step: str, phase: str) -> dict:
    return {
        "cause": cause,
        "label": CAUSE_LABELS[cause],
        "area": CAUSE_AREAS[cause],
        "step": step,
        "phase": phase,
    }


def _cancelled_step(timeline):
    """(job, step) of the step GitHub cancelled inside a failed job: what a
    job that hit its time limit looks like. None when there is none."""
    for job in timeline or []:
        if (job.get("conclusion") or "") not in ("failure", "timed_out"):
            continue
        for step in job.get("steps") or []:
            if (step.get("conclusion") or "") == "cancelled":
                return job.get("name") or "", step.get("name") or ""
    return None


# -- rollup passes ----------------------------------------------------------

# When each hourly rollup last finished a pass, new data or not, in
# trends.db. The pages read it to say how old their numbers are: a timer that
# stops leaves its last pass behind and the gap grows.
ROLLUP_PASSES_SCHEMA = """
CREATE TABLE IF NOT EXISTS rollup_passes (
    job TEXT PRIMARY KEY,
    finished_at TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT ''
)
"""


def record_pass(conn, job: str, now: datetime, summary: str = "") -> None:
    """Remember that `job` ("trends", "spans") finished a pass at `now`."""
    with conn:
        conn.execute(ROLLUP_PASSES_SCHEMA)
        conn.execute(
            "INSERT INTO rollup_passes (job, finished_at, summary) VALUES (?, ?, ?)"
            " ON CONFLICT(job) DO UPDATE SET finished_at = excluded.finished_at, summary = excluded.summary",
            (job, now.strftime("%Y-%m-%dT%H:%M:%SZ"), summary),
        )


# -- timelines ----------------------------------------------------------------

def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def seconds_between(start, end) -> float | None:
    first, last = parse_time(start), parse_time(end)
    if first is None or last is None:
        return None
    return max(0.0, (last - first).total_seconds())


def load_timeline(value) -> list:
    """The stored timeline_json as a list; [] for empty or unreadable text."""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def main_job(timeline: list) -> dict | None:
    """The job that ran the suite: the one named ".../ e2e" (or "e2e"), else
    the job that ran longest. The pull-request suite also has a short
    "verify" job on a GitHub-hosted runner, which is not the suite."""
    jobs = [job for job in timeline if job.get("started_at")]
    if not jobs:
        return None
    named = [job for job in jobs if (job.get("name") or "").strip().lower().rsplit(" / ", 1)[-1] == "e2e"]
    if named:
        return named[-1]
    return max(jobs, key=lambda job: seconds_between(job.get("started_at"), job.get("completed_at")) or 0)


def run_phases(timeline: list | None, steps: list | None = None) -> dict:
    """{phase: seconds} for one suite run, plus `queue_s` and `total_s`.

    With a timeline: each step's own start and end, the main job's wait for a
    runner as `queue`, and the wall clock from the first job's creation to
    the last job's completion as `total_s`. Without one (a row stored before
    timelines existed): the stored step durations, no queue, and `total_s`
    None so the caller can fall back to the stored run duration.
    """
    phases = dict.fromkeys(PHASE_KEYS, 0.0)
    measured = False
    if timeline:
        for job in timeline:
            for step in job.get("steps") or []:
                seconds = seconds_between(step.get("started_at"), step.get("completed_at"))
                if seconds is None:
                    continue
                phases[step_phase(step.get("name"), job.get("name"))] += seconds
                measured = True
        main = main_job(timeline)
        queue = seconds_between(main.get("created_at"), main.get("started_at")) if main else None
        if queue is not None:
            phases["queue"] = queue
        created = [parse_time(job.get("created_at")) for job in timeline]
        completed = [parse_time(job.get("completed_at")) for job in timeline]
        created = [moment for moment in created if moment]
        completed = [moment for moment in completed if moment]
        total = (max(completed) - min(created)).total_seconds() if created and completed else None
        return {
            "phases": phases,
            "queue_s": queue,
            "total_s": max(0.0, total) if total is not None else None,
            "measured": measured,
            "source": "timeline",
        }
    for step in steps or []:
        seconds = step.get("duration_s")
        if not isinstance(seconds, (int, float)):
            continue
        phases[step_phase(step.get("name"))] += float(seconds)
        measured = True
    return {"phases": phases, "queue_s": None, "total_s": None, "measured": measured, "source": "steps"}
