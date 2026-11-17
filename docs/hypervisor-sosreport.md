# Private hypervisor SOS diagnostics

Full-install VMaaS, BMaaS, CaaS and full-regression jobs collect a bounded SOS
report on their self-hosted libvirt host before cluster log gathering and
destructive teardown. This is additional diagnostic evidence, not a root-cause
fix. Collection does not need a kubeconfig. Failures withhold this report without
changing the test result or preventing teardown. A cancellation retry reuses a
completed candidate from the same job/attempt; an interrupted candidate is removed.
GitHub's cancellation deadline can prevent collection or publication entirely.

## Host prerequisite and rollout

The existing RHEL/CentOS/Fedora machine-init and fleet `machine_base` package
steps now install `sos`.
This integration supports the **4.10.x** interface and checks both the installed
version and mandatory options. Distribution package availability and the actual
fleet-installed version are not established by this change. An older or otherwise
unsupported package withholds the report, never drops masking flags. Operators
must supply a supported distribution package before enabling this on that host.

The approved host setup must install `scripts/osac-hypervisor-sosreport` as
`root:root`, mode `0755`, at `/usr/local/sbin/osac-hypervisor-sosreport` and regenerate
the machine-init runner-user sudoers policy. Only the no-argument helper is
authorized; neither a checkout script, arbitrary SOS options nor arbitrary output
paths may run as root. The fleet role's native helper-copy list installs it
before the single-file machine-init runner-user script regenerates sudoers.
Use the existing `fleet/playbooks/update.yml` `packages,runner-user` tags through
the documented operator dry-run, canary and widening procedure. This change
does not execute that rollout or establish fleet package compatibility.

The host needs systemd transient services and tmpfs/cgroup support. The helper
uses private temporary mounts (384 MiB `/var/tmp`, 32 MiB `/tmp`), a 1 GiB memory
limit, 128 tasks and a 140-second service lifetime plus a 5-second stop deadline.
A fixed root-owned lock permits only one host collection at a time; a busy host
withholds the additional report instead of queuing work.
The SOS subprocess has a 100-second deadline, 512 KiB captured-output limit,
1 MiB log and journal selections, 15-second plugin and 10-second command timeouts, two threads,
and an empty private configuration/mapping. Environment and working directory
are controlled, `--no-update` prevents persistent mapping updates, and `--build`
prevents SOS's vendor upload path. `--log-size` explicitly excludes journals in
SOS 4.10; the separate supported `--journal-size=1` bounds those selections.
Multiple selections can still exceed the report budget; oversized reports are
withheld, not truncated into supposedly complete diagnostics. Process groups
are signaled before reaping their leader, so its PID cannot be reused meanwhile.
The transient service requests an IP-address deny policy without replacing the
host network namespace. SOS sees host files and local libvirt, but private
`/tmp` and `/var/tmp` mounts hide the host's temporary trees. Systemd can warn
instead of enforcing unsupported cgroup/BPF controls: operators must verify
effective runtime, memory, mount and network controls on rollout. These controls
have not been exercised on a fleet host by the synthetic tests. The caller bounds
the transport file to 40 MiB and waits at most 155 seconds before termination.

## Masking evidence and report format

The implementation follows upstream [SOS 4.10.0 report source](https://github.com/sosreport/sos/blob/4.10.0/sos/report/__init__.py),
[cleaner source](https://github.com/sosreport/sos/blob/4.10.0/sos/cleaner/__init__.py),
[common options](https://github.com/sosreport/sos/blob/4.10.0/sos/__init__.py), and
[result selection](https://github.com/sosreport/sos/blob/4.10.0/sos/policies/__init__.py).

- `--no-env-vars` and `--cleaner` are mandatory, with `--batch --no-update --build`.
- `SoSCleaner.execute` emits `Successfully obfuscated 1 report(s)` after completing
  the report loop. `SoSReport.final_work` catches cleaner exceptions and can still
  produce raw output, so exit zero, a report directory or a private map alone
  are deliberately insufficient.
- Require that success message, no cleaner-tagged error/failure or report-level
  obfuscation exception, and the
  subsequent `Your sos report build tree has been generated in:` result selecting
  exactly one real immediate child of the private scratch directory. Reject SOS
  internal cleaner log errors too; both upstream `sos.log` and `ui.log` must be
  present, bounded regular files. Benign plugin/host diagnostic failures do not
  invalidate otherwise confirmed masking. Output is captured privately, not printed. Quiet mode
  is not used because positive cleaner evidence must remain observable privately.
- The cleaner's `<obfuscated-name>-private_map` is outside that selected tree.
  Mapping files, `sos_logs`, symlinks and special files are never transported.
  Limit the selected regular-file tree to 4096 files, 8 MiB per file and 32 MiB total.

The candidate is a private uncompressed tar outside `osac-artifacts`, bound to the
destination, run, attempt and job. Before the ordinary release gate deletes its
Secret inventory, a private copy is retained for this candidate. Only the existing
private publisher can consume it. A completed host candidate can start an empty
private handoff even if cluster collection had no kubeconfig or its ordinary
artifacts were withheld. No ordinary files or synthetic redaction markers are
introduced: the independent host candidate must pass its own gates below.
An empty handoff after those gates is not dispatched.
Using its existing repository-scoped app token,
the publisher verifies live repository metadata reports `private=true` and the
exact requested repository; redirects and metadata/authentication errors fail
private publication rather than silently suppressing baseline security failures.
An optional SOS inspection/scanner failure still permits ordinary, already-gated
artifacts to dispatch. SOS additionally requires the exact
configured destination and matching
collection binding. Ordinary public uploads never read or include the candidate.

The publisher safely unpacks regular files without links/traversal/duplicates,
runs the existing `release_gate.py --fail-on-find` including known-secret inputs
and the retained Secret inventory, then the same pinned gitleaks image/config
with a 256 MiB memory limit, 64 tasks and a 40 MiB output-file limit. Both scanners
must succeed with no findings. This report format disallows archived leaf files:
the existing release gate's archive signatures and the standard-library tar
header validator reject them before scanning, rather than relying on incomplete
nested-archive traversal. Such content withholds the whole report. The release
gate's existing nested/uninspectable/oversize rules are otherwise unchanged;
any withheld member withholds the whole SOS report. Scanner output stays private
or is discarded, never copied to artifacts or printed. Container cleanup runs
even on timeout. Only then is the cleaned `hypervisor-sosreport/` tree moved into
the existing private handoff directory, never the ordinary artifact tree. The
receiving contract remains `host_label`, `src_dir` and existing provenance inputs;
the receiver packages that directory as before. Do not wrap SOS in another archive:
the deployed postrun scanner's default archive depth does not inspect it. The
tree keeps every member visible to the unchanged trusted-main scanner/config.
Reports remain
sensitive/private: these flags and scanners do not guarantee removal of all secrets.

Missing credentials, public/unknown/wrong destination, mismatched bindings,
timeouts, stale attempts, masking/scanner failure or unsupported content withhold
SOS. Candidate cleanup runs after either upload route; root scratch disappears
with the bounded service, including cancellation. The existing private handoff's
day-old staging cleanup still applies if dispatch or the receiving job fails.
Netris/public-only gather callers keep their current routing and collect no SOS.

## Local checks

Only synthetic unprivileged fixtures are used. Do not run the privileged helper
or collect a real report to validate this change.

```sh
python3 -I .github/scripts/hypervisor_sosreport_test.py
python3 -I .github/scripts/hypervisor_sosreport_consumer_test.py
python3 -I .github/scripts/upload_artifacts_test.py
python3 -I .github/scripts/release_gate_test.py
python3 -I .github/scripts/release_gate_canary_test.py
pre-commit run --files <changed-files>
```

The consumer test uses the real digest-pinned gitleaks container, generated
synthetic report trees and the unmodified postrun Bash; only GitHub metadata,
artifact transfer and purge are mocked. `SOS_TRUSTED_MAIN=/path/to/frozen/checkout`
also tests that exact public default-branch dependency closure. No real API
writes or host reports are needed. A pin/config binding regression guards drift.
The script tests are part of `.github/workflows/ci.yml`; no E2E suite is added here.
Host sandbox/package integration and the private receiving workflow still need
operator-approved rollout verification, not live execution during development.
