"""Dedup + state (deterministic, no LLM).

Idempotent weekly runs need durable memory of what we've already turned into a
draft, so a re-run does not email the same docket twice.

Mechanism:
  - `seen_dockets.json`, a JSON object KEYED ON docket_id.
  - A case is processed only if its docket_id is NOT already a key.
  - The id is written ONLY AFTER its draft is successfully created
    (the orchestrator calls `commit(...)` right after the draft push). So:
      * crash/early-exit BEFORE a draft is made  -> id not written -> the lead is
        simply retried next run (no duplicate, nothing silently lost).
      * we never pre-mark, which would risk marking a lead "seen" whose draft
        never got created and thus dropping it forever.
    The only residual duplicate window is "draft created, then crash before the
    very next line persists the id" -- we shrink that to ~one statement by
    persisting per-lead with an atomic write (temp file + os.replace).

This module does no network, no LLM, no email.
"""

from __future__ import annotations

import datetime as dt
import json
import os

DEFAULT_STATE_PATH = "seen_dockets.json"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_state(path: str = DEFAULT_STATE_PATH) -> dict:
    """Load state; tolerate a missing or corrupt file without crashing the run."""
    if not os.path.exists(path):
        return {"updated_at": None, "seen": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "seen" not in data:
            raise ValueError("unexpected state shape")
        return data
    except (json.JSONDecodeError, ValueError) as err:
        # Don't lose the run over a bad file: quarantine it and start clean.
        backup = path + ".corrupt"
        os.replace(path, backup)
        print(f"[state] WARNING: {path} was unreadable ({err}); quarantined to {backup}")
        return {"updated_at": None, "seen": {}}


def is_seen(state: dict, docket_id) -> bool:
    return str(docket_id) in state["seen"]


def mark_seen(state: dict, docket_id, meta: dict | None = None) -> None:
    """Record a docket as drafted. In-memory only -- call save_state to persist."""
    state["seen"][str(docket_id)] = {"first_seen": _now(), **(meta or {})}


def save_state(state: dict, path: str = DEFAULT_STATE_PATH) -> None:
    """Atomic write: a crash mid-write can't corrupt the existing state file."""
    state["updated_at"] = _now()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)  # atomic on POSIX


def commit(state: dict, lead: dict, path: str = DEFAULT_STATE_PATH) -> None:
    """Mark one lead as drafted AND persist immediately. Orchestrator calls this
    right after a draft is successfully created -- never before."""
    mark_seen(state, lead["docket_id"], {
        "company": lead.get("company"),
        "docket_number": lead.get("docket_number"),
        "case_name": lead.get("case_name"),
    })
    save_state(state, path)


def select_new(qualified: list[dict], state: dict) -> tuple[list[dict], list[dict]]:
    """Split qualified leads into (new, already_seen), preserving order."""
    new, already = [], []
    for lead in qualified:
        (already if is_seen(state, lead["docket_id"]) else new).append(lead)
    return new, already


# --- CLI: report the dedup split (read-only by default) ----------------------
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Report / commit the dedup split.")
    ap.add_argument("--state", default=DEFAULT_STATE_PATH, help="state file path")
    ap.add_argument("--qualified", default=os.path.join("data", "qualified.json"))
    ap.add_argument("--commit", action="store_true",
                    help="mark all currently-new leads as seen (simulates successful drafts)")
    args = ap.parse_args()

    if not os.path.exists(args.qualified):
        raise SystemExit(f"Missing {args.qualified}. Run `python3 qualify.py` first.")

    qualified = json.load(open(args.qualified, encoding="utf-8"))["qualified"]
    state = load_state(args.state)
    new, already = select_new(qualified, state)

    print("=" * 72)
    print("Dedup + state")
    print("=" * 72)
    print(f"state file        : {args.state} ({len(state['seen'])} dockets already seen)")
    print(f"qualified leads    : {len(qualified)}")
    print(f"already seen (skip): {len(already)}")
    print(f"NEW after dedup    : {len(new)}")
    print()
    print("New leads that would be drafted this run:")
    for lead in new[:12]:
        print(f"  + {lead['docket_id']}  {lead['court_id']:5}  {lead['company']}")
    if len(new) > 12:
        print(f"  ... and {len(new) - 12} more")
    if already:
        print("\nSkipped (already drafted in a prior run):")
        for lead in already[:12]:
            print(f"  - {lead['docket_id']}  {lead['company']}")

    if args.commit:
        for lead in new:
            commit(state, lead, args.state)
        print(f"\nCommitted {len(new)} dockets to {args.state} (simulating successful drafts).")
