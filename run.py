"""Orchestrator: fetch -> qualify -> dedup -> (one LLM call) -> draft -> state.

Deterministic everywhere except draft.generate (the single LLM call). Designed
for idempotent weekly runs: re-running drafts nothing already drafted.

Per-lead ordering is deliberate (see DECISIONS.md / state.py):
    generate draft  ->  push as .eml (+ optional Gmail draft)  ->  append leads.jsonl
    ->  commit docket_id to seen state
The docket is marked seen ONLY after its draft exists, so a crash just retries
the lead next run instead of dropping or duplicating it.

Usage:
    python3 run.py                  # real run (needs ANTHROPIC_API_KEY in .env)
    python3 run.py --top-n 10       # cap drafts created this run
    python3 run.py --use-cache      # reuse data/raw_search_results.json (no refetch)
    python3 run.py --gmail          # also push real Gmail drafts (needs GMAIL_ACCESS_TOKEN)
    python3 run.py --dry-run        # exercise the pipeline with NO LLM (dev-only outputs)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import draft as draft_mod
import fetch_cases
import qualify
import sinks
import state as state_mod

LEADS_PATH = "leads.jsonl"
DRAFTS_DIR = "drafts"
RAW_CACHE = os.path.join("data", "raw_search_results.json")

# Test mailbox only -- never a real prospect. Override via env.
FROM_ADDR = os.environ.get("STILTA_FROM", "stilta-outbound@example.test")
TEST_MAILBOX = os.environ.get("TEST_MAILBOX", "stilta-leads-test@example.test")


def append_lead(path: str, lead: dict, d: dict, eml_ref: dict, gmail_ref: dict | None) -> None:
    """Append one JSON Lines record. First four keys are the required schema;
    the rest enrich the dataflow for downstream agents (qualify/CRM/follow-up)."""
    record = {
        "company": lead["company"],
        "event_date": lead["event_date"],
        "why_this_matters": d["why_this_matters"],
        "suggested_outreach_angle": d["suggested_outreach_angle"],
        # --- enrichment (optional for the spec, useful for the next agent) ---
        "docket_id": lead["docket_id"],
        "docket_number": lead["docket_number"],
        "plaintiff": lead["plaintiff"],
        "court": lead["court"],
        "judge": lead["judge"],
        "npe_plaintiff": lead["signals"]["npe_plaintiff"],
        "priority_score": lead["signals"]["priority_score"],
        "patents": lead["grounding"]["patents"],
        "docket_url": lead["docket_url"],
        "email_subject": d["email_subject"],
        "draft_eml": eml_ref["ref"],
        "gmail_draft_id": gmail_ref["ref"] if gmail_ref else None,
        "campaign": lead.get("campaign"),  # set only on a collapsed-campaign representative
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stilta trigger-based lead drafter.")
    ap.add_argument("--days", type=int, default=7, help="filing window (default 7)")
    ap.add_argument("--top-n", type=int, default=12, help="max drafts this run (0 = all)")
    ap.add_argument("--use-cache", action="store_true", help="reuse cached fetch output")
    ap.add_argument("--gmail", action="store_true", help="also create real Gmail drafts via OAuth (needs GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN in .env)")
    ap.add_argument("--dry-run", action="store_true", help="no LLM; dev-only outputs")
    ap.add_argument("--reset", action="store_true", help="clear seen state, leads file, and drafts before running (re-drafts everything)")
    args = ap.parse_args()

    if args.reset:
        import shutil
        for path in [LEADS_PATH, state_mod.DEFAULT_STATE_PATH]:
            try:
                os.remove(path)
                print(f"[reset] removed {path}")
            except FileNotFoundError:
                pass
        if os.path.isdir(DRAFTS_DIR):
            shutil.rmtree(DRAFTS_DIR)
            print(f"[reset] removed {DRAFTS_DIR}/")

    fetch_cases.load_dotenv()
    cl_token = os.environ.get("COURTLISTENER_API_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("ANTHROPIC_MODEL", draft_mod.DEFAULT_MODEL)

    if not args.dry_run and not api_key:
        sys.exit("ERROR: set ANTHROPIC_API_KEY (e.g. in .env) for the LLM draft step, "
                 "or use --dry-run to test the pipeline without it.")

    # Dry-run writes to dev-only paths so it never pollutes the real deliverables.
    leads_path = "leads.dryrun.jsonl" if args.dry_run else LEADS_PATH
    drafts_dir = "drafts_dryrun" if args.dry_run else DRAFTS_DIR
    state_path = "seen_dryrun.json" if args.dry_run else state_mod.DEFAULT_STATE_PATH

    # 1. FETCH
    if args.use_cache and os.path.exists(RAW_CACHE):
        bundle = json.load(open(RAW_CACHE, encoding="utf-8"))
        print(f"[fetch] using cache {RAW_CACHE} ({bundle['fetched']} cases)")
    else:
        bundle = fetch_cases.fetch_recent_patent_cases(days=args.days, token=cl_token)
    fetched = bundle["fetched"]

    # 2. QUALIFY
    qresult = qualify.qualify_cases(bundle["results"])
    qualified = qresult["qualified"]

    # 3. DEDUP
    st = state_mod.load_state(state_path)
    new, already = state_mod.select_new(qualified, st)
    to_process = new if args.top_n == 0 else new[: args.top_n]

    # sinks: .eml always; Gmail only if asked AND credentials are present
    eml_sink = sinks.EmlSink(drafts_dir, FROM_ADDR, TEST_MAILBOX)
    gmail_sink = None
    if args.gmail:
        gid = os.environ.get("GMAIL_CLIENT_ID")
        gsec = os.environ.get("GMAIL_CLIENT_SECRET")
        gref = os.environ.get("GMAIL_REFRESH_TOKEN")
        if gid and gsec and gref:
            print("[gmail] minting access token from refresh_token ...")
            gmail_sink = sinks.GmailDraftSink(gid, gsec, gref, FROM_ADDR, TEST_MAILBOX)
            print("[gmail] token OK")
        else:
            print("[gmail] --gmail set but GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN missing; writing .eml only")

    # 4. GENERATE + DRAFT + COMMIT (per lead)
    drafts_created = 0
    for lead in to_process:
        tag = f"{lead['docket_id']} {lead['company']}"
        try:
            d = draft_mod.dry_run_generate(lead) if args.dry_run else draft_mod.generate(lead, api_key, model)
            eml_ref = eml_sink.push(lead, d)               # .eml written
            gmail_ref = gmail_sink.push(lead, d) if gmail_sink else None  # hard error if fails
            append_lead(leads_path, lead, d, eml_ref, gmail_ref)
            state_mod.commit(st, lead, state_path)         # mark seen ONLY after ALL sinks succeed
            drafts_created += 1
            print(f"[draft] OK  {tag} -> {eml_ref['ref']}")
        except Exception as e:
            print(f"[draft] ERR {tag}: {e}  (skipped; NOT marked seen, will retry next run)")

    # 5. LOGGING (required metrics)
    print("\n" + "=" * 60)
    print("RUN SUMMARY" + ("  [DRY-RUN]" if args.dry_run else ""))
    print("=" * 60)
    print(f"fetched          : {fetched}")
    print(f"qualified        : {len(qualified)}")
    print(f"new-after-dedup  : {len(new)}  (skipped {len(already)} already-seen)")
    print(f"drafts-created   : {drafts_created}  (capped at top-{args.top_n})" if args.top_n
          else f"drafts-created   : {drafts_created}")
    print(f"leads file       : {leads_path}")
    print(f"drafts dir       : {drafts_dir}/")

    # monitoring heartbeat: every run drops a machine-readable status file so a
    # watcher can alert on staleness (no recent run) or drafts_created == 0.
    os.makedirs("logs", exist_ok=True)
    hb_path = "logs/last_run.dryrun.json" if args.dry_run else "logs/last_run.json"
    with open(hb_path, "w", encoding="utf-8") as fh:
        json.dump({
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "fetched": fetched,
            "qualified": len(qualified),
            "new_after_dedup": len(new),
            "drafts_created": drafts_created,
            "alert_zero_drafts": drafts_created == 0,
        }, fh, indent=2)
    print(f"heartbeat        : {hb_path}")

    if drafts_created == 0:
        print("\n!! ALERT: drafts-created = 0. Either nothing new this week, or a "
              "fetch/qualify/LLM failure. Investigate before assuming 'no leads'.")


if __name__ == "__main__":
    main()
