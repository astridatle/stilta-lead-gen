"""Deterministic behaviour tests -- plain assertions, NO LLM judge.

Covers system properties the LLM judge cannot verify:
  (a) idempotency / dedup : a second run on the same window creates 0 new drafts
      and does not grow the leads file.
  (b) no send capability  : nothing in the pipeline code path can send mail.
  (c) empty window        : an empty fetch fires the zero-leads alert.

All checks run the pipeline in --dry-run (no LLM, no network, dev-only outputs),
so this is fast, free, and safe to run anytime:

    python3 test_behaviour.py     # exits non-zero on any failure
"""

import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
DRYRUN_ARTIFACTS = ["leads.dryrun.jsonl", "seen_dryrun.json", "seen_dryrun.json.tmp",
                    "logs/last_run.dryrun.json"]


def _run(*args):
    return subprocess.run([PY, "run.py", *args], cwd=REPO, capture_output=True, text=True)


def _clean_dryrun():
    for p in DRYRUN_ARTIFACTS:
        try:
            os.remove(os.path.join(REPO, p))
        except FileNotFoundError:
            pass
    shutil.rmtree(os.path.join(REPO, "drafts_dryrun"), ignore_errors=True)


def _heartbeat():
    return json.load(open(os.path.join(REPO, "logs/last_run.dryrun.json")))


def _leads_lines():
    path = os.path.join(REPO, "leads.dryrun.jsonl")
    return sum(1 for _ in open(path)) if os.path.exists(path) else 0


def test_idempotency_dedup():
    """(a) Run the pipeline twice on the same window; the second run must add nothing."""
    _clean_dryrun()
    try:
        _run("--dry-run", "--use-cache", "--top-n", "0")
        hb1, n1 = _heartbeat(), _leads_lines()
        _run("--dry-run", "--use-cache", "--top-n", "0")
        hb2, n2 = _heartbeat(), _leads_lines()

        assert hb1["drafts_created"] > 0, f"run 1 should create drafts, got {hb1['drafts_created']}"
        assert hb2["drafts_created"] == 0, f"run 2 must create 0 new drafts, got {hb2['drafts_created']}"
        assert n2 == n1, f"leads file grew on re-run: {n1} -> {n2}"
        print(f"PASS (a) idempotency/dedup: run1={hb1['drafts_created']} drafts, "
              f"run2=0 new, leads stable at {n1}")
    finally:
        _clean_dryrun()


def test_no_send_capability():
    """(b) No SMTP / messages.send anywhere in the pipeline; mail can only be DRAFTED."""
    pipeline = ["fetch_cases.py", "qualify.py", "state.py", "draft.py", "sinks.py", "run.py"]
    forbidden = ["smtplib", "sendmail", "messages/send", "users.messages.send",
                 ".send_message(", "SMTP("]
    offenders = []
    for fname in pipeline:
        src = open(os.path.join(REPO, fname), encoding="utf-8").read()
        offenders += [f"{fname}:{pat}" for pat in forbidden if pat in src]
    assert not offenders, f"send capability found in pipeline: {offenders}"

    sinks = open(os.path.join(REPO, "sinks.py"), encoding="utf-8").read()
    assert "/drafts" in sinks, "Gmail sink should target users/me/drafts"
    assert "messages/send" not in sinks, "Gmail sink must not target the send endpoint"
    print("PASS (b) no send: no SMTP/messages.send in pipeline; mail is draft-only")


def test_empty_window_alert():
    """(c) An empty window yields 0 leads and fires the zero-drafts alert."""
    cache = os.path.join(REPO, "data", "raw_search_results.json")
    backup = cache + ".bak_test"
    had_cache = os.path.exists(cache)
    if had_cache:
        shutil.copy(cache, backup)
    _clean_dryrun()
    try:
        os.makedirs(os.path.join(REPO, "data"), exist_ok=True)
        json.dump({"fetched_at": "test", "filed_after": "2026-01-01", "days": 7,
                   "pages": 0, "fetched": 0, "results": []}, open(cache, "w"))
        proc = _run("--dry-run", "--use-cache", "--top-n", "5")
        hb = _heartbeat()

        assert hb["qualified"] == 0, f"empty window should yield 0 qualified, got {hb['qualified']}"
        assert hb["drafts_created"] == 0, f"empty window should create 0 drafts, got {hb['drafts_created']}"
        assert hb["alert_zero_drafts"] is True, "alert_zero_drafts flag must be set"
        assert "ALERT" in proc.stdout, "zero-drafts ALERT should print to stdout"
        print("PASS (c) empty window: 0 leads, alert_zero_drafts=True, ALERT printed")
    finally:
        if had_cache:
            shutil.move(backup, cache)
        else:
            os.remove(cache)
        _clean_dryrun()


if __name__ == "__main__":
    test_idempotency_dedup()
    test_no_send_capability()
    test_empty_window_alert()
    print("\nAll behaviour tests passed.")
