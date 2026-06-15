# Stilta — trigger-based lead detection (CourtListener)

Continuously detects newly-filed U.S. **patent infringement** suits, qualifies the
ones worth contacting, and turns each into a **unique mail draft** addressed to the
defendant company's in-house IP/legal decision-maker — for human review, never sent.

```
fetch  ──▶  qualify  ──▶  dedup  ──▶  one LLM call  ──▶  .eml draft + leads.jsonl  ──▶  commit state
(CourtListener) (rules)   (state)   (Anthropic)        (drafts/)   (JSON Lines)      (seen_dockets.json)
```

Everything is **deterministic except the single LLM call** that writes each lead's
prose fields + email. The model never touches fetching, filtering, dedup, or state.
**No send function exists anywhere in the code** — the only outputs are `.eml` files
and (optionally) Gmail `drafts.create`.

## Requirements

- **Python:** use `/usr/bin/python3` (macOS system Python, 3.9). The Framework
  Python 3.12 (`python3` on a fresh Mac) has no CA bundle and will fail on HTTPS
  calls to CourtListener and Anthropic. All commands below use `/usr/bin/python3`.
- **No third-party packages** — standard library only.
- A `.env` file in the repo root (gitignored). Copy `.env.example` and fill in:

  ```
  COURTLISTENER_API_TOKEN=<40-char token from courtlistener.com>
  ANTHROPIC_API_KEY=<sk-ant-...>
  # for Gmail drafts (--gmail flag):
  GMAIL_CLIENT_ID=<...apps.googleusercontent.com>
  GMAIL_CLIENT_SECRET=<GOCSPX-...>
  GMAIL_REFRESH_TOKEN=<1//...>
  TEST_MAILBOX=<your-test-gmail@gmail.com>
  ```

## Quick start

```bash
cp .env.example .env          # fill in your keys
/usr/bin/python3 run.py --dry-run --use-cache   # smoke-test (no LLM, no network)
/usr/bin/python3 run.py --use-cache             # real run on cached data
/usr/bin/python3 run.py                         # full live run (fetches from CourtListener)
```

## Pushing drafts to Gmail — two options

**Option A — Claude Code + Gmail MCP (no OAuth setup needed).**
Run the pipeline without `--gmail` to generate `leads.jsonl` and `.eml` files, then
open Claude Code with the Gmail MCP connected and say:
*"Read leads.jsonl and create a Gmail draft for each lead."*
Claude reads each record's `email_subject` and `email_body` and calls `drafts.create`
via the MCP. No credentials required beyond having Claude Code and Gmail MCP active.
This is the easiest path for a reviewer.

**Option B — OAuth (for unattended weekly cron).**
The `--gmail` flag mints an access token from `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`
/ `GMAIL_REFRESH_TOKEN` in `.env` and pushes drafts in the same run. No human present
needed — works in launchd or cron. See "Gmail OAuth setup" below for the one-time
credential flow.

## Run options

```bash
/usr/bin/python3 run.py                  # full weekly run (drafts top 12 by priority)
/usr/bin/python3 run.py --top-n 0        # draft every qualified lead
/usr/bin/python3 run.py --use-cache      # reuse data/raw_search_results.json (no refetch)
/usr/bin/python3 run.py --gmail          # also push real Gmail drafts (Option B, needs .env Gmail vars)
/usr/bin/python3 run.py --dry-run        # exercise pipeline with NO LLM (dev-only outputs)
/usr/bin/python3 run.py --reset          # clear seen state + leads + drafts, then run fresh
```

Each stage is also runnable alone for inspection:
```bash
/usr/bin/python3 fetch_cases.py
/usr/bin/python3 qualify.py
/usr/bin/python3 state.py
```

## Outputs

| Path | What |
|---|---|
| `leads.jsonl` | The agent-readable lead list — one JSON object per line. |
| `drafts/<docket_id>-<company>.eml` | One unique draft per lead (RFC 5322). |
| `seen_dockets.json` | Dedup state, keyed on `docket_id`. |
| `logs/last_run.json` | Monitoring heartbeat (metrics of the most recent run). |
| `data/*.json` | Intermediate fetch/qualify artifacts (included for `--use-cache`). |

**`leads.jsonl` record** — the four required fields come first, then enrichment for
downstream agents (qualification, CRM sync, follow-ups):
```json
{"company":"Imperva, Inc.","event_date":"2026-06-10",
 "why_this_matters":"…","suggested_outreach_angle":"…",
 "docket_id":73468336,"docket_number":"2:26-cv-00461","plaintiff":"Congruent Media Resourcing LLC",
 "court":"E.D. Tex.","judge":"James Rodney Gilstrap","npe_plaintiff":true,"priority_score":36,
 "patents":["9,135,418"],"docket_url":"https://…","email_subject":"…",
 "draft_eml":"drafts/73468336-imperva-inc.eml","gmail_draft_id":"r-…","generated_at":"…"}
```

**Why JSON Lines?** Chosen over CSV and Markdown: append-only (one line per weekly run,
no file rewrites), each record is self-contained (nested fields like `patents` need no
escaping), and any downstream agent consumes it with a single `json.loads` per line —
no parsing acrobatics. The format is designed to feed directly into the next agent step
(CRM sync, qualification re-scoring, follow-up drafting).

## Evaluating draft quality

```bash
/usr/bin/python3 eval_judge.py
```

Runs every draft in `leads.jsonl` through an independent LLM judge (claude-opus-4-6,
a different model from the generator) and scores five dimensions:

| Dimension | Type | What it checks |
|---|---|---|
| `icp_fit` | 1–5 | Is this company the right target? |
| `grounding` | 1–5 | Only facts from the docket used — no hallucinations? |
| `angle_specificity` | 1–5 | Is the pitch specific to this case, not generic? |
| `draft_uniqueness` | 1–5 | Does it read like a real email, not a template? |
| `field_correctness` | pass/fail | All required JSON fields present and non-empty? |
| `correct_parties` | pass/fail | Plaintiff/defendant order correct? |
| `format_hygiene` | pass/fail | No em-dashes, correct sign-off, ASCII only? |
| `agent_consumability` | pass/fail | JSON is clean and machine-readable? |

Results are written to `eval_results.jsonl`.

## Running the tests

```bash
/usr/bin/python3 test_behaviour.py
```

Three deterministic checks (no LLM, no network — fast and free to run anytime):

- **(a) Idempotency:** a second run on the same window creates 0 new drafts and does
  not grow the leads file.
- **(b) No send:** grep confirms no SMTP/send capability exists anywhere in the pipeline.
- **(c) Empty window:** an empty fetch fires the `alert_zero_drafts` flag and prints `ALERT`.

## Resetting the system

If all leads have already been drafted (you get `new-after-dedup: 0`) and you want
to re-run and re-draft everything from scratch, use the `--reset` flag:

```bash
/usr/bin/python3 run.py --reset --use-cache --top-n 0          # re-draft, no Gmail
/usr/bin/python3 run.py --reset --gmail --use-cache --top-n 0  # re-draft + push to Gmail
```

`--reset` deletes `seen_dockets.json`, `leads.jsonl`, and the `drafts/` folder before
the run starts, then proceeds normally. The cached fetch data (`data/`) is kept so you
don't have to refetch from CourtListener.

**When to use this:**
- You want to regenerate all drafts after changing the prompt
- You are demoing or testing the system and want a clean slate
- A reviewer wants to run the pipeline themselves end-to-end

Note: `--reset` only clears the real output files. The dry-run dev files
(`leads.dryrun.jsonl`, `seen_dryrun.json`, `drafts_dryrun/`) are never touched.

## Gmail OAuth setup (one-time)

To use `--gmail`, you need a Google Cloud OAuth client. One-time setup:

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create a project.
2. Enable the **Gmail API**.
3. Create an **OAuth 2.0 Client ID** (Desktop app type) and download the credentials
   (`client_id` and `client_secret`).
4. Add the scope `https://www.googleapis.com/auth/gmail.compose`.
5. Run the OAuth flow once to get a `refresh_token`:
   ```
   GET https://accounts.google.com/o/oauth2/v2/auth
     ?client_id=CLIENT_ID
     &redirect_uri=urn:ietf:wg:oauth:2.0:oob
     &response_type=code
     &scope=https://www.googleapis.com/auth/gmail.compose
     &access_type=offline
     &prompt=consent
   ```
   Exchange the returned `code` for tokens:
   ```bash
   curl -X POST https://oauth2.googleapis.com/token \
     -d client_id=CLIENT_ID \
     -d client_secret=CLIENT_SECRET \
     -d code=CODE \
     -d grant_type=authorization_code \
     -d redirect_uri=urn:ietf:wg:oauth:2.0:oob
   ```
   Copy `refresh_token` from the response into `.env`.

The pipeline auto-mints a short-lived access token at the start of each run —
no manual token refresh needed.

## Dedup mechanism (idempotent weekly runs)

- **`seen_dockets.json`** is a JSON object keyed on `docket_id`. A case is processed
  only if its id is not already a key.
- The id is written **only AFTER** its draft is successfully created (`state.commit`),
  and persisted with an **atomic write** (temp file + `os.replace`). A crash before a
  draft just retries that lead next run — never a silent drop.
- A corrupt state file is quarantined to `.corrupt` and the run continues.
- **Reset** (re-draft everything): `run.py --reset` (or manually: `rm seen_dockets.json leads.jsonl && rm -rf drafts`).

## Exact weekly schedule

Designed for **weekly** runs (filing window = last 7 days). Two equivalent options;
both call the wrapper [`schedule/run_weekly.sh`](schedule/run_weekly.sh).

**macOS — launchd (recommended).** Runs **Mondays 08:00 local**.
```bash
chmod +x schedule/run_weekly.sh
cp schedule/com.stilta.leadgen.weekly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stilta.leadgen.weekly.plist
# verify / run once now / remove:
launchctl list | grep stilta
launchctl start com.stilta.leadgen.weekly
launchctl unload ~/Library/LaunchAgents/com.stilta.leadgen.weekly.plist
```

**Linux/cron** — `crontab -e`, then (Mondays 08:00):
```cron
0 8 * * 1  /Users/astridatle/stilta-lead-gen/schedule/run_weekly.sh
```

## Monitoring

Every run writes `logs/last_run.json` and prints the run metrics
(`fetched / qualified / new-after-dedup / drafts-created`), and **alerts in-line when
`drafts-created == 0`**. A lightweight watcher should alert if any of:
- `logs/last_run.json` is **older than ~8 days** (the weekly run did not fire), or
- `alert_zero_drafts == true`, or
- `fetched` drops to an anomalous level (source/RECAP outage).

See [PM.md](PM.md) for failure modes and the 2-week roadmap,
[DECISIONS.md](DECISIONS.md) for all design choices, and
[SOURCES.md](SOURCES.md) for the top-3 source selection and reasoning.
