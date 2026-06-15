# Top 3 sources

## 1. CourtListener / RECAP — federal patent dockets (BUILT)
**Signal:** a newly-filed patent infringement complaint (nature-of-suit 830) is the
*earliest, highest-intent* trigger for Stilta — the moment a company is sued, it needs
invalidity / prior-art / IPR strategy, which is exactly Stilta's product. Maps directly
to both ICPs: the in-house IP/legal team being sued, and the litigation boutique that
will defend them.
**Why this one for the prototype:** free public Search API (no scraping), ~daily fresh
RECAP ingestion, and the records already carry the complaint text, asserted patent
numbers, venue, and judge — enough to qualify *and* to ground a specific draft without
extra calls. 44 patent cases in a single 7-day window on the sample run.
**Filterable to relevance:** `type=r`, `nature_of_suit=830`, `filed_after=today-7d`.

## 2. USPTO PTAB — IPR/PGR validity challenges (reasoning only)
**Signal:** an inter partes review or post-grant review petition means a patent's
validity is actively contested — a strong trigger for invalidity research and for both
the petitioner and patent-owner sides. PTAB has its own open API (PTAB Open Data / bulk
trial data) keyed on patent and party.
**Why not first:** PTAB activity is generally *downstream* of a district-court suit
(petitions often follow assertion), so it's a narrower, later trigger than a fresh
complaint — a strong complement, not the lead. Qualification would mirror the built
pipeline (operating-entity gate + NPE/known-petitioner signal); recipient would shift
toward the patent owner or petitioner depending on which side Stilta is selling.

## 3. SEC EDGAR — 8-K / 10-K material IP litigation (reasoning only)
**Signal:** when a public company discloses *material* patent litigation in an 8-K or
10-K risk section, it is, by its own admission, high-stakes — a premium in-house
IP/licensing lead. EDGAR full-text search + the submissions API are free.
**Why not first:** lower timeliness and volume than dockets (quarterly/event-driven),
and extraction is **narrative** — you must parse prose disclosures and disambiguate
which matter is material, which is noisier than a structured docket. Best as an
*enrichment/scoring* layer (does the defendant consider this material?) layered on top
of the CourtListener trigger rather than a standalone first source.

---
**Also considered, set aside:** a news API (NPE campaigns, licensing deals) — great for
narrative color but noisy and harder to attribute to a specific actionable matter;
USPTO **patent assignment** data (ownership transfers / portfolio building) — a slower,
lower-intent signal better suited to account research than a weekly outbound trigger.
