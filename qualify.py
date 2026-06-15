"""Qualify / filter step (deterministic, no LLM).

Input : raw CourtListener search results (from fetch_cases.py).
Output: the qualified subset, each enriched with the deterministic signals the
        later LLM step will turn into `why_this_matters` / `suggested_outreach_angle`.

Pipeline of decisions (all rule-based, explainable):

  1. Parse `caseName` ("Plaintiff v. Defendant") -> plaintiff / defendant.
     `company` = the DEFENDANT, derived here so it survives dockets where
     CourtListener has not yet ingested structured party data.

  2. HARD GATE -- keep only cases whose defendant is a real operating company.
     Drop: empty/unparseable, Schedule A / Doe mass-defendant cases,
     government entities, and individuals / unverifiable single-name defendants.

  3. PRIORITY SIGNALS (rank, and decide the noise line):
       - NPE / patent-troll plaintiff (name heuristics + repeat-filer in the batch)
       - patent-heavy venue (EDTX = txed, D. Del. = ded)
     A case QUALIFIES if it passes the gate AND shows at least one of:
     NPE plaintiff, priority venue, or repeat filer. That is the motivated line
     between a real lead and noise: an operating-company defendant alone is not
     enough -- there must be a troll/venue signal that Stilta's services map to.

The model never runs here. This step is fully deterministic and reproducible.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter

# --- venue -------------------------------------------------------------------
PRIORITY_VENUES = {"txed": "E.D. Tex.", "ded": "D. Del."}   # qualifies on its own
SECONDARY_VENUES = {"txwd": "W.D. Tex. (Waco)"}             # score boost only

# --- judges (light priority boost) ------------------------------------------
PATENT_JUDGE_HINTS = ("Gilstrap", "Albright", "Payne", "Connolly", "Andrews")

# --- corporate / entity recognition -----------------------------------------
CORP_RE = re.compile(
    r"\b("
    r"Inc|Incorporated|LLC|L\.L\.C|LLP|PLLC|Corp|Corporation|Co|Company|"
    r"LP|L\.P|Ltd|Limited|GmbH|AG|SE|PLC|N\.V|B\.V|S\.A|S\.p\.A|S\.r\.l|"
    r"AB|Oy|KG|Pte|Pty|K\.K|Sdn|Bhd|"
    r"Technolog(?:y|ies)|Systems?|Holdings?|Group|Industries|Laborator(?:y|ies)|"
    r"Labs|Networks?|Semiconductors?|Electronics|Pharmaceuticals?|Motors|"
    r"Products|Solutions|Software|International|Bancorp|Financial|Energy|"
    r"Airlines?|Foods?|Media|Studios|Stores|Retail"
    r")\b\.?",
    re.IGNORECASE,
)

GOV_RE = re.compile(
    r"\b(united states|u\.s\.a|department of|state of|commonwealth of|city of|"
    r"county of|secretary of|commissioner of|internal revenue|uspto|"
    r"patent and trademark office|director of the|board of|regents of)\b",
    re.IGNORECASE,
)

SCHEDULE_A_RE = re.compile(
    r"(schedule\s+[\"']?a|identified on schedule|the individuals|"
    r"the partnerships|unincorporated associations|the entities|"
    r"\bdoe\b|\bdoes\b|defendants identified)",
    re.IGNORECASE,
)

# --- NPE heuristics ----------------------------------------------------------
NPE_TOKEN_RE = re.compile(
    r"\b(IP|Licensing|Patents?|Ventures|Holdings|Innovations?|Acquisitions?|"
    r"Assets|Monetization)\b"
)
LLC_RE = re.compile(r"\b(LLC|L\.L\.C\.?|LP|L\.P\.?)\b", re.IGNORECASE)
OPERATING_MARKER_RE = re.compile(
    r"\b(Products|Sports|Coolers|Plastics|Laborator(?:y|ies)|Labs|Foods|"
    r"Pharmaceuticals|Motors|Electronics|Restaurant|Hotels|Airlines|Bank|"
    r"Insurance|Apparel|Beverage|Cosmetics|Brewing|Tools)\b",
    re.IGNORECASE,
)
SUFFIX_TOKEN_RE = re.compile(r"^(LLC|LP|Inc|Corp|Co|Ltd|LLLP|LLP|PLLC)$", re.IGNORECASE)

# --- patent-number extraction (grounding for the draft step) -----------------
PATENT_RE = re.compile(r"US\s?\d{7,8}|\b\d{1,2},\d{3},\d{3}\b|\bRE\d{5,6}\b", re.IGNORECASE)

SPLIT_RE = re.compile(r"\s+v\.?\s+|\s+vs\.?\s+", re.IGNORECASE)


def split_case_name(name: str) -> tuple[str, str]:
    """'Plaintiff v. Defendant' -> (plaintiff, defendant). Defendant '' if no 'v.'."""
    parts = SPLIT_RE.split(name or "", maxsplit=1)
    if len(parts) == 2:
        return _clean_party(parts[0]), _clean_party(parts[1])
    return _clean_party(name or ""), ""


def _clean_party(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s*,?\s*et al\.?\s*$", "", s, flags=re.IGNORECASE)
    return s.strip().strip(",").strip()


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation -> stable key for repeat-filer counting."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())).strip()


def classify_defendant(name: str) -> tuple[bool, str]:
    """Hard gate. Returns (is_operating_company, reason)."""
    n = (name or "").strip()
    if not n:
        return False, "no defendant parsed from caseName"
    if SCHEDULE_A_RE.search(n):
        return False, "Schedule A / Doe mass-defendant case"
    if GOV_RE.search(n):
        return False, "government entity"
    if CORP_RE.search(n):
        return True, "operating company (corporate indicator)"
    tokens = re.findall(r"[A-Za-z0-9&+]+", n)
    brandish = any(t.isupper() and len(t) >= 2 for t in tokens) or bool(re.search(r"[0-9&+]", n))
    if brandish:
        return True, "operating company (brand-style name)"
    if len(tokens) <= 2:
        return False, "individual or unverifiable defendant (no corporate indicator)"
    return True, "operating company (multi-word entity)"


def assess_plaintiff_npe(name: str, repeat_count: int) -> tuple[bool, list[str]]:
    # NPE label requires a troll-like NAME signal or a real CAMPAIGN (>=3 suits).
    # A corporate-named plaintiff filing 2 mirror suits (e.g. a competitor dispute)
    # is NOT a troll -- that case can still qualify on venue, just not as "NPE".
    reasons: list[str] = []
    if repeat_count >= 3:
        reasons.append(f"campaign filer: {repeat_count} patent suits in this run")
    if NPE_TOKEN_RE.search(name or ""):
        reasons.append("NPE-style name (IP/Licensing/Patents/Holdings/Ventures)")
    content = [t for t in re.findall(r"[A-Za-z0-9]+", name or "") if not SUFFIX_TOKEN_RE.match(t)]
    if LLC_RE.search(name or "") and len(content) >= 3 and not OPERATING_MARKER_RE.search(name or ""):
        reasons.append("shell-style multi-word LLC plaintiff")
    return bool(reasons), reasons


def venue_info(court_id: str) -> tuple[bool, int, str | None]:
    if court_id in PRIORITY_VENUES:
        return True, 2, PRIORITY_VENUES[court_id]
    if court_id in SECONDARY_VENUES:
        return False, 1, SECONDARY_VENUES[court_id]  # boosts score, does not qualify alone
    return False, 0, None


def extract_patents(case: dict) -> list[str]:
    seen, out = set(), []
    for doc in case.get("recap_documents", []) or []:
        text = " ".join(str(doc.get(k, "")) for k in ("description", "short_description", "snippet"))
        for m in PATENT_RE.findall(text):
            key = re.sub(r"\D", "", m)
            if len(key) >= 6 and key not in seen:
                seen.add(key)
                out.append(m.strip())
    return out[:8]


ADMIN_DOC_RE = re.compile(
    r"(Disclosure Statement|Civil Cover|Clerk'?s|Case Assigned|Summons|Receipt|"
    r"Notice of|Filing fee|Reassign)",
    re.IGNORECASE,
)


def complaint_snippet(case: dict) -> str:
    docs = case.get("recap_documents", []) or []
    # Prefer the actual complaint.
    for doc in docs:
        desc = (doc.get("description") or "").strip()
        if "COMPLAINT" in desc.upper():
            return re.sub(r"\s+", " ", desc)[:400]
    # Otherwise the longest substantive (non-administrative) description, if any.
    best = ""
    for doc in docs:
        desc = (doc.get("description") or "").strip()
        if not desc or ADMIN_DOC_RE.search(desc):
            continue
        if len(desc) > len(best):
            best = desc
    return re.sub(r"\s+", " ", best)[:400]


# --- defendant quality tier (fix 2: PRIMARY ranking driver) ------------------
# Lightweight, name-based stand-in for real firmographic enrichment (a v2 item).
# "Known facts" are encoded as a small curated set of well-known large/established
# operating companies, plus conservative large-signal and small-reseller name
# heuristics. Deterministic -- the heavy generator model never runs here.
KNOWN_LARGE = {
    "samsung", "apple", "google", "microsoft", "amazon", "intel", "qualcomm", "cisco",
    "nvidia", "broadcom", "micron", "ibm", "oracle", "sony", "panasonic", "huawei",
    "dji", "lenovo", "imperva", "pepperl+fuchs", "johnson controls", "fortinet", "cigna",
    "boe technology", "dormakaba", "avis budget", "sandisk", "western digital", "siemens",
    "bosch", "honeywell", "schneider", "texas instruments", "analog devices",
    "stmicroelectronics", "teltonika", "janus international", "meg energy", "nutramax",
    "graco", "conair", "motive technologies", "netlist", "murphy usa", "duluth",
}
LARGE_SIGNAL_RE = re.compile(
    r"\b(PLC|AG|SE|N\.?V|S\.?A|GmbH|International|Corporation|Electronics|Semiconductors?|"
    r"Pharmaceuticals?|Motors|Airlines?|Bancorp|Financial|Aerospace|Telecom)\b",
    re.IGNORECASE,
)
SMALL_RESELLER_RE = re.compile(
    r"\b(Arms|Tactical|Ammunition|Ammo|Guns?|Firearms?|Depot|Outfitters?|Sporting|Supply|"
    r"Sales|Trading|Store|Shop|Retail|Wholesale|Outlet|Sons)\b",
    re.IGNORECASE,
)
TIER_BASE = {"large": 30, "mid": 15, "small": 5}  # primary; secondary signals add <= 8


def classify_defendant_tier(name: str) -> tuple[str, str]:
    """Return (tier, reason); tier is 'large' | 'mid' | 'small'."""
    n = name or ""
    base = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+ ]", " ", n.lower())).strip()
    for known in KNOWN_LARGE:
        if re.search(r"\b" + re.escape(known) + r"\b", base):
            return "large", f"known large/established company ({known})"
    if SMALL_RESELLER_RE.search(n):
        return "small", "small-reseller name signal (retail/firearms/sales)"
    if LARGE_SIGNAL_RE.search(n):
        return "large", "large-company name signal (multinational / sector suffix)"
    return "mid", "no strong size signal (defaulted to mid)"


_SUFFIX_KEEP_UPPER = {"LLC", "INC", "LTD", "LLP", "PLLC", "PLC", "LP", "CO", "CORP",
                      "AG", "SE", "GMBH", "NV", "BV", "SA", "KG", "PTE", "PTY"}


def title_case_company(name: str) -> str:
    """Normalize a mostly-uppercase ('shouting') company name to title case for
    display/salutation (fix 4). Preserves short all-caps acronyms (<= 3 letters,
    e.g. DJI, SZ) and corporate suffixes; leaves already-mixed-case names untouched."""
    letters = [c for c in name if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.7:
        return name
    out = []
    for tok in name.split():
        core = tok.strip(".,;:()[]")
        if not (core.isalpha() and core.isupper()):
            out.append(tok)               # mixed/lowercase/punctuated -> leave as-is
        elif core.upper() in _SUFFIX_KEEP_UPPER:
            out.append(tok)               # corporate suffix -> keep upper
        elif len(core) <= 3:
            out.append(tok)               # short acronym (DJI, SZ) -> keep
        else:
            out.append(tok.capitalize())  # long all-caps word -> Title case
    return " ".join(out)


def deflood_campaigns(leads: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group qualified leads by plaintiff and de-flood NPE campaigns (fix 3).

    Default is one lead per case. Large-tier defendants are ALWAYS kept as their
    own per-case lead. For a given plaintiff, the low-tier (non-large) tail is
    collapsed into ONE representative (highest priority_score) only when that tail
    has 2+ cases; the representative records campaign_size and the other folded
    defendants so nothing is lost and a downstream agent can expand it.
    Returns (kept_leads, collapsed_records)."""
    groups: dict = {}
    for lead in leads:
        groups.setdefault(normalize_name(lead.get("plaintiff", "")), []).append(lead)

    kept, collapsed = [], []
    for group in groups.values():
        large = [l for l in group if l["signals"]["defendant_tier"] == "large"]
        tail = [l for l in group if l["signals"]["defendant_tier"] != "large"]
        kept.extend(large)  # large defendants are never collapsed
        if len(tail) >= 2:
            tail.sort(key=lambda l: (l["signals"]["priority_score"], l["event_date"]), reverse=True)
            rep, others = dict(tail[0]), tail[1:]
            rep["campaign"] = {
                "plaintiff": rep.get("plaintiff"),
                "campaign_size": len(group),
                "collapsed_cases": len(tail),
                "other_defendants": [
                    {"docket_id": o["docket_id"], "company": o["company"], "court_id": o["court_id"]}
                    for o in others
                ],
            }
            kept.append(rep)
            collapsed.extend({
                "docket_id": o["docket_id"], "company": o["company"],
                "plaintiff": o.get("plaintiff"), "represented_by": rep["docket_id"],
            } for o in others)
        else:
            kept.extend(tail)  # tail of 0 or 1: keep as-is
    kept.sort(key=lambda l: (l["signals"]["priority_score"], l["event_date"]), reverse=True)
    return kept, collapsed


def qualify_cases(cases: list[dict]) -> dict:
    # Pass 1: count plaintiff filings across the whole batch (campaign detection).
    plaintiff_counts: Counter = Counter()
    parsed = []
    for c in cases:
        plaintiff, defendant = split_case_name(c.get("caseName", ""))
        parsed.append((c, plaintiff, defendant))
        if plaintiff:
            plaintiff_counts[normalize_name(plaintiff)] += 1

    qualified, dropped = [], []
    for c, plaintiff, defendant in parsed:
        ok, gate_reason = classify_defendant(defendant)
        if not ok:
            dropped.append({
                "docket_id": c.get("docket_id"),
                "case_name": c.get("caseName"),
                "defendant": defendant,
                "drop_reason": gate_reason,
            })
            continue

        repeat = plaintiff_counts[normalize_name(plaintiff)] if plaintiff else 0
        npe, npe_reasons = assess_plaintiff_npe(plaintiff, repeat)
        prio_venue, venue_score, venue_label = venue_info(c.get("court_id", ""))

        qualifies = npe or prio_venue or repeat >= 3
        if not qualifies:
            dropped.append({
                "docket_id": c.get("docket_id"),
                "case_name": c.get("caseName"),
                "defendant": defendant,
                "drop_reason": "operating-company defendant but no NPE / priority-venue / campaign signal",
            })
            continue

        judge = c.get("assignedTo") or ""
        referred = c.get("referredTo") or ""
        judge_boost = 1 if any(h in judge or h in referred for h in PATENT_JUDGE_HINTS) else 0
        repeat_boost = 3 if repeat >= 5 else 2 if repeat >= 3 else 1 if repeat == 2 else 0
        tier, tier_reason = classify_defendant_tier(defendant)
        # Defendant quality is the PRIMARY driver; NPE/venue/judge are secondary (<= 8),
        # so a large defendant always outranks a small one regardless of the boosts.
        secondary = venue_score + (2 if npe else 0) + repeat_boost + judge_boost
        score = TIER_BASE[tier] + secondary

        qualified.append({
            "docket_id": c.get("docket_id"),
            "docket_number": c.get("docketNumber"),
            "company": title_case_company(defendant),  # title-cased for display (fix 4)
            "plaintiff": plaintiff,
            "event_date": c.get("dateFiled"),
            "court_id": c.get("court_id"),
            "court": c.get("court_citation_string") or c.get("court"),
            "judge": judge,
            "cause": c.get("cause"),
            "case_name": c.get("caseName"),
            "docket_url": "https://www.courtlistener.com" + (c.get("docket_absolute_url") or ""),
            "signals": {
                "defendant_tier": tier,
                "defendant_tier_reason": tier_reason,
                "npe_plaintiff": npe,
                "npe_reasons": npe_reasons,
                "priority_venue": prio_venue,
                "venue_label": venue_label,
                "repeat_filer_count": repeat,
                "judge_signal": bool(judge_boost),
                "priority_score": score,
            },
            "grounding": {
                "patents": extract_patents(c),
                "complaint_snippet": complaint_snippet(c),
            },
        })

    qualified.sort(key=lambda r: (r["signals"]["priority_score"], r["event_date"]), reverse=True)
    kept, collapsed = deflood_campaigns(qualified)

    drop_reasons = Counter(d["drop_reason"] for d in dropped)
    return {
        "fetched": len(cases),
        "qualified_before_deflood": len(qualified),
        "qualified_count": len(kept),
        "collapsed_count": len(collapsed),
        "dropped_count": len(dropped),
        "drop_reasons": dict(drop_reasons),
        "qualified": kept,
        "collapsed": collapsed,
        "dropped": dropped,
    }


def _print_report(result: dict) -> None:
    print("=" * 92)
    print("Qualify / filter step")
    print("=" * 92)
    print(f"fetched                  : {result['fetched']}")
    print(f"qualified (pre-deflood)  : {result.get('qualified_before_deflood', result['qualified_count'])}")
    print(f"collapsed campaign cases : {result.get('collapsed_count', 0)}")
    print(f"qualified (final leads)  : {result['qualified_count']}")
    print(f"dropped                  : {result['dropped_count']}")
    print("  drop reasons:")
    for reason, n in sorted(result["drop_reasons"].items(), key=lambda kv: -kv[1]):
        print(f"    {n:>2}  {reason}")
    if result["qualified_count"] == 0:
        print("\n!! WARNING: 0 qualified leads -- check the source/filters before drafting.")
    print()

    print("QUALIFIED leads (sorted by priority; tier is the primary driver):")
    print(f"{'#':>2}  {'score':>5}  {'tier':5}  {'court':6}  {'company (defendant)':34}  signals")
    print("-" * 92)
    for i, r in enumerate(result["qualified"], 1):
        s = r["signals"]
        flags = []
        if s["npe_plaintiff"]:
            flags.append("NPE")
        if s["priority_venue"]:
            flags.append("PRIO-VENUE")
        if s["repeat_filer_count"] >= 2:
            flags.append(f"x{s['repeat_filer_count']}")
        if s["judge_signal"]:
            flags.append("judge")
        comp = r["company"][:33] + ("…" if len(r["company"]) > 34 else "")
        camp = ""
        if r.get("campaign"):
            camp = (f"  [CAMPAIGN rep: {r['campaign']['campaign_size']} cases, "
                    f"+{len(r['campaign']['other_defendants'])} folded]")
        print(f"{i:>2}  {s['priority_score']:>5}  {s.get('defendant_tier',''):5}  {r['court_id']:6}  {comp:34}  "
              f"{', '.join(flags)}  <- {r['plaintiff'][:28]}{camp}")
    print()
    if result.get("collapsed"):
        print(f"Collapsed campaign cases ({len(result['collapsed'])}), folded into a representative:")
        for c in result["collapsed"]:
            print(f"  - {c['docket_id']}  {c['company'][:32]:32}  (plaintiff {c['plaintiff'][:22]}) "
                  f"-> rep {c['represented_by']}")
        print()

    print("Sample qualified record (full shape, top lead):")
    print("-" * 92)
    if result["qualified"]:
        print(json.dumps(result["qualified"][0], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    raw_path = os.path.join("data", "raw_search_results.json")
    if not os.path.exists(raw_path):
        sys.exit(f"Missing {raw_path}. Run `python3 fetch_cases.py` first.")
    bundle = json.load(open(raw_path, encoding="utf-8"))
    result = qualify_cases(bundle["results"])

    out_path = os.path.join("data", "qualified.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    _print_report(result)
    print(f"\nQualified subset written to {out_path} for the next step.")
