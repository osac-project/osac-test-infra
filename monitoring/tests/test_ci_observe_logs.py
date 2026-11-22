"""The artifact-log rollup (ci_observe_logs.py): failed runs' error lines into
Loki with few stream labels and the run as structured metadata, every run's
signatures into trends.db, at the latest attempt, with a refusal stopping
the pass and a run's own problem recorded as the run's fact."""

from __future__ import annotations

import importlib.util
import io
import sqlite3
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


logs = _load("ci_observe_logs")

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def bundle(files):
    """A zip of {path: text}."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    data.seek(0)
    return zipfile.ZipFile(data)


def run(run_id="501", outcome="failure", attempt="1", created=NOW - timedelta(hours=2),
        finished=NOW - timedelta(hours=1), suite="vmaas"):
    return {"run_id": run_id, "repo": "osac", "trigger": "scheduled", "workflow": "E2E VMaaS Full Install",
            "failed_step": "e2e → Install OSAC" if outcome == "failure" else "", "suite": suite,
            "conclusion": outcome, "outcome": outcome, "attempt": attempt, "created_at": created,
            "finished_at": finished}


INSTALL_LOG = "\n".join([
    "2026-11-20T10:05:00Z TASK [cert-manager] fatal: cert-manager cannot get lease id=4f2a9c",
    "2026-11-20T10:05:01Z ok: [host-1]",
])
POD_LOG = "E1120 10:06:00.000000 1 leader.go:42] error retrieving lock kube-system/cert-manager: forbidden\n"
SUMMARY = "=== failure summary ===\ninstall failed at the cert-manager step\n"


def test_a_failed_run_is_pushed_with_few_labels_and_its_run_as_metadata():
    archive = bundle({"aap-jobs/install.log": INSTALL_LOG, "namespaces/cm/pods/cm.log": POD_LOG,
                      "failure-summary.txt": SUMMARY})
    streams, signatures, counts = logs.read_bundle(archive, run(), NOW)
    labels = {key for group in streams for key, _ in group}
    # One stream per repository, suite, kind and stamp-day: the run and the
    # signature would make one per run and signature, and a stream shared
    # by every run of a suite would lose backfill entries older than
    # Loki's out-of-order window to a 400.
    assert labels == {"source", "repo", "suite", "kind", "bucket"}
    by_kind = {dict(group)["kind"]: values for group, values in streams.items()}
    assert set(by_kind) == {"error", "summary"}
    errors = by_kind["error"]
    assert {value[2]["run_id"] for value in errors} == {"501"}
    assert all(value[2]["attempt"] == "1" and value[2]["signature"] for value in errors)
    assert all("signature" not in value[2] for value in by_kind["summary"])
    assert counts["error_lines"] == 3 and counts["files"] == 3
    assert {signature for signature, _ in signatures} == {value[2]["signature"] for value in errors}


def test_lines_of_different_days_land_in_different_streams():
    """A stream's entries must stay within Loki's out-of-order window
    (max_chunk_age/2, 84 h here) of its newest entry: a backfill works
    through the backlog newest first, so lines days apart must not share
    a stream. The bucket is the stamp's own UTC day, so a run that spans
    midnight splits its lines across two streams."""
    across = run(created=datetime(2026, 11, 19, 23, 30, tzinfo=UTC),
                 finished=datetime(2026, 11, 20, 0, 30, tzinfo=UTC))
    lines = "\n".join([
        "2026-11-19T23:45:00Z error: before midnight",
        "2026-11-20T00:15:00Z error: after midnight",
    ])
    streams, _, _ = logs.read_bundle(bundle({"x.log": lines}), across, NOW)
    buckets = {dict(group)["bucket"]: [value[0] for value in values] for group, values in streams.items()}
    assert set(buckets) == {"2026-11-19", "2026-11-20"}
    for stamps in buckets.values():
        assert (max(stamps) - min(stamps)) / 1e9 <= 86400, "one stream spans more than a day"


def test_runs_days_apart_never_share_a_stream():
    first = logs.read_bundle(bundle({"x.log": "2026-11-19T10:00:00Z error: newer run\n"}),
                             run(run_id="1", created=datetime(2026, 11, 19, 9, tzinfo=UTC),
                                 finished=datetime(2026, 11, 19, 11, tzinfo=UTC)), NOW)
    second = logs.read_bundle(bundle({"x.log": "2026-11-16T10:00:00Z error: older run\n"}),
                              run(run_id="2", created=datetime(2026, 11, 16, 9, tzinfo=UTC),
                                  finished=datetime(2026, 11, 16, 11, tzinfo=UTC)), NOW)
    streams = {**first[0], **second[0]}
    assert len(streams) == 2
    assert {dict(group)["bucket"] for group in streams} == {"2026-11-19", "2026-11-16"}


def test_a_passing_run_pushes_nothing_and_still_counts_its_signatures():
    archive = bundle({"aap-jobs/install.log": INSTALL_LOG})
    streams, signatures, counts = logs.read_bundle(archive, run(outcome="success"), NOW)
    assert streams == {}
    assert len(signatures) == 1


def test_a_line_stamped_outside_the_run_takes_the_run_end():
    lines = "\n".join([
        f"{iso(NOW - timedelta(hours=1, minutes=30))} error: inside the run",
        f"{iso(NOW + timedelta(hours=3))} error: a skewed clock",
        "2026-10-01T08:00:00Z error: a date quoted from long ago",
        "error: no stamp at all",
    ])
    streams, _, _ = logs.read_bundle(bundle({"x.log": lines}), run(), NOW)
    stamps = {value[1].split("error: ")[1]: value[0] for values in streams.values() for value in values}
    end = int((NOW - timedelta(hours=1)).timestamp() * 1e9)
    assert stamps["inside the run"] == int((NOW - timedelta(hours=1, minutes=30)).timestamp() * 1e9)
    assert stamps["a skewed clock"] == end
    assert stamps["a date quoted from long ago"] == end
    assert stamps["no stamp at all"] == end


def test_a_run_that_just_ended_never_stamps_a_line_in_the_future():
    just = run(finished=NOW - timedelta(minutes=5))
    lines = f"{iso(NOW + timedelta(minutes=30))} error: ahead of the clock\n"
    streams, _, _ = logs.read_bundle(bundle({"x.log": lines}), just, NOW)
    [values] = streams.values()
    assert values[0][0] <= int(NOW.timestamp() * 1e9)


def test_a_run_ended_ahead_of_the_local_clock_clamps_its_unstamped_lines():
    """The fallback stamp is the run's end, held to `latest` (now): a run
    whose stored end is ahead of the local clock must not stamp its
    unstamped lines into the future, where the run-page query cannot see
    them."""
    ahead = run(finished=NOW + timedelta(minutes=90))
    streams, _, _ = logs.read_bundle(bundle({"x.log": "error: no stamp\n"}), ahead, NOW)
    [values] = streams.values()
    assert values[0][0] == int(NOW.timestamp() * 1e9)


def test_oversized_files_and_lines_are_skipped_and_counted():
    archive = bundle({"big.log": "error: x\n" * (logs.MAX_FILE_BYTES // 9 + 10),
                      "wide.log": "error: " + "y" * (logs.MAX_LINE_BYTES + 1) + "\nerror: short\n"})
    streams, _, counts = logs.read_bundle(archive, run(), NOW)
    assert counts["skipped_files"] == 1 and counts["skipped_lines"] == 1
    assert [value[1] for values in streams.values() for value in values] == ["error: short"]


class Response:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class Session:
    """Loki: answers the pushes from `answers` (204 once they run out)."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.pushes = []
        self.headers = {}

    def post(self, url, json=None, timeout=None, **kwargs):
        if url.endswith("/flush"):
            return Response(204)
        self.pushes.append((url, json, kwargs))
        answer = self.answers.pop(0) if self.answers else Response(204)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_a_push_carries_structured_metadata_in_batches():
    session = Session()
    values = [(i, f"line {i}", {"run_id": "501", "attempt": "1", "signature": "s"}) for i in range(1, 502)]
    logs.push_streams({(("kind", "error"), ("repo", "osac"), ("source", "artifact"), ("suite", "vmaas")): values},
                      session)
    assert len(session.pushes) == 2  # 500 a push
    stream = session.pushes[0][1]["streams"][0]
    assert stream["stream"] == {"kind": "error", "repo": "osac", "source": "artifact", "suite": "vmaas"}
    assert stream["values"][0] == ["1", "line 1", {"run_id": "501", "attempt": "1", "signature": "s"}]


@pytest.mark.parametrize(("answer", "error"), [
    (Response(400, "entry too far behind"), "LokiRejected"),
    (Response(503, "ingester down"), "LokiRefused"),
    (Response(429, "rate limited"), "LokiRefused"),
    (logs.requests.exceptions.ConnectionError("refused"), "LokiRefused"),
])
def test_loki_answers_are_told_apart(answer, error):
    with pytest.raises(getattr(logs, error)):
        logs.push_streams({(("source", "artifact"),): [(1, "x", {})]}, Session([answer]))


# -- the pass -----------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The exporter's job history, an empty trends.db, GitHub's bundles and Loki."""
    jobs = tmp_path / "workflow-exporter.db"
    with sqlite3.connect(jobs) as conn:
        conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, run_id TEXT, repo TEXT, workflow TEXT, event TEXT,"
                     " trigger TEXT, failed_step TEXT, conclusion TEXT, created_at TEXT, updated_at TEXT,"
                     " run_attempt INTEGER, category TEXT)")
    monkeypatch.setattr(logs, "EXPORTER_DB", str(jobs))
    monkeypatch.setattr(logs, "TRENDS_DB", str(tmp_path / "trends.db"))
    monkeypatch.setattr(logs, "TOKEN", "ghp_the_token")

    class World:
        bundles = {}
        session = Session()
        looked_up = []

        @staticmethod
        def run(job_id, run_id, conclusion, attempt=1, ended_hours_ago=1, workflow="E2E VMaaS Full Install"):
            ended = NOW - timedelta(hours=ended_hours_ago)
            with sqlite3.connect(jobs) as conn:
                conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?, 'e2e')",
                             (job_id, run_id, "osac", workflow, "schedule", "scheduled",
                              "e2e → Install OSAC" if conclusion == "failure" else "", conclusion,
                              iso(ended - timedelta(hours=1)), iso(ended), attempt))

        @staticmethod
        def state():
            with sqlite3.connect(tmp_path / "trends.db") as conn:
                conn.row_factory = sqlite3.Row
                return {(row["run_id"], row["suite"]): dict(row) for row in conn.execute("SELECT * FROM artifact_logs")}

        @staticmethod
        def signatures():
            with sqlite3.connect(tmp_path / "trends.db") as conn:
                return sorted(conn.execute("SELECT run_id, outcome, signature FROM artifact_log_signatures"))

        @staticmethod
        def passes():
            with sqlite3.connect(tmp_path / "trends.db") as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if "rollup_passes" not in tables:
                    return {}
                return dict(conn.execute("SELECT job, summary FROM rollup_passes"))

    world = World()
    # The shared GitHub budget each lookup's answer states (None: GitHub
    # sends no count), and the lookup from which GitHub says it is spent.
    world.remaining = None
    world.spent_from = None

    def list_bundle(run, headers, budget=None):
        if world.spent_from is not None and len(world.looked_up) >= world.spent_from:
            raise logs.GitHubRateLimited("osac-project/osac-ci-artifacts answered HTTP 403 to the artifact"
                                         " listing: the token's GitHub API rate limit is spent")
        world.looked_up.append((run["run_id"], run["attempt"]))
        assert headers["Authorization"] == "Bearer ghp_the_token"
        if budget is not None and world.remaining is not None:
            budget.observe(type("Answer", (), {"headers": {"X-RateLimit-Remaining": str(world.remaining)}})())
            world.remaining -= 1
        found = world.bundles.get(run["run_id"])
        return None if found is None else {"suite": run["suite"], "artifact": found, "label": "", "private": True}

    monkeypatch.setattr(logs, "list_bundle", list_bundle)
    monkeypatch.setattr(logs, "download_bundle", lambda artifact, headers, budget=None: (bundle(artifact), ""))
    monkeypatch.setattr(logs.requests, "Session", lambda: world.session)
    fixed = type("FixedDatetime", (datetime,), {"now": staticmethod(lambda tz=None: NOW)})
    monkeypatch.setattr(logs, "datetime", fixed)
    return world


def test_a_pass_indexes_failed_and_passing_runs_and_records_itself(world):
    world.run(1, "501", "failure")
    world.run(2, "502", "success")
    world.bundles = {"501": {"aap-jobs/install.log": INSTALL_LOG}, "502": {"aap-jobs/install.log": INSTALL_LOG}}
    assert logs.main(["--days", "30"]) == 0
    state = world.state()
    assert {key: (row["state"], row["attempt"]) for key, row in state.items()} == {
        ("501", "vmaas"): ("indexed", "1"), ("502", "vmaas"): ("indexed", "1")}
    # Only the failed run's lines go to Loki; both runs' signatures are kept.
    pushed = {value[2]["run_id"] for _, payload, _ in world.session.pushes
              for stream in payload["streams"] for value in stream["values"]}
    assert pushed == {"501"}
    assert {(run_id, outcome) for run_id, outcome, _ in world.signatures()} == {("501", "failure"), ("502", "success")}
    assert "2 indexed" in world.passes()["logs"]


def test_the_loki_pushes_do_not_carry_the_github_token(world):
    world.run(1, "501", "failure")
    world.bundles = {"501": {"aap-jobs/install.log": INSTALL_LOG}}
    logs.main(["--days", "30"])
    assert world.session.pushes
    assert "ghp_the_token" not in repr(world.session.headers) + repr([p[2] for p in world.session.pushes])


def test_a_run_read_once_is_not_read_again_until_a_newer_attempt(world):
    world.run(1, "501", "failure")
    world.bundles = {"501": {"aap-jobs/install.log": INSTALL_LOG}}
    logs.main(["--days", "30"])
    logs.main(["--days", "30"])
    assert world.looked_up == [("501", "1")]
    # The re-run passed: its bundle is read and its signatures replace the failed attempt's.
    world.run(2, "501", "success", attempt=2)
    assert logs.main(["--days", "30"]) == 0
    assert world.looked_up == [("501", "1"), ("501", "2")]
    assert world.state()[("501", "vmaas")]["attempt"] == "2"
    assert {outcome for _, outcome, _ in world.signatures()} == {"success"}


def test_only_the_latest_attempt_of_a_suite_is_read(world):
    world.run(1, "601", "failure", attempt=1, ended_hours_ago=3, workflow="E2E vmaas (merge queue)")
    world.run(2, "601", "success", attempt=2, ended_hours_ago=1, workflow="E2E vmaas (merge queue)")
    world.bundles = {"601": {"aap-jobs/install.log": INSTALL_LOG}}
    logs.main(["--days", "30"])
    assert world.looked_up == [("601", "2")]


def test_failures_only_selects_the_latest_attempt_before_filtering(world):
    """A run whose re-run passed is not a failure: filtering the SQL first
    would keep its attempt-1 failure row, read the newest bundle under
    that old attempt, and count its signatures toward a failure that no
    longer happened."""
    world.run(1, "601", "failure", attempt=1, ended_hours_ago=3, workflow="E2E vmaas (merge queue)")
    world.run(2, "601", "success", attempt=2, ended_hours_ago=1, workflow="E2E vmaas (merge queue)")
    world.bundles = {"601": {"aap-jobs/install.log": INSTALL_LOG}}
    assert logs.main(["--days", "30", "--failures-only"]) == 0
    assert world.looked_up == []
    assert world.state() == {}


def test_a_missing_bundle_is_retried_while_young_then_unavailable(world):
    world.run(1, "701", "failure", ended_hours_ago=1)
    world.run(2, "702", "failure", ended_hours_ago=logs.MISSING_RETRY_HOURS + 2)
    assert logs.main(["--days", "30"]) == 0
    state = world.state()
    assert state[("701", "vmaas")]["state"] == "missing"
    assert state[("702", "vmaas")]["state"] == "unavailable"
    assert "hours after the run ended" in state[("702", "vmaas")]["reason"]
    world.looked_up.clear()
    logs.main(["--days", "30"])
    assert world.looked_up == [("701", "1")]  # the young one again, the unavailable one never


def test_a_run_loki_rejects_is_its_own_fact_and_the_pass_goes_on(world):
    world.run(1, "801", "failure", ended_hours_ago=1)
    world.run(2, "802", "failure", ended_hours_ago=2)
    world.bundles = {"801": {"x.log": "error: one\n"}, "802": {"x.log": "error: two\n"}}
    world.session.answers = [Response(400, "entry for stream has timestamp too new")]
    assert logs.main(["--days", "30"]) == 1  # visible
    state = world.state()
    assert state[("801", "vmaas")]["state"] == "rejected"
    assert "timestamp too new" in state[("801", "vmaas")]["reason"]
    assert state[("802", "vmaas")]["state"] == "indexed"
    assert "1 unreadable or rejected" in world.passes()["logs"]
    pushes = len(world.session.pushes)
    logs.main(["--days", "30"])
    assert len(world.session.pushes) == pushes  # the rejected run is not pushed twice


def test_a_refusal_stops_the_pass_and_records_no_pass(world):
    world.run(1, "901", "failure", ended_hours_ago=1)
    world.run(2, "902", "failure", ended_hours_ago=2)
    world.bundles = {"901": {"x.log": "error: one\n"}, "902": {"x.log": "error: two\n"}}
    world.session.answers = [Response(503, "ingester unavailable")]
    assert logs.main(["--days", "30"]) == 1
    assert world.state() == {}
    assert world.passes() == {}


def test_the_limit_counts_every_run_looked_up(world):
    for job_id in range(1, 6):
        world.run(job_id, str(1000 + job_id), "failure", ended_hours_ago=job_id)
    logs.main(["--days", "30", "--limit", "2"])  # every bundle is missing: still GitHub calls
    assert len(world.looked_up) == 2


def test_rows_from_before_the_attempt_column_read_as_the_first_attempt(world, tmp_path):
    with sqlite3.connect(tmp_path / "trends.db") as conn:
        conn.execute(
            "CREATE TABLE artifact_logs (run_id TEXT NOT NULL, suite TEXT NOT NULL, repo TEXT NOT NULL,"
            " state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', files INTEGER NOT NULL DEFAULT 0,"
            " lines INTEGER NOT NULL DEFAULT 0, error_lines INTEGER NOT NULL DEFAULT 0,"
            " skipped_files INTEGER NOT NULL DEFAULT 0, skipped_lines INTEGER NOT NULL DEFAULT 0,"
            " ingested_at TEXT NOT NULL, PRIMARY KEY (run_id, suite))")
        conn.execute("INSERT INTO artifact_logs (run_id, suite, repo, state, ingested_at)"
                     " VALUES ('501', 'vmaas', 'osac', 'indexed', '2026-11-19T00:00:00Z')")
    world.run(1, "501", "failure")
    logs.main(["--days", "30"])
    assert world.looked_up == []  # not re-read (its lines are already in Loki)


def test_a_run_loki_rejects_keeps_its_signatures(world):
    """The data-loss path of 2026-11-22's review: a rejected run's signatures
    are bundle facts, recorded whatever Loki said about its lines, and the
    ranking counts them. Before, they were lost for good: a rejected run is
    not read again at its attempt."""
    world.run(1, "801", "failure", ended_hours_ago=1)
    world.bundles = {"801": {"aap-jobs/install.log": INSTALL_LOG}}
    world.session.answers = [Response(400, "entry too far behind")]
    logs.main(["--days", "30"])
    assert world.state()[("801", "vmaas")]["state"] == "rejected"
    assert {run_id for run_id, _, _ in world.signatures()} == {"801"}


def test_a_spent_budget_ends_the_pass_with_what_it_read(world):
    """GitHub saying the budget is spent is neither the run's fact nor a
    fault: what was read is recorded, the pass is a pass, and the runs left
    are counted for the pages."""
    for job_id in range(1, 4):
        world.run(job_id, str(700 + job_id), "failure", ended_hours_ago=job_id)
    world.bundles = {str(700 + job_id): {"aap-jobs/install.log": INSTALL_LOG} for job_id in range(1, 4)}
    world.spent_from = 1
    assert logs.main(["--days", "30"]) == 0
    assert {key[0]: row["state"] for key, row in world.state().items()} == {"701": "indexed"}
    summary = world.passes()["logs"]
    assert "1 indexed" in summary and "2 left for the next passes" in summary
    assert "rate limit is spent" in summary
    with sqlite3.connect(logs.TRENDS_DB) as conn:
        assert conn.execute("SELECT pending FROM rollup_passes WHERE job = 'logs'").fetchone() == (2,)


def test_a_pass_the_spent_budget_let_read_nothing_is_not_a_pass(world):
    world.run(1, "701", "failure", ended_hours_ago=1)
    world.bundles = {"701": {"aap-jobs/install.log": INSTALL_LOG}}
    world.spent_from = 0
    assert logs.main(["--days", "30"]) == 1
    assert world.passes() == {}, "nothing it had to read was read: the store's freshness ages and alerts"


def test_the_lookups_stop_at_the_reserve_left_for_the_exporter(world, monkeypatch):
    monkeypatch.setattr(logs, "GITHUB_RESERVE", 1000)
    for job_id in range(1, 4):
        world.run(job_id, str(700 + job_id), "failure", ended_hours_ago=job_id)
    world.remaining = 1000  # the first answer leaves exactly the reserve, the second one below it
    logs.main(["--days", "30"])
    assert [run_id for run_id, _ in world.looked_up] == ["701", "702"]
    assert "below the 1000 this reader leaves for the exporter" in world.passes()["logs"]


def test_resign_recomputes_a_rejected_runs_signatures_and_keeps_its_state(world):
    """Runs rejected before 2026-11-22 have no signatures; --resign recomputes
    them from the bundle, pushes nothing, and the run stays rejected (its
    lines are in Loki only in part)."""
    world.run(1, "801", "failure", ended_hours_ago=1)
    world.bundles = {"801": {"aap-jobs/install.log": INSTALL_LOG}}
    world.session.answers = [Response(400, "entry too far behind")]
    logs.main(["--days", "30"])
    with sqlite3.connect(logs.TRENDS_DB) as conn:
        conn.execute("DELETE FROM artifact_log_signatures")
    pushes = len(world.session.pushes)
    assert logs.main(["--days", "30", "--resign"]) == 0
    assert len(world.session.pushes) == pushes, "a repair pushes nothing"
    assert {run_id for run_id, _, _ in world.signatures()} == {"801"}
    row = world.state()[("801", "vmaas")]
    assert (row["state"], row["reason"]) == (
        "rejected", "Loki answered HTTP 400 to a push of 1 lines: entry too far behind")


def test_a_stamp_with_an_offset_buckets_by_its_utc_day():
    """01:15+02:00 on the 20th is 23:15 UTC on the 19th: the bucket the
    module docstring promises, so a reader can find it by its UTC day."""
    late = run(created=datetime(2026, 11, 19, 22, tzinfo=UTC), finished=datetime(2026, 11, 20, 0, 30, tzinfo=UTC))
    streams, _, _ = logs.read_bundle(bundle({"x.log": "2026-11-20T01:15:00+02:00 error: offset stamp\n"}), late, NOW)
    [group] = streams
    assert dict(group)["bucket"] == "2026-11-19"


def test_failures_only_reads_newest_first_like_a_full_pass(world):
    world.run(1, "601", "failure", ended_hours_ago=5)
    world.run(2, "602", "failure", ended_hours_ago=1)
    world.run(3, "603", "success", ended_hours_ago=2)
    logs.main(["--days", "30", "--failures-only"])
    assert [run_id for run_id, _ in world.looked_up] == ["602", "601"]
