"""Fetch step: pull recent U.S. patent cases from the CourtListener v4 REST API.

This module is 100% deterministic. It does no filtering/qualification, no dedup,
no LLM work, and no email. It only retrieves the raw set of candidate cases that
later steps build on.

Source query (fixed):
  - CourtListener Search API, type=r (RECAP federal dockets)
  - nature_of_suit=830 (Patent)
  - filed_after = today - N days (default 7), so the run is designed for weekly use
  - ordered by filing date, newest first

Dependencies: NONE beyond the Python standard library (urllib, json). This is
deliberate -- it runs on any python3 with no `pip install` and no interpreter
mismatch.

Auth is optional: set COURTLISTENER_API_TOKEN (env var, or a KEY=VALUE line in a
local .env file) to get role-labeled party data and higher rate limits. The
public Search API also answers unauthenticated requests, with lower limits.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
NATURE_OF_SUIT = "830"  # Patent
SEARCH_TYPE = "r"        # RECAP dockets (one result per docket)

REQUEST_TIMEOUT = 30
PAGE_DELAY_SEC = 0.5     # be polite between pages
MAX_PAGES = 50           # safety cap against runaway pagination
MAX_RETRIES = 4
USER_AGENT = "stilta-lead-gen/0.1 (CourtListener fetch step)"


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader, no third-party dependency.

    A NON-EMPTY real environment variable wins over the file; a missing OR empty
    one is filled from the file. (Some harnesses pre-set keys such as
    ANTHROPIC_API_KEY to an empty string -- that must not shadow the .env value.)
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not os.environ.get(key):  # missing or empty -> take from .env
                os.environ[key] = value.strip().strip('"').strip("'")


def _build_headers(token: str | None) -> dict:
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Token {token}"
    return headers


def _get_json(url: str, headers: dict) -> dict:
    """GET JSON with retry/backoff on 429 (rate limit) and transient 5xx/URL errors."""
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 429 or err.code >= 500:
                time.sleep(min(60, 2 ** attempt))
                last_err = err
                continue
            raise  # 4xx other than 429 (e.g. 401 auth) is a real error -- surface it
        except (urllib.error.URLError, TimeoutError, socket.timeout) as err:
            # Network blip / read timeout (socket.timeout is NOT a URLError subclass).
            time.sleep(min(60, 2 ** attempt))
            last_err = err
            continue
    raise last_err or RuntimeError("request failed after retries")


def fetch_recent_patent_cases(
    days: int = 7,
    token: str | None = None,
    max_pages: int = MAX_PAGES,
) -> dict:
    """Return raw CourtListener search results for patent cases filed in the window.

    Output bundle:
      {
        "fetched_at":  ISO-8601 UTC timestamp of this run,
        "filed_after": "YYYY-MM-DD" lower bound used in the query,
        "days":        the lookback window,
        "pages":       number of API pages read,
        "fetched":     number of cases returned,
        "results":     list of raw search-result objects (unmodified),
      }
    """
    today = dt.datetime.now(dt.timezone.utc).date()
    filed_after = (today - dt.timedelta(days=days)).isoformat()

    headers = _build_headers(token)
    params = {
        "type": SEARCH_TYPE,
        "nature_of_suit": NATURE_OF_SUIT,
        "filed_after": filed_after,
        "order_by": "dateFiled desc",
    }
    url: str | None = SEARCH_URL + "?" + urllib.parse.urlencode(params)

    results: list[dict] = []
    pages = 0
    while url and pages < max_pages:
        data = _get_json(url, headers)
        results.extend(data.get("results", []))
        url = data.get("next")  # full URL incl. cursor + original filters
        pages += 1
        if url:
            time.sleep(PAGE_DELAY_SEC)

    return {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "filed_after": filed_after,
        "days": days,
        "pages": pages,
        "fetched": len(results),
        "results": results,
    }


def _print_sample(bundle: dict, authed: bool) -> None:
    results = bundle["results"]
    print("=" * 72)
    print("CourtListener fetch step")
    print("=" * 72)
    print(f"fetched_at   : {bundle['fetched_at']}")
    print(f"window       : filed on/after {bundle['filed_after']} "
          f"(last {bundle['days']} days)")
    print(f"auth token   : {'yes' if authed else 'no (public rate limit)'}")
    print(f"pages read   : {bundle['pages']}")
    print(f"fetched      : {bundle['fetched']} patent cases (NOS 830)")
    print()

    print(f"First {min(10, len(results))} cases (filed | court | docket no. | case name):")
    for r in results[:10]:
        name = r.get("caseName", "")
        if len(name) > 60:
            name = name[:57] + "..."
        print(f"  {r.get('dateFiled')} | {r.get('court_id'):>6} | "
              f"{r.get('docketNumber', ''):<14} | {name}")
    print()

    if results:
        print("Full raw shape of one result (results[0]):")
        print("-" * 72)
        print(json.dumps(results[0], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    load_dotenv()
    token = os.environ.get("COURTLISTENER_API_TOKEN")
    bundle = fetch_recent_patent_cases(days=7, token=token)

    os.makedirs("data", exist_ok=True)
    out_path = os.path.join("data", "raw_search_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2, ensure_ascii=False)

    _print_sample(bundle, authed=bool(token))
    print()
    print(f"Raw bundle written to {out_path} for inspection.")
