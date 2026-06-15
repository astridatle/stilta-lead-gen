# Design decisions (locked)

Running log of the choices this prototype is built to. Updated as we go.

## Source (DONE — fetch step)
- CourtListener **v4 Search API**, `type=r` (RECAP federal dockets).
- `nature_of_suit=830` (Patent), `filed_after = today − 7 days`, ordered newest first.
- Designed for **weekly** runs. Stdlib-only (urllib) — no third-party deps.

## Qualify / filter (DONE)
- **Hard gate:** keep only cases where the **defendant is a real operating company.**
  Drop individuals, shell entities, government, Schedule A / "Doe" cases.
- `company` = the **defendant**, derived from `caseName` ("Plaintiff v. Defendant"),
  **title-cased for display** (fix 4, acronym/suffix-preserving), so it survives
  dockets where CourtListener has not yet ingested party data.
- **Ranking — defendant quality is the PRIMARY driver (fix 2).** A lightweight,
  deterministic name-based tier (large / mid / small — a curated known-large set plus
  heuristics, standing in for v2 firmographic enrichment) dominates `priority_score`;
  NPE-plaintiff and patent-heavy venue (EDTX=`txed`, D.Del.=`ded`) are secondary boosts.
  So Samsung / Imperva / Pepperl rank above small resellers.
- **Qualification line:** an operating-company defendant qualifies only with a
  troll/venue signal (NPE-style plaintiff, ≥3-suit campaign, or priority venue).
- **Campaign de-flooding (fix 3):** group by plaintiff; large defendants always stay as
  their own lead; a low-tier (non-large) tail of ≥2 collapses into ONE representative
  that records `campaign_size` + the other defendants (lossless). One NPE campaign = one
  lead, not a flood. (Sample run: 44 fetched → 31 qualified → 21 leads.)

## Recipient (v1 — PIVOTED 2026-06-15)
- Original spec said "defendant's counsel from the docket." **Empirically unavailable:**
  freshly-filed defendants have **0 counsel of record** (only the plaintiff who filed
  has appeared), and many fresh dockets have **no party data ingested at all**.
- **Pivot:** the draft is written **FOR the defendant company's in-house IP/legal
  decision-maker** (GC / Head of IP / in-house IP counsel) — the party that actually
  needs invalidity/FTO help. Salutation + outreach angle target that in-house persona.
- `To:` is always a **test mailbox** (drafts only, never sent).

## Output
- Append one record per lead to **`leads.jsonl`** (JSON Lines: one valid JSON object
  per line — agent-readable, append-friendly, no parsing acrobatics).
- Fields: `company`, `event_date`, `why_this_matters`, `suggested_outreach_angle`.

## Dedup / state
- **`seen_dockets.json`** keyed on `docket_id`. Skip cases already seen.
- Write the id **only AFTER** the draft is successfully created, so a mid-run crash
  drops a lead rather than creating a duplicate.

## Drafts
- One **unique** email per lead (subject + body), grounded in that case's specific
  facts — **never a template**.
- Push as a **draft** via Gmail `drafts.create` (OAuth, auto-minted from
  `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` in `.env`).
  Gmail MCP was tested but is agent-session-only and cannot run in an unattended cron.
- **NEVER send.** There must be no send function anywhere in the code. Test mailbox only.

## Logging
- Per run, print: **fetched / qualified / new-after-dedup / drafts-created.**
- **Flag** if drafts-created is 0.

## Architecture
- Everything **deterministic** EXCEPT the single LLM call that produces the lead
  fields + draft. The model never touches fetching, filtering, dedup, or state.

## Runtime notes (discovered)
- Token via `COURTLISTENER_API_TOKEN`, loaded from a **gitignored `.env`** (the shell
  `export` did not propagate to the run environment).
- `/parties/` is slow/flaky under rapid calls → call it only **after** the cheap
  caseName/venue qualification, with retry/backoff (already in `_get_json`).
