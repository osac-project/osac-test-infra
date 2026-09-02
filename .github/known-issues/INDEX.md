# Known CI issues / flakes

Curated, human-reviewed patterns the AI diagnostic checks against before
inventing a fresh root cause. Every file here (including this index) is
loaded directly into the diagnosis prompt in full -- there is no
retrieval/tool-call step, so keep entries short and keep the total corpus
small (a few dozen entries, each a couple hundred words, at most).

Add an entry (and its own `.md` file in this directory) whenever a
diagnosis reveals a recurring, previously-undocumented failure mode worth
teaching future runs about. Entries are added via normal PR review, like
any other change to this repo -- the diagnostic script never writes to
this directory itself. That's deliberate: a wrong high-confidence
diagnosis must never become a permanent, self-reinforcing "known issue"
that poisons every future run.

- [gate-blocks-full-install](gate-blocks-full-install.md) -- an early gate
  job (e.g. e2e-readiness) failed and skipped the real
  e2e-*-full-install job entirely; there is no cluster/test evidence to
  diagnose, and the real fix is unblocking the gate, not a code change.
