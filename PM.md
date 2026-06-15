# PM — trigger-based lead detection for Stilta

**What it does.** Each week it pulls newly-filed U.S. patent suits (CourtListener,
nature-of-suit 830, last 7 days), keeps the ones worth contacting, and drafts a
unique email per lead to the **defendant's in-house IP/legal decision-maker** —
reviewed by a human, never sent. End-to-end and deterministic except one grounded
LLM call.

**The qualification line (where noise starts).** Hard gate: the **defendant must be a
real operating company** — drop individuals (`Shi`), Schedule A / "Doe" storefront
cases (the N.D. Ill. cluster), government, and unverifiable single-token names. Then a
case only qualifies if it also shows a **troll/venue signal**: an NPE-style plaintiff
(name heuristics or a ≥3-suit campaign in the same batch) **or** a patent-heavy venue
(E.D. Tex., D. Del.). An operating-company defendant with neither signal is treated as
noise. On the sample run this took **44 fetched → 31 qualified**, with the ABC IP
10-suit firearms campaign and EDTX NPE filings rising to the top.

**What I chose against.** Paid sources (Lex Machina, Docket Navigator, Darts-IP) —
better-structured but cost/integration overhead against an 80/20 goal; CourtListener
is free, fast, and already carries complaint text for grounding. A maintained NPE list
(RPX/Unified) — replaced with name + repeat-filer heuristics for v1. Defendant's
*outside counsel* as recipient — abandoned after the data showed fresh dockets have
**no defendant counsel of record** (and often no party data ingested yet); the in-house
persona is always derivable from the case caption. Company-level clustering of mirror
suits — left as per-docket for now.

**Failure modes.**
- *Source down / RECAP lag.* CourtListener outage or ingestion delay → few/zero cases.
  Mitigated by retries+backoff and the `drafts-created == 0` alert; a real zero is
  indistinguishable from an outage only if we ignore the `fetched` count, so we monitor
  both (see below).
- *Bad dedup.* `docket_id` is stable, so the main risks are a corrupt state file
  (quarantined to `.corrupt`, run continues) and the tiny "draft created, crash before
  persist" window (shrunk to one atomic write; bias is toward a rare duplicate, never a
  silent drop). Wrong key would re-mail everyone — covered by an idempotency check.
- *False positives → wrong mail.* Two layers. (1) The hard gate removes individuals /
  Schedule A / government. (2) The LLM is instructed to ground only in supplied facts;
  verified that thin-grounding cases say "the asserted patents aren't public yet" rather
  than inventing a number. Residual risk (e.g. an operating company misread as an NPE)
  is contained because everything is a **draft to a test mailbox**, gated on human review — nothing sends.
- *LLM variance / API error.* A failed generation skips that lead (not marked seen,
  retried next run); drafts are not templates, so quality is the review gate.

**If I had two more weeks (priority order).** (1) Party/attorney **enrichment** via the
authenticated `/parties/` endpoint so we name the real in-house contact / upgrade to
counsel-of-record once it appears. (2) Swap NPE heuristics for a real NPE dataset +
litigation-history lookup (entity resolution across name variants). (3) Pull full
complaint text (`/recap-documents/`) for richer, more specific grounding and accused
products. (4) Add **PTAB** and **SEC EDGAR** sources behind the same qualify/draft
interface. (5) A reviewer UI + CRM sync from `leads.jsonl` — `eval_judge.py` already scores
every draft (grounding, ICP fit, angle specificity, format hygiene) via an independent
opus judge; what's missing is a human approve/reject interface that moves approved
drafts to a real outbox. (6) Company-level dedup/clustering so two suits against one
defendant become one outreach.

**How we know the weekly run still works.** Every run writes `logs/last_run.json`
(`fetched / qualified / new_after_dedup / drafts_created / alert_zero_drafts`). A watcher
(cron + Slack/email) alerts if: the heartbeat is **>8 days old** (the job didn't fire),
`drafts_created == 0`, `qualified == 0`, or `fetched` collapses versus its trailing
average (source/RECAP problem). That separates "a quiet week" from "the pipeline broke."
