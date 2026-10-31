#!/usr/bin/env python3
"""Regression tests for release_gate.py (run: python3 .github/scripts/release_gate_test.py)."""
from __future__ import annotations

import base64
import contextlib
import gzip
import io
import bz2
import json
import lzma
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import tracemalloc
import zipfile

# Import sibling module without requiring package install.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import release_gate as gate  # noqa: E402

# Synthetic values; none of these is a real credential.
USER = "robot+ci_access_0123456789abcdef"
TOKEN = "FAKEtokenFAKEtoken0123456789abcdefFAKEtokenFAKEtoken0123456789"
AUTH = base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()
PULL_SECRET = {
    "auths": {
        "quay.io": {"auth": AUTH, "email": "infrastructure-operator@example.com"},
        "registry.example.io": {"auth": "cGxhY2U=", "email": "infrastructure-operator@example.com"},
    }
}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _run(root: pathlib.Path, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = gate.main(["release_gate", str(root), *extra])
    return code, out.getvalue(), err.getvalue()


class Fixture:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._tmp.name)
        self.root = base / "artifact"
        self.root.mkdir()
        self.secret = base / "pull-secret.json"
        self.secret.write_text(json.dumps(PULL_SECRET))

    def __enter__(self) -> "Fixture":
        return self

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()

    def write(self, name: str, content: str | bytes) -> pathlib.Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def gate(self, *extra: str) -> tuple[int, str, str]:
        return _run(self.root, "--secret-file", str(self.secret), *extra)

    def everything(self) -> bytes:
        return b"".join(p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file())


def _no_secret(blob: bytes) -> None:
    for value in (TOKEN, AUTH):
        _assert(value.encode() not in blob, "secret still present")


def test_incident_shape_is_censored_and_json_stays_valid() -> None:
    """spec.pullSecret as an escaped JSON string, and AAP extra_vars as a string."""
    with Fixture() as fx:
        orders = {"items": [{"spec": {"pullSecret": json.dumps(PULL_SECRET), "name": "keep-me"}}]}
        extra_vars = json.dumps({"osac_job_vars": {"resource": orders["items"][0]}})
        jobs = {"results": [{"id": 1, "status": "successful", "extra_vars": extra_vars}]}
        fx.write("clusterorders.json", json.dumps(orders))
        fx.write("jobs-page-1.json", json.dumps(jobs))
        code, _, _ = fx.gate()
        _assert(code == 0, "censoring must not fail the run by default")
        _no_secret(fx.everything())
        # Still parseable, and the unrelated data survived.
        cleaned = json.loads((fx.root / "clusterorders.json").read_text())
        _assert(cleaned["items"][0]["spec"]["name"] == "keep-me", "unrelated field lost")
        json.loads((fx.root / "jobs-page-1.json").read_text())


def test_shape_rules_catch_it_without_any_known_value() -> None:
    """The layer that would have caught the incident with no inventory at all."""
    with Fixture() as fx:
        fx.write("clusterorders.json", json.dumps({"spec": {"pullSecret": json.dumps(PULL_SECRET)}}))
        code, _, _ = _run(fx.root)
        _assert(code == 0, "run failed")
        _no_secret(fx.everything())
        report = json.loads((fx.root / gate.REPORT_NAME).read_text())
        _assert(any(c["rule"] == "shape:docker-auth" for c in report["censored"]), f"rule missing: {report}")


def test_token_inside_larger_base64() -> None:
    with Fixture() as fx:
        blobs = []
        for pad in range(3):
            blob = base64.b64encode(b"x" * pad + f"prefix:{TOKEN}:suffix".encode()).decode()
            blobs.append(blob)
            fx.write(f"blob{pad}.json", json.dumps({"data": blob, "keep": "yes"}))
        fx.gate()
        # The plain token is never in these files, so check that the encoded blob is gone.
        for pad, blob in enumerate(blobs):
            text = (fx.root / f"blob{pad}.json").read_text()
            _assert(blob not in text, f"encoded token survived at alignment {pad}")
            _assert(json.loads(text)["keep"] == "yes", "json broken")


def test_raw_token_and_nested_directory() -> None:
    with Fixture() as fx:
        fx.write("nested/deep/debug.log", f"login ok token={TOKEN}\nnext line\n")
        fx.gate()
        text = (fx.root / "nested/deep/debug.log").read_text()
        _assert(TOKEN not in text and "next line" in text, f"unexpected: {text!r}")
        _assert("[REDACTED:" in text, "no marker left behind")


def test_check_mode_changes_nothing_and_fails() -> None:
    with Fixture() as fx:
        path = fx.write("a.log", f"x {TOKEN} y")
        before = path.read_bytes()
        code, _, _ = fx.gate("--check")
        _assert(code == 1, "check mode must exit 1 on a find")
        _assert(path.read_bytes() == before, "check mode modified a file")
        _assert(not (fx.root / gate.REPORT_NAME).exists(), "check mode wrote a report")


def test_fail_on_find_censors_then_fails() -> None:
    with Fixture() as fx:
        fx.write("a.log", f"x {TOKEN} y")
        code, _, _ = fx.gate("--fail-on-find")
        _assert(code == 1, "--fail-on-find must exit 1")
        _no_secret(fx.everything())


def test_clean_artifact_untouched() -> None:
    with Fixture() as fx:
        path = fx.write("a.log", "nothing sensitive\n" * 3)
        before = path.read_bytes()
        code, _, _ = fx.gate("--fail-on-find")
        _assert(code == 0 and path.read_bytes() == before, "clean file was changed or failed")


def test_report_and_output_never_contain_the_secret() -> None:
    with Fixture() as fx:
        fx.write("a.log", f"{AUTH} {TOKEN} {USER}\n")
        _, out, err = fx.gate()
        report = (fx.root / gate.REPORT_NAME).read_text()
        for value in (TOKEN, AUTH):
            _assert(value not in out + err + report, "secret value was printed or reported")


def test_identity_fields_and_placeholders_do_not_match() -> None:
    with Fixture() as fx:
        path = fx.write("a.log", "contact infrastructure-operator@example.com cGxhY2U= quay.io\n")
        before = path.read_bytes()
        fx.gate()
        _assert(path.read_bytes() == before, "identity field or placeholder was censored")


def test_other_shapes() -> None:
    with Fixture() as fx:
        # Assembled so the repo's detect-private-key hook does not flag this fixture.
        label = "RSA " + "PRIVATE KEY"
        pem = f"-----BEGIN {label}-----\\nMIIEowIBAAKCAQEA\\nabc\\n-----END {label}-----"
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4eHh4eHh4eCJ9.c2lnbmF0dXJlc2lnbmF0dXJl"
        fx.write("shapes.json", json.dumps({
            "key": pem, "jwt": jwt, "password": "hunter2hunter2",
            "Authorization": "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
            "aws": "AKIAABCDEFGHIJKLMNOP", "ref": {"secret": "my-secret-name", "secretName": "fulfillment-db"},
        }))
        fx.gate()
        text = (fx.root / "shapes.json").read_text()
        for leaked in ("MIIEowIBAAKCAQEA", jwt, "hunter2hunter2", "abcdefghijklmnopqrstuvwxyz0123456789", "AKIAABCDEFGHIJKLMNOP"):
            _assert(leaked not in text, f"{leaked[:8]} survived")
        _assert("my-secret-name" in text and "fulfillment-db" in text, "Secret references must not be censored")
        json.loads(text)


def test_already_redacted_values_not_rewritten() -> None:
    with Fixture() as fx:
        path = fx.write("a.json", '{"password": "REDACTED", "token": "x"}')
        before = path.read_bytes()
        fx.gate()
        _assert(path.read_bytes() == before, "placeholder password was rewritten")


def test_archives() -> None:
    with Fixture() as fx:
        fx.write("clean.gz", gzip.compress(b"nothing here\n"))
        fx.write("dirty.gz", gzip.compress(f"leak {TOKEN}\n".encode()))
        fx.write("unknown.zst", b"\x28\xb5\x2f\xfd" + b"\0" * 32)
        code, _, _ = fx.gate()
        _assert((fx.root / "clean.gz").exists(), "clean archive removed")
        _assert(not (fx.root / "dirty.gz").exists(), "archive holding a secret was kept")
        _assert(not (fx.root / "unknown.zst").exists(), "uninspectable archive was kept")
        report = json.loads((fx.root / gate.REPORT_NAME).read_text())
        _assert({w["file"] for w in report["withheld"]} == {"dirty.gz", "unknown.zst"}, str(report))


def test_symlink_is_removed() -> None:
    with Fixture() as fx:
        outside = pathlib.Path(fx.secret)
        (fx.root / "link.txt").symlink_to(outside)
        fx.gate()
        _assert(not (fx.root / "link.txt").is_symlink(), "symlink kept")
        _assert(outside.exists(), "symlink target was touched")


def test_missing_secret_file_is_skipped_and_empty_one_is_an_error() -> None:
    with Fixture() as fx:
        fx.write("a.log", "x\n")
        code, out, _ = _run(fx.root, "--secret-file", str(fx.root.parent / "absent.json"))
        _assert(code == 0 and "not present" in out, f"unexpected: {code} {out}")
        empty = fx.root.parent / "empty.json"
        empty.write_text("{}")
        code, _, _ = _run(fx.root, "--secret-file", str(empty))
        _assert(code == 2, f"an inventory source with no values must fail closed, got {code}")


def test_env_and_k8s_secret_inventory() -> None:
    with Fixture() as fx:
        k8s_password = "Zx9!qL2mPa7vR4tY"
        document = {"items": [
            {"metadata": {"namespace": "ns", "name": "db"}, "data": {
                "password": base64.b64encode(k8s_password.encode()).decode(),
                "ca.crt": base64.b64encode(b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----").decode(),
                "short": base64.b64encode(b"admin").decode()}},
        ]}
        listing = fx.root.parent / "secrets.json"
        listing.write_text(json.dumps(document))
        fx.write("a.log", f"db password {k8s_password}; user admin; env=ENVSECRET-0123456789abcdef\n")
        import os
        os.environ["GATE_TEST_SECRET"] = "ENVSECRET-0123456789abcdef"
        try:
            _run(fx.root, "--k8s-secrets", str(listing), "--env", "GATE_TEST_SECRET")
        finally:
            del os.environ["GATE_TEST_SECRET"]
        text = (fx.root / "a.log").read_text()
        _assert(k8s_password not in text and "ENVSECRET" not in text, f"inventory value survived: {text!r}")
        _assert("user admin" in text, "a short, low-entropy Secret value must not be censored")


def test_symlink_to_directory_is_removed_and_target_left_alone() -> None:
    with Fixture() as fx:
        outside = fx.root.parent / "outside"
        outside.mkdir()
        victim = outside / "other-job.log"
        victim.write_text(f"not ours {TOKEN}\n")
        (fx.root / "linked").symlink_to(outside, target_is_directory=True)
        fx.gate()
        _assert(not (fx.root / "linked").is_symlink(), "directory symlink was published")
        _assert(victim.read_text() == f"not ours {TOKEN}\n", "a file outside the artifact was rewritten")
        report = json.loads((fx.root / gate.REPORT_NAME).read_text())
        _assert(any(w["file"] == "linked" for w in report["withheld"]), f"not reported: {report}")


def test_nested_archives_are_withheld() -> None:
    with Fixture() as fx:
        inner = gzip.compress(f"leak {TOKEN}\n".encode())
        fx.write("gz-in-gz.gz", gzip.compress(inner))
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("plain.txt", "fine")
            zf.writestr("hidden.gz", inner)
        fx.write("zip-with-gz.zip", archive.getvalue())
        tar_bytes = io.BytesIO()
        with tarfile.open(fileobj=tar_bytes, mode="w") as tf:
            info = tarfile.TarInfo("hidden.gz")
            info.size = len(inner)
            tf.addfile(info, io.BytesIO(inner))
        fx.write("tar-with-gz.tar.gz", gzip.compress(tar_bytes.getvalue()))
        fx.write("single-level.gz", gzip.compress(b"nothing here\n"))
        fx.gate()
        for name in ("gz-in-gz.gz", "zip-with-gz.zip", "tar-with-gz.tar.gz"):
            _assert(not (fx.root / name).exists(), f"{name}: nested archive was published")
        _assert((fx.root / "single-level.gz").exists(), "a clean single-level archive must stay")


def test_a_crash_exits_2_never_1() -> None:
    with Fixture() as fx:
        fx.write("a.log", "x\n")
        original = gate.process_directory
        gate.process_directory = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = gate.run_cli(["release_gate", str(fx.root)])
        finally:
            gate.process_directory = original
        _assert(code == 2, f"a crash must exit 2 (1 means found), got {code}")
        _assert("RuntimeError" in err.getvalue() and "boom" not in err.getvalue(), "report the class, not the message")


def test_non_utf8_environment_variable_does_not_crash() -> None:
    with Fixture() as fx:
        raw = b"prefix-\xff\xfe-0123456789abcdef"
        os.environb[b"GATE_TEST_BINARY"] = raw
        try:
            fx.write("a.log", b"before " + raw + b" after\n")
            code, _, _ = _run(fx.root, "--env", "GATE_TEST_BINARY")
        finally:
            del os.environb[b"GATE_TEST_BINARY"]
        _assert(code == 0, f"gate crashed on a non-UTF-8 value: {code}")
        _assert(raw not in (fx.root / "a.log").read_bytes(), "undecodable secret survived")


def test_non_ascii_secret_with_an_ascii_locale() -> None:
    """The file encodings must not depend on the runner's locale."""
    with Fixture() as fx:
        value = "P\u00e4ssw\u00f6rd-0123456789abcdef"
        fx.secret.write_text(json.dumps({"password": value}, ensure_ascii=False), encoding="utf-8")
        fx.write("a.log", f"login {value}\n")
        env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
        env.pop("PYTHONIOENCODING", None)
        # No -I: isolated mode ignores the PYTHON* variables that force the ASCII locale.
        done = subprocess.run([sys.executable, "-X", "utf8=0", str(pathlib.Path(gate.__file__)), str(fx.root),
                               "--secret-file", str(fx.secret)], env=env, capture_output=True, text=True)
        _assert(done.returncode == 0, f"gate failed under an ASCII locale: {done.stderr[-300:]}")
        _assert(value.encode() not in (fx.root / "a.log").read_bytes(), "non-ASCII secret survived")


def test_zip_entries_with_the_same_name_are_all_inspected() -> None:
    import warnings
    with Fixture() as fx:
        archive = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # zipfile warns about the duplicate on purpose
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("log.txt", f"first {TOKEN}")
                zf.writestr("log.txt", "second, harmless")
        fx.write("dup.zip", archive.getvalue())
        fx.gate()
        _assert(not (fx.root / "dup.zip").exists(), "the first of two same-named entries was never inspected")


def test_a_file_named_like_the_report_is_still_scanned() -> None:
    with Fixture() as fx:
        planted = fx.write(gate.REPORT_NAME, f"hidden {TOKEN}\n")
        elsewhere = fx.root.parent / "report-elsewhere.json"
        code, _, _ = _run(fx.root, "--secret-file", str(fx.secret), "--report", str(elsewhere))
        _assert(code == 0, "run failed")
        _assert(TOKEN.encode() not in planted.read_bytes(), "a file named like the report skipped the scan")


def test_a_secret_file_that_is_not_utf8_is_an_error() -> None:
    with Fixture() as fx:
        fx.write("a.log", "x\n")
        broken = fx.root.parent / "broken.txt"
        broken.write_bytes(b"prefix \xff\xfe " + TOKEN.encode())
        code, _, err = _run(fx.root, "--secret-file", str(broken))
        _assert(code == 2, f"an undecodable secret file must fail closed (exit 2), got {code}")
        _assert(TOKEN not in err, "the value was printed")


def test_decompression_bombs_are_abandoned_at_the_limit() -> None:
    """A small archive that expands hugely must not be inflated in memory first."""
    zeros = bytes(64 * 1024 * 1024)
    members = io.BytesIO()
    with zipfile.ZipFile(members, "w", zipfile.ZIP_DEFLATED) as zf:
        for index in range(20):  # each member is under the limit, together they are far over it
            zf.writestr(f"part{index}.txt", bytes(3 * 1024 * 1024))
    single = io.BytesIO()
    with zipfile.ZipFile(single, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.txt", zeros)
    samples = {"gzip.gz": gzip.compress(zeros), "bz2.bz2": bz2.compress(zeros), "xz.xz": lzma.compress(zeros),
               "one-member.zip": single.getvalue(), "many-members.zip": members.getvalue()}
    bomb_methods = [("bzip2", zipfile.ZIP_BZIP2), ("lzma", zipfile.ZIP_LZMA)]
    if hasattr(zipfile, "ZIP_ZSTANDARD"):
        bomb_methods.append(("zstd", zipfile.ZIP_ZSTANDARD))
    for label, method in bomb_methods:
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w", method) as zf:
            zf.writestr("big.txt", zeros)
        samples[f"{label}-member.zip"] = packed.getvalue()
    original = gate.MAX_FILE_BYTES
    try:
        for name, blob in samples.items():
            with Fixture() as fx:
                fx.write(name, blob)
                tracemalloc.start()
                try:
                    code, _, _ = fx.gate("--max-file-mbytes", "4")
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                _assert(code == 0, f"{name}: gate failed instead of withholding the archive")
                _assert(gate.MAX_FILE_BYTES == 4 * 1024 * 1024, "CLI did not apply the 4 MiB test limit")
                _assert(not (fx.root / name).exists(), f"{name}: oversized archive was published")
                _assert(peak < 24 * 1024 * 1024, f"{name}: inflated {peak >> 20} MiB before giving up")
    finally:
        gate.MAX_FILE_BYTES = original


def test_size_limit_cli_default_and_overrides() -> None:
    original = gate.MAX_FILE_BYTES
    cases = [(("--max-file-mbytes", "4"), 4), (("--max-file-mbytes", "2048"), 2048), ((), 512)]
    try:
        for arguments, expected in cases:
            with Fixture() as fx:
                fx.write("a.log", "ordinary log output\n")
                code, _, _ = fx.gate(*arguments)
                _assert(code == 0, "size limit configuration failed")
                _assert(gate.MAX_FILE_BYTES == expected * 1024 * 1024, "CLI size limit was not applied")
    finally:
        gate.MAX_FILE_BYTES = original


def test_zip_entries_with_unbounded_methods_are_refused() -> None:
    """Even a small, harmless entry: the interpreter's limit on these methods is not guaranteed."""
    with Fixture() as fx:
        methods = [("bzip2", zipfile.ZIP_BZIP2), ("lzma", zipfile.ZIP_LZMA)]
        if hasattr(zipfile, "ZIP_ZSTANDARD"):  # Python 3.14 and later
            methods.append(("zstd", zipfile.ZIP_ZSTANDARD))
        allowed = [("deflate", zipfile.ZIP_DEFLATED), ("stored", zipfile.ZIP_STORED)]
        for label, method in methods + allowed:
            packed = io.BytesIO()
            with zipfile.ZipFile(packed, "w", method) as zf:
                zf.writestr("note.txt", "nothing sensitive")
            fx.write(f"{label}.zip", packed.getvalue())
        # A method this module has never heard of: patch the id in both headers of a stored zip.
        odd = io.BytesIO()
        with zipfile.ZipFile(odd, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("note.txt", "nothing sensitive")
        raw = odd.getvalue().replace(b"PK\x03\x04\x14\x00\x00\x00\x00\x00", b"PK\x03\x04\x14\x00\x00\x00\x63\x00") \
                           .replace(b"PK\x01\x02\x14\x03\x14\x00\x00\x00\x00\x00", b"PK\x01\x02\x14\x03\x14\x00\x00\x00\x63\x00")
        fx.write("unknown-method.zip", raw)
        fx.gate()
        for label, _ in methods:
            _assert(not (fx.root / f"{label}.zip").exists(), f"{label} zip entry was accepted")
        _assert(not (fx.root / "unknown-method.zip").exists(), "an unknown zip method was accepted")
        for label, _ in allowed:
            _assert((fx.root / f"{label}.zip").exists(), f"a harmless {label} zip must stay")


def main() -> None:
    test_incident_shape_is_censored_and_json_stays_valid()
    test_shape_rules_catch_it_without_any_known_value()
    test_token_inside_larger_base64()
    test_raw_token_and_nested_directory()
    test_check_mode_changes_nothing_and_fails()
    test_fail_on_find_censors_then_fails()
    test_clean_artifact_untouched()
    test_report_and_output_never_contain_the_secret()
    test_identity_fields_and_placeholders_do_not_match()
    test_other_shapes()
    test_already_redacted_values_not_rewritten()
    test_archives()
    test_symlink_is_removed()
    test_missing_secret_file_is_skipped_and_empty_one_is_an_error()
    test_env_and_k8s_secret_inventory()
    test_symlink_to_directory_is_removed_and_target_left_alone()
    test_nested_archives_are_withheld()
    test_a_crash_exits_2_never_1()
    test_non_utf8_environment_variable_does_not_crash()
    test_non_ascii_secret_with_an_ascii_locale()
    test_zip_entries_with_the_same_name_are_all_inspected()
    test_a_file_named_like_the_report_is_still_scanned()
    test_a_secret_file_that_is_not_utf8_is_an_error()
    test_decompression_bombs_are_abandoned_at_the_limit()
    test_size_limit_cli_default_and_overrides()
    test_zip_entries_with_unbounded_methods_are_refused()
    print("release_gate_test.py: ok")


if __name__ == "__main__":
    main()
