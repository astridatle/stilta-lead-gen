"""Draft generation -- the ONE non-deterministic component.

For a single qualified lead this builds a grounded prompt and makes exactly one
LLM call (Anthropic Messages API, stdlib urllib) that returns the lead's prose
fields plus a unique email. The model is given ONLY this case's facts and is told
not to invent anything. It never sees fetching, filtering, dedup, or state.

Output contract (strict JSON):
  { "why_this_matters", "suggested_outreach_angle", "email_subject", "email_body" }
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"  # override with ANTHROPIC_MODEL

REQUIRED_KEYS = ("why_this_matters", "suggested_outreach_angle", "email_subject", "email_body")

SYSTEM_PROMPT = """\
You are an outbound research assistant for Stilta, an agentic AI platform for \
patent work: invalidity research, infringement analysis, and freedom-to-operate \
(FTO). You write short, credible cold emails to the IN-HOUSE IP / legal \
decision-maker (General Counsel, Head of IP, or senior in-house IP counsel) at a \
company that has JUST been sued for patent infringement, offering Stilta's help \
(rapid prior-art / invalidity search, PTAB/IPR strategy, claim-chart analysis).

Hard rules:
- Ground every statement ONLY in the facts provided. Do NOT invent patent numbers,
  patent COUNTS, accused products, dollar amounts, dates, or people's names. If a
  fact is not given, do not state it. When the asserted patents are not provided,
  refer to them only as "the asserted patents" with NO count and NO numbers.
- Every email must be structurally unique. Let what is most distinctive about THIS
  case drive the structure: the opening angle, the order facts are introduced, and
  the closing ask must all follow from the specific case -- not a fixed skeleton.
  Ask yourself: what is the single most relevant fact here? Lead with that.
  Examples of case-driven variation:
    - Large, well-resourced defendant: open with the tactical situation (Gilstrap's
      tight schedule, early claim construction) rather than the filing itself.
    - Small or mid-size defendant: open with the NPE context and what single-patent
      NPE suits typically demand in terms of response speed.
    - Multi-patent or campaign case: open with the scope or pattern before the ask.
    - Defendant outside the US or in an unfamiliar venue: open with what makes
      this particular filing unusual or high-stakes for them.
  Do not reuse the same sentence structure, paragraph rhythm, or closing line
  across emails. The recipient should not be able to guess that a system wrote it.
- The first sentence after "Hi," must name the DEFENDANT and their situation.
  Do NOT open with the plaintiff's name. The plaintiff is a supporting detail.
- Subject line: under 60 characters, framed from the DEFENDANT's perspective. Do
  NOT use a case caption or patent numbers. Vary the phrasing across emails.
- Party order: whenever you write a case caption like "X v. Y" in the body, X must
  be the PLAINTIFF (the party that FILED the suit) and Y the DEFENDANT. Never
  reverse this.
- Address the defendant's in-house IP/legal TEAM as a role, not a person. Open
  with exactly "Hi," on its own line. No names, no bracketed placeholders.
- Company name: our company is spelled EXACTLY "Stilta": S-T-I-L-T-A. One i, one l,
  one t. Never "Stiita" (double i), "Stitta" (double t), or any other variant.
- Punctuation: no em-dashes, en-dashes, or double hyphens (--). Periods, colons,
  or commas only. Plain ASCII: straight quotes, no fancy typography.
- Three short paragraphs, ~110-160 words total. No "I hope this finds you well",
  no hype, no guarantees. One clear call to action -- vary its form (a call, an
  offer to send an overview, a concrete next step) to fit the situation.
- Sign off EXACTLY as: "Best regards," on one line, then "Astrid Atle" on the
  next. No other sign-off, title, or company line.

Return STRICT JSON only (no markdown, no prose around it) with exactly these keys:
  why_this_matters            -- 1-2 sentences: why this filing signals a need for Stilta.
  suggested_outreach_angle    -- 1 sentence: the specific pitch angle for this lead.
  email_subject               -- under 60 chars; defendant-perspective framing; no caption, no patent numbers.
  email_body                  -- the full email, greeting through sign-off.
"""


def build_user_prompt(lead: dict) -> str:
    s = lead.get("signals", {})
    g = lead.get("grounding", {})
    npe_line = "yes -- " + "; ".join(s.get("npe_reasons", [])) if s.get("npe_plaintiff") else "no"
    patents_list = g.get("patents", [])
    if patents_list:
        patent_facts = (f"EXACTLY {len(patents_list)} asserted patent(s); the ONLY patent numbers "
                        f"you may mention are: {', '.join(patents_list)}. You may say there are "
                        f"{len(patents_list)}. Mention no other number and no other count.")
    else:
        patent_facts = ("NOT available. Refer to them only as 'the asserted patents'. Do NOT state "
                        "how many there are (no digit, no number word) and do NOT mention any "
                        "patent number.")
    snippet = g.get("complaint_snippet") or "(complaint text not ingested yet)"
    return (
        "Draft outreach for this newly-filed patent case.\n\n"
        f"- Defendant (our prospect / the company to help): {lead.get('company')}\n"
        f"- Plaintiff (who sued them): {lead.get('plaintiff')}\n"
        f"- Plaintiff looks like an NPE/patent troll: {npe_line}\n"
        f"- Court / venue: {lead.get('court')} (court id {lead.get('court_id')})\n"
        f"- Judge: {lead.get('judge') or '(unassigned)'}\n"
        f"- Date filed: {lead.get('event_date')}\n"
        f"- Cause: {lead.get('cause')}\n"
        f"- Asserted patents: {patent_facts}\n"
        f"- Complaint excerpt: {snippet}\n\n"
        "Write to the defendant's in-house IP/legal decision-maker. Do not invent or "
        "infer any patent number, patent count, product, or name not listed above, "
        "even if you could guess it from the plaintiff or the complaint excerpt. "
        f"If you write a case caption (e.g. in the subject), put the PLAINTIFF first then the "
        f"DEFENDANT (here {lead.get('plaintiff')} sued {lead.get('company')}); never reverse "
        f"that order. Shorten each party to a recognizable short name so the subject stays "
        f"under ~70 characters."
    )


def _anthropic_call(system: str, user: str, api_key: str, model: str,
                    max_tokens: int = 900, temperature: float = 0.6) -> str:
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_URL, data=payload, method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"Anthropic API {err.code}: {body}") from None
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def _extract_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?", "", text.strip()).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if not m:
            raise ValueError(f"model did not return JSON: {text[:200]!r}")
        return json.loads(m.group(0))


EM_EN_DASH_RE = re.compile(r"[ \t]*[—–―‒][ \t]*")  # em/en/bar/figure dash
RECIPIENT_NAME_RE = re.compile(r"\[[^\]]*\bname\b[^\]]*\]", re.IGNORECASE)
STILTA_TYPO_RE = re.compile(r"\bSti(?:i+ta|[il]{2,}ta|t+a)\b", re.IGNORECASE)
SMART_TYPOGRAPHY = {
    "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...",
}


def sanitize_draft_text(text: str) -> str:
    """Deterministic house-style guardrail on model output (does not rely on the
    LLM obeying): no em/en dashes (they MIME-encode and read AI-written) and no
    recipient-name placeholder (we address a role, not a person). Result is plain
    ASCII so the .eml never needs quoted-printable. Sign-off is always the hardcoded
    'Best regards,\nAstrid Atle' — no placeholder needed."""
    if not text:
        return text
    text = RECIPIENT_NAME_RE.sub("", text)           # drop any "[...name...]" placeholder
    text = STILTA_TYPO_RE.sub("Stilta", text)        # fix "Stiita" and similar typos
    text = EM_EN_DASH_RE.sub(", ", text)             # unicode em/en dashes -> comma
    text = re.sub(r"[ \t]*--+[ \t]*", ", ", text)    # ASCII '--' used as a dash -> comma
    for bad, good in SMART_TYPOGRAPHY.items():
        text = text.replace(bad, good)
    text = re.sub(r"\b(Hi|Hello|Dear)[ \t]+,", r"\1,", text)   # "Hi ," -> "Hi,"
    text = re.sub(r"\b(Hi|Hello|Dear),[ \t]*,", r"\1,", text)  # "Hi, ," -> "Hi,"
    text = re.sub(r",[ \t]*,", ", ", text)                     # ", ," -> ", "
    text = re.sub(r"[ \t]{2,}", " ", text)                     # collapse spaces
    text = re.sub(r"[ \t]+\n", "\n", text)                     # strip trailing spaces
    return text.strip()


PATENT_NUM_RE = re.compile(r"US\s?\d{7,8}|\b\d{1,2},\d{3},\d{3}\b|\bRE\d{5,6}\b", re.IGNORECASE)
_NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_COUNT_CLAIM_RE = re.compile(
    r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"[\s-]+(?:asserted\s+|separate\s+|different\s+)?"
    r"patent(?:s|[\s-](?:complaint|suit|campaign|case|infringement))?\b",
    re.IGNORECASE,
)


def _grounded_keys(patents: list) -> set:
    return {re.sub(r"\D", "", p) for p in patents}


def _claimed_counts(text: str) -> set:
    out = set()
    for m in _COUNT_CLAIM_RE.finditer(text):
        tok = m.group(1).lower()
        out.add(int(tok) if tok.isdigit() else _NUMWORDS.get(tok))
    out.discard(None)
    return out


def patent_grounding_violations(fields: dict, patents: list) -> list:
    """Deterministic grounding check: no patent number that isn't in `patents`, and
    no patent COUNT other than exactly len(patents) (none at all when empty)."""
    grounded = _grounded_keys(patents)
    text = " ".join(fields[k] for k in REQUIRED_KEYS)
    problems = []
    bad_nums = sorted({m for m in PATENT_NUM_RE.findall(text)
                       if len(re.sub(r"\D", "", m)) >= 6 and re.sub(r"\D", "", m) not in grounded})
    if bad_nums:
        problems.append(f"ungrounded patent number(s): {bad_nums}")
    counts = _claimed_counts(text)
    if not patents and counts:
        problems.append(f"states patent count {sorted(counts)} but none are grounded")
    if patents and any(c != len(patents) for c in counts):
        problems.append(f"states patent count {sorted(counts)} != grounded {len(patents)}")
    return problems


def _force_patent_safe(fields: dict, patents: list) -> dict:
    """Last-resort deterministic scrub if the model still won't comply after retries:
    replace ungrounded numbers and wrong/empty counts with 'the asserted patents'."""
    grounded = _grounded_keys(patents)
    n = len(patents)

    def _num_sub(m):
        return m.group(0) if re.sub(r"\D", "", m.group(0)) in grounded else "the asserted patents"

    def _count_sub(m):
        tok = m.group(1).lower()
        val = int(tok) if tok.isdigit() else _NUMWORDS.get(tok)
        return m.group(0) if (patents and val == n) else "the asserted patents"

    out = {}
    for k, text in fields.items():
        text = PATENT_NUM_RE.sub(_num_sub, text)
        text = _COUNT_CLAIM_RE.sub(_count_sub, text)
        out[k] = re.sub(r"[ \t]{2,}", " ", text).strip()
    return out


def _retry_note(patents: list, plaintiff: str = "", defendant: str = "") -> str:
    parts = []
    if patents:
        parts.append(f"there are EXACTLY {len(patents)} asserted patents "
                     f"({', '.join(patents)}); state no other number and no other count")
    else:
        parts.append("the asserted patents are unknown; do NOT state a count or any patent "
                     "number, use only 'the asserted patents'")
    if plaintiff and defendant:
        parts.append(f"in any 'X v. Y' caption the plaintiff ({plaintiff}) comes first and the "
                     f"defendant ({defendant}) second; you may shorten the names; never reverse the order")
    parts.append("keep email_subject under 60 characters; frame it from the defendant's "
                 "perspective; no case caption, no patent numbers")
    return "STRICT REMINDER: " + "; ".join(parts) + "."


_CAPTION_STOP = {"inc", "llc", "ltd", "co", "corp", "plc", "lp", "llp", "group", "holdings",
                 "company", "international", "technologies", "technology", "the", "and", "of",
                 "dba", "vs", "v"}
_CAPTION_RE = re.compile(r"([A-Za-z0-9][^,:;|\n]*?)\s+(vs?\.)\s+([A-Za-z0-9][^,:;|\n]*)", re.IGNORECASE)


def _name_sig(name: str) -> set:
    """Distinctive lowercase tokens of a party name (drops suffixes/stopwords)."""
    return {t for t in re.findall(r"[a-z0-9]+", (name or "").lower())
            if len(t) >= 2 and t not in _CAPTION_STOP}


def caption_order_violations(fields: dict, plaintiff: str, defendant: str) -> list:
    """Flag any 'X v. Y' caption whose order is reversed (defendant before plaintiff)."""
    pl, de = _name_sig(plaintiff), _name_sig(defendant)
    problems = []
    for key in REQUIRED_KEYS:
        for m in _CAPTION_RE.finditer(fields[key]):
            ls, rs = _name_sig(m.group(1)), _name_sig(m.group(3))
            if (ls & de) and (rs & pl) and len(ls & de) > len(ls & pl) and len(rs & pl) > len(rs & de):
                problems.append(f"{key}: reversed caption '{m.group(0).strip()}'")
    return problems


def _fix_caption_order(fields: dict, plaintiff: str, defendant: str) -> dict:
    """Last-resort deterministic swap, applied ONLY when each side is cleanly a party
    name (its tokens are a subset of that party's tokens), so text is never mangled."""
    pl, de = _name_sig(plaintiff), _name_sig(defendant)

    def repl(m):
        left, sep, right = m.group(1).strip(), m.group(2), m.group(3).strip()
        ls, rs = _name_sig(left), _name_sig(right)
        if ls and rs and ls <= de and rs <= pl:   # both sides are purely a party name
            return f"{right} {sep} {left}"
        return m.group(0)

    return {k: _CAPTION_RE.sub(repl, v) for k, v in fields.items()}


def _truncate_subject(fields: dict, limit: int = 65) -> dict:
    """Deterministic safety net: keep the subject within `limit` chars, trimmed at a
    word boundary (the prompt already targets ~70; this guarantees the ceiling)."""
    subject = fields.get("email_subject", "")
    if len(subject) <= limit:
        return fields
    trimmed = subject[:limit].rsplit(" ", 1)[0].rstrip(" ,:;-")
    return {**fields, "email_subject": trimmed}


def generate(lead: dict, api_key: str, model: str = DEFAULT_MODEL, max_attempts: int = 3) -> dict:
    """The single LLM generation step, with bounded retry + deterministic fixes so a
    fabricated patent number/count or a reversed 'plaintiff v. defendant' caption can
    never ship. Returns sanitized fields."""
    patents = lead.get("grounding", {}).get("patents", []) or []
    plaintiff, defendant = lead.get("plaintiff", ""), lead.get("company", "")
    base = build_user_prompt(lead)
    fields = None
    for attempt in range(max_attempts):
        user = base if attempt == 0 else base + "\n\n" + _retry_note(patents, plaintiff, defendant)
        raw = _anthropic_call(SYSTEM_PROMPT, user, api_key, model)
        parsed = _extract_json(raw)
        missing = [k for k in REQUIRED_KEYS if not str(parsed.get(k, "")).strip()]
        if missing:
            raise ValueError(f"draft missing required keys: {missing}")
        fields = {k: sanitize_draft_text(str(parsed[k]).strip()) for k in REQUIRED_KEYS}
        if not patent_grounding_violations(fields, patents) and \
                not caption_order_violations(fields, plaintiff, defendant):
            return _truncate_subject(fields)
    # deterministic last-resort fixes after retries are exhausted
    fields = _force_patent_safe(fields, patents)
    fields = _fix_caption_order(fields, plaintiff, defendant)
    return _truncate_subject(fields)


def dry_run_generate(lead: dict) -> dict:
    """Deterministic placeholder for plumbing tests only (NO LLM, clearly marked).
    Never used for real output -- run.py routes these to dev-only files."""
    comp, pl, court = lead.get("company"), lead.get("plaintiff"), lead.get("court")
    pats = ", ".join(lead.get("grounding", {}).get("patents", [])) or "the asserted patents"
    return {
        "why_this_matters": f"[DRY-RUN] {comp} was just sued for patent infringement by "
                            f"{pl} in {court}; a fresh suit is a strong trigger for invalidity support.",
        "suggested_outreach_angle": f"[DRY-RUN] Offer rapid invalidity/prior-art analysis on "
                                    f"{pats} to support {comp}'s defense.",
        "email_subject": f"[DRY-RUN] {pl} v. {comp}: invalidity support",
        "email_body": (f"Hi,\n\n[DRY-RUN PLACEHOLDER. No LLM was called.]\n\n"
                       f"{comp} was named in a patent suit by {pl} in {court}. "
                       f"Stilta can help with rapid invalidity research.\n\nBest regards,\nAstrid Atle"),
    }
