"""Draft sinks -- where a generated email is pushed AS A DRAFT.

IMPORTANT: there is deliberately NO send capability in this file or anywhere in
the project. The only operations are:
  - EmlSink        : write an .eml file to disk (a file cannot send itself).
  - GmailDraftSink : POST to Gmail `users.drafts.create` (creates a draft only).
Neither calls SMTP, `messages.send`, or any transport that delivers mail.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import format_datetime
import datetime as dt

_TOKEN_URL = "https://oauth2.googleapis.com/token"


def mint_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange a refresh_token for a short-lived access_token (stdlib urllib only).
    Called once per run — tokens last ~1 hour, plenty for a single batch."""
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(
        _TOKEN_URL, data=body, method="POST",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"OAuth token mint {err.code}: {detail}") from None
    if "access_token" not in data:
        raise RuntimeError(f"No access_token in response: {data}")
    return data["access_token"]


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")[:48]


def build_message(lead: dict, draft: dict, from_addr: str, to_addr: str) -> EmailMessage:
    """Build an RFC 5322 message. `To` is a TEST mailbox; the intended human
    recipient (defendant in-house IP/legal) is recorded in a header + the body."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr  # test mailbox only -- never the real prospect
    msg["Subject"] = draft["email_subject"]
    msg["Date"] = format_datetime(dt.datetime.now(dt.timezone.utc))
    msg["X-Stilta-Docket-Id"] = str(lead.get("docket_id"))
    msg["X-Stilta-Intended-Recipient"] = f"In-house IP/Legal: {lead.get('company')}"
    msg["X-Stilta-Source"] = "CourtListener NOS-830 trigger"
    body = draft["email_body"]
    if body.isascii():
        # Plain ASCII -> emit as 7bit so the raw .eml reads as clean text
        # (no quoted-printable '=' soft-wraps or encoded sequences).
        msg.set_content(body, cte="7bit")
    else:
        msg.set_content(body)  # non-ASCII -> let email choose a safe encoding
    return msg


class EmlSink:
    """Primary sink: writes a draft as an .eml file. Guarantees 'never sent'."""

    name = "eml"

    def __init__(self, out_dir: str, from_addr: str, to_addr: str):
        self.out_dir = out_dir
        self.from_addr = from_addr
        self.to_addr = to_addr
        os.makedirs(out_dir, exist_ok=True)

    def push(self, lead: dict, draft: dict) -> dict:
        msg = build_message(lead, draft, self.from_addr, self.to_addr)
        fname = f"{lead.get('docket_id')}-{_slug(lead.get('company', ''))}.eml"
        path = os.path.join(self.out_dir, fname)
        with open(path, "wb") as fh:
            fh.write(bytes(msg))
        return {"sink": self.name, "ref": path}


class GmailDraftSink:
    """Optional sink: creates a real Gmail DRAFT via the API. No send anywhere.

    Auto-mints an access_token from (client_id, client_secret, refresh_token)
    so the pipeline works unattended in cron — no pre-obtained token needed.
    Stdlib urllib only; no google client libraries."""

    name = "gmail"
    DRAFTS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 from_addr: str, to_addr: str):
        self.token = mint_access_token(client_id, client_secret, refresh_token)
        self.from_addr = from_addr
        self.to_addr = to_addr

    def push(self, lead: dict, draft: dict) -> dict:
        msg = build_message(lead, draft, self.from_addr, self.to_addr)
        raw = base64.urlsafe_b64encode(bytes(msg)).decode("ascii")
        body = json.dumps({"message": {"raw": raw}}).encode("utf-8")
        req = urllib.request.Request(
            self.DRAFTS_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}", "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Gmail drafts.create {err.code}: {detail}") from None
        return {"sink": self.name, "ref": data.get("id")}
