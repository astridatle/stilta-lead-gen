"""Standalone LLM-as-judge evaluation. READ-ONLY over the pipeline's outputs.

This script NEVER writes leads.jsonl or seen_dockets.json, and NEVER creates or
sends drafts. Its only output is eval_results.jsonl. It is fully self-contained
(no import from the generator pipeline) and uses a DIFFERENT model from the one
that produced the drafts, so the generator never grades its own work.

For each lead it makes ONE judge call with the fixed rubric below, joining:
  - the lead record (leads.jsonl: company, event_date, why_this_matters, angle, ...)
  - the generated draft (subject from the record + body from its .eml)
  - the source case data (data/qualified.json: caseName, venue, plaintiff,
    defendant, filing date, asserted patents)

Usage:
  python3 eval_judge.py --limit 3      # preview on first 3 leads
  python3 eval_judge.py                 # full batch
  EVAL_MODEL=claude-opus-4-8 python3 eval_judge.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from email import message_from_binary_file, policy

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# The generator's default (must match draft.DEFAULT_MODEL). The judge MUST differ.
GENERATOR_MODEL_DEFAULT = "claude-sonnet-4-6"
EVAL_MODEL_DEFAULT = "claude-opus-4-6"  # different family -> independent judge

NUMERIC_DIMS = ["icp_fit", "grounding", "angle_specificity", "draft_uniqueness"]
PASSFAIL_DIMS = ["field_correctness", "correct_parties", "format_hygiene", "agent_consumability"]
ALL_DIMS = NUMERIC_DIMS + PASSFAIL_DIMS

JUDGE_SYSTEM = """\
You are an INDEPENDENT reviewer scoring outbound sales leads that another system
produced for Stilta, an AI platform for patent work (invalidity research,
infringement analysis, freedom-to-operate). You did not write these leads and have
no stake in them. Judge strictly, using ONLY the data provided in the message.

For EVERY dimension you must write your REASONING FIRST and then the score. Never
put a score before its reasoning. Keep each reasoning to 1-2 sentences.

Rubric:
- icp_fit (1-5): Is the DEFENDANT plausibly a company that would buy external
  patent expertise (an operating company with real exposure), versus a tiny
  shell/individual that would not? 5 = clearly a substantial operating company,
  1 = implausible buyer.
- grounding (1-5): Is "why this matters" actually TRUE given the source case data,
  with nothing invented (no fabricated patent numbers, patent counts, products,
  judges, or dates)? 5 = fully supported by the source, 1 = fabricated/contradicted.
- angle_specificity (1-5): Is suggested_outreach_angle sharp and specific to THIS
  case rather than a platitude? 5 = concretely tailored, 1 = generic boilerplate.
- draft_uniqueness (1-5): Does the email reference this case's concrete facts
  (plaintiff, venue, judge, patents) so it could NOT be sent unchanged to a
  different defendant? 5 = clearly case-specific, 1 = could go to anyone.
- field_correctness (pass/fail): Do the lead's company and event_date match the
  source case data? pass only if BOTH match.
- correct_parties (pass/fail): Does the email address the DEFENDANT's in-house
  IP/legal team (the party being SUED), without confusing plaintiff and defendant
  or pitching the wrong side? fail on any party confusion.
- format_hygiene (pass/fail): No unfilled recipient placeholders (e.g. "[Name]"),
  no em-dashes or en-dashes, and a reasonable subject length (about <= 80 chars).
  The sender sign-off "[Your name]" is allowed and is not a violation.
- agent_consumability (pass/fail): Is the LEAD RECORD a single valid JSON object
  with all four of company, event_date, why_this_matters, suggested_outreach_angle
  present and non-empty?

Output STRICT JSON only (no markdown, no prose around it), an object with EXACTLY
these keys: icp_fit, grounding, angle_specificity, draft_uniqueness,
field_correctness, correct_parties, format_hygiene, agent_consumability.
Each value MUST be an object whose FIRST field is "reasoning" (string) and whose
SECOND field is "score". For the 1-5 dimensions "score" is an integer 1-5. For the
pass/fail dimensions "score" is exactly "pass" or "fail".
"""


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not os.environ.get(key):
                os.environ[key] = value.strip().strip('"').strip("'")


def anthropic_call(system: str, user: str, api_key: str, model: str,
                   max_tokens: int = 1500, temperature: float = 0.0) -> str:
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_URL, data=payload, method="POST",
        headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"Anthropic API {err.code}: {err.read().decode('utf-8','replace')[:400]}") from None
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def extract_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?", "", text.strip()).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if not m:
            raise ValueError(f"judge did not return JSON: {text[:200]!r}")
        return json.loads(m.group(0))


def load_sources(path: str = os.path.join("data", "qualified.json")) -> dict:
    src = {}
    if os.path.exists(path):
        for r in json.load(open(path, encoding="utf-8")).get("qualified", []):
            src[r["docket_id"]] = {
                "case_name": r.get("case_name"),
                "venue": r.get("court"),
                "plaintiff": r.get("plaintiff"),
                "defendant": r.get("company"),
                "filing_date": r.get("event_date"),
                "asserted_patents": r.get("grounding", {}).get("patents", []),
                "judge": r.get("judge"),
                "repeat_filer_count": r.get("signals", {}).get("repeat_filer_count"),
                "complaint_snippet": r.get("grounding", {}).get("complaint_snippet", ""),
            }
    return src


def source_for(lead: dict, sources: dict) -> dict:
    s = sources.get(lead.get("docket_id"))
    if s:
        return s
    # Fallback: reconstruct from the lead record itself if qualified.json lacks it.
    return {
        "case_name": f"{lead.get('plaintiff')} v. {lead.get('company')}",
        "venue": lead.get("court"),
        "plaintiff": lead.get("plaintiff"),
        "defendant": lead.get("company"),
        "filing_date": lead.get("event_date"),
        "asserted_patents": lead.get("patents", []),
        "judge": lead.get("judge"),
    }


def read_body(eml_path: str) -> str:
    if not eml_path or not os.path.exists(eml_path):
        return ""
    with open(eml_path, "rb") as fh:
        msg = message_from_binary_file(fh, policy=policy.default)
    return (msg.get_content() or "").strip()


def build_judge_user(lead: dict, raw_line: str, source: dict, body: str) -> str:
    pats = source.get("asserted_patents") or []
    pats_str = ", ".join(pats) if pats else "(none available in source)"
    repeat = source.get("repeat_filer_count")
    repeat_str = f"{repeat} suit(s) by this plaintiff in the same run" if repeat else "n/a"
    return (
        "SOURCE CASE DATA (ground truth to check the lead against). Any fact below is\n"
        "grounded and the lead may use it; a claim NOT supported here is a grounding miss.\n"
        f"- caseName        : {source.get('case_name')}\n"
        f"- venue           : {source.get('venue')}\n"
        f"- plaintiff       : {source.get('plaintiff')}\n"
        f"- defendant       : {source.get('defendant')}\n"
        f"- filing date     : {source.get('filing_date')}\n"
        f"- asserted patents: {pats_str}\n"
        f"- assigned judge  : {source.get('judge') or '(unassigned)'}\n"
        f"- plaintiff campaign size: {repeat_str}\n"
        f"- complaint excerpt: {source.get('complaint_snippet') or '(not ingested)'}\n\n"
        "LEAD RECORD (raw JSON line, exactly as an agent would read it):\n"
        f"{raw_line}\n\n"
        "PARSED LEAD FIELDS:\n"
        f"- company                  : {lead.get('company')}\n"
        f"- event_date               : {lead.get('event_date')}\n"
        f"- why_this_matters         : {lead.get('why_this_matters')}\n"
        f"- suggested_outreach_angle : {lead.get('suggested_outreach_angle')}\n\n"
        "GENERATED EMAIL DRAFT:\n"
        f"Subject: {lead.get('email_subject')}\n"
        "Body:\n"
        f"{body}\n\n"
        "Score every rubric dimension. Reasoning first, then score, for each."
    )


def evaluate_lead(lead: dict, raw_line: str, source: dict, body: str,
                  api_key: str, model: str) -> dict:
    user = build_judge_user(lead, raw_line, source, body)
    parsed = extract_json(anthropic_call(JUDGE_SYSTEM, user, api_key, model))
    scores, justifications = {}, {}
    for dim in ALL_DIMS:
        obj = parsed.get(dim, {})
        score = obj.get("score")
        if dim in NUMERIC_DIMS:
            score = int(score)
            if not 1 <= score <= 5:
                raise ValueError(f"{dim} score out of range: {score}")
        else:
            score = str(score).lower().strip()
            if score not in ("pass", "fail"):
                raise ValueError(f"{dim} verdict invalid: {score!r}")
        scores[dim] = score
        justifications[dim] = obj.get("reasoning", "")
    return {
        "docket_id": lead.get("docket_id"),
        "company": lead.get("company"),
        "scores": scores,
        "justifications": justifications,
        "judge_model": model,
        "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def print_summary(results: list) -> None:
    print("\n" + "=" * 60)
    print(f"EVAL SUMMARY  (n={len(results)}, judge={results[0]['judge_model'] if results else 'n/a'})")
    print("=" * 60)
    print("1-5 dimensions (average):")
    for dim in NUMERIC_DIMS:
        vals = [r["scores"][dim] for r in results]
        print(f"  {dim:20s} {sum(vals)/len(vals):.2f}")
    print("pass/fail dimensions (pass rate):")
    for dim in PASSFAIL_DIMS:
        vals = [r["scores"][dim] for r in results]
        passes = sum(1 for v in vals if v == "pass")
        print(f"  {dim:20s} {passes}/{len(vals)}  ({100*passes/len(vals):.0f}%)")


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM-as-judge eval over pipeline output (read-only).")
    ap.add_argument("--leads", default="leads.jsonl")
    ap.add_argument("--out", default="eval_results.jsonl")
    ap.add_argument("--limit", type=int, default=0, help="evaluate only the first N leads (0 = all)")
    ap.add_argument("--model", default=None, help="judge model (default $EVAL_MODEL or opus)")
    args = ap.parse_args()

    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ERROR: ANTHROPIC_API_KEY not set (put it in .env).")

    generator_model = os.environ.get("ANTHROPIC_MODEL", GENERATOR_MODEL_DEFAULT)
    eval_model = args.model or os.environ.get("EVAL_MODEL", EVAL_MODEL_DEFAULT)
    if eval_model == generator_model:
        sys.exit(f"ERROR: judge model ({eval_model}) must DIFFER from the generator "
                 f"model ({generator_model}). Set EVAL_MODEL to a different model.")

    if not os.path.exists(args.leads):
        sys.exit(f"Missing {args.leads}. Run the pipeline first.")
    raw_lines = [ln.rstrip("\n") for ln in open(args.leads, encoding="utf-8") if ln.strip()]
    leads = [json.loads(ln) for ln in raw_lines]
    if args.limit:
        raw_lines, leads = raw_lines[: args.limit], leads[: args.limit]

    sources = load_sources()
    print(f"judging {len(leads)} lead(s) | generator={generator_model} | judge={eval_model}")

    results = []
    for raw_line, lead in zip(raw_lines, leads):
        src = source_for(lead, sources)
        body = read_body(lead.get("draft_eml", ""))
        try:
            res = evaluate_lead(lead, raw_line, src, body, api_key, eval_model)
        except Exception as e:
            print(f"  [skip] {lead.get('docket_id')} {lead.get('company')}: {e}")
            continue
        results.append(res)
        sc = res["scores"]
        print(f"  judged {lead.get('docket_id')} {lead.get('company')[:26]:26} "
              f"icp={sc['icp_fit']} grnd={sc['grounding']} ang={sc['angle_specificity']} "
              f"uniq={sc['draft_uniqueness']} | "
              f"{'/'.join(sc[d][0].upper() for d in PASSFAIL_DIMS)}")

    # eval_results.jsonl is THIS script's only output; rewritten each run.
    with open(args.out, "w", encoding="utf-8") as fh:
        for res in results:
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")

    if results:
        print_summary(results)
    print(f"\nwrote {len(results)} rows to {args.out}")


if __name__ == "__main__":
    main()
