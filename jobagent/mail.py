"""Mailbox sync: find replies to applications and classify them.

IMAP (read) + SMTP (optional self-notification). Gmail needs an app password:
https://myaccount.google.com/apppasswords (2-step verification must be on).
Outlook/Hotmail: outlook.office365.com / smtp.office365.com with your normal password
or an app password if 2FA is on.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage

from .llm import LLM

log = logging.getLogger(__name__)

RECRUIT_WORDS = re.compile(
    r"\b(interview|application|applied|position|role|vacanc|recruit|talent|careers?|hiring|candidate|"
    r"assessment|hackerrank|codility|offer|next steps?|screening|opportunit)", re.I)
NOISE_WORDS = re.compile(r"\b(job alert|jobs? for you|recommended jobs|new jobs|newsletter|unsubscribe|daily digest|webinar)\b", re.I)
STOP = {"the", "and", "plc", "ltd", "llp", "llc", "inc", "group", "capital", "partners", "management", "securities",
        "bank", "research", "technologies", "technology", "trading", "asset", "global", "holdings", "co", "uk", "limited"}


@dataclass
class MailMessage:
    msg_id: str
    from_name: str
    from_addr: str
    subject: str
    date: str          # ISO
    body: str


def company_tokens(name: str) -> list[str]:
    words = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]
    toks = [t for t in words if len(t) > 3 and t not in STOP]
    if not toks:   # short names like UBS, IMC, Man: keep them, plus the squashed full name
        toks = [t for t in words if t not in STOP and len(t) > 1] or [w for w in words if len(w) > 1]
        toks.append("".join(words))
    return toks


def _decode(s) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:  # noqa: BLE001
        return str(s or "")


def _body(msg) -> str:
    parts = []
    if msg.is_multipart():
        for p in msg.walk():
            ct = p.get_content_type()
            if ct == "text/plain" and not p.get("Content-Disposition", "").startswith("attachment"):
                try:
                    parts.append(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "ignore"))
                except Exception:  # noqa: BLE001
                    pass
        if not parts:
            for p in msg.walk():
                if p.get_content_type() == "text/html":
                    try:
                        from bs4 import BeautifulSoup
                        parts.append(BeautifulSoup(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "ignore"), "html.parser").get_text(" ", strip=True))
                    except Exception:  # noqa: BLE001
                        pass
    else:
        try:
            raw = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore")
            if msg.get_content_type() == "text/html":
                from bs4 import BeautifulSoup
                raw = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
            parts.append(raw)
        except Exception:  # noqa: BLE001
            pass
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return text[:6000]


class Mailbox:
    def __init__(self, host: str, user: str, password: str, folder: str = "INBOX", port: int = 993):
        self.host, self.user, self.password, self.folder, self.port = host, user, password, folder, port

    def test(self) -> str:
        with imaplib.IMAP4_SSL(self.host, self.port) as m:
            m.login(self.user, self.password)
            typ, data = m.select(self.folder, readonly=True)
            return f"OK, {data[0].decode()} messages in {self.folder}"

    def fetch_since(self, since: datetime, limit: int = 300) -> list[MailMessage]:
        out: list[MailMessage] = []
        with imaplib.IMAP4_SSL(self.host, self.port) as m:
            m.login(self.user, self.password)
            m.select(self.folder, readonly=True)
            typ, data = m.search(None, f'(SINCE "{since.strftime("%d-%b-%Y")}")')
            ids = data[0].split()[-limit:]
            for i in ids:
                typ, msgdata = m.fetch(i, "(RFC822)")
                if typ != "OK" or not msgdata or not msgdata[0]:
                    continue
                msg = email.message_from_bytes(msgdata[0][1])
                name, addr = email.utils.parseaddr(_decode(msg.get("From")))
                try:
                    dt = email.utils.parsedate_to_datetime(msg.get("Date"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:  # noqa: BLE001
                    dt = datetime.now(timezone.utc)
                if dt < since:
                    continue
                out.append(MailMessage(msg_id=(msg.get("Message-ID") or f"<{i.decode()}@{self.host}>").strip(),
                                       from_name=name, from_addr=addr.lower(), subject=_decode(msg.get("Subject")),
                                       date=dt.astimezone(timezone.utc).isoformat(timespec="seconds"), body=_body(msg)))
        return out


def candidate_matches(msg: MailMessage, applied: list[dict]) -> list[dict]:
    """applied: [{uid, company, title}] -> subset whose company tokens appear in the sender or subject."""
    hay = f"{msg.from_addr} {msg.from_name} {msg.subject}".lower()
    hits = []
    for a in applied:
        toks = company_tokens(a["company"])
        if any(t in hay for t in toks):
            hits.append(a)
    return hits


def looks_recruiting(msg: MailMessage) -> bool:
    if NOISE_WORDS.search(msg.subject) or NOISE_WORDS.search(msg.body[:500]):
        return False
    return bool(RECRUIT_WORDS.search(msg.subject) or RECRUIT_WORDS.search(msg.body[:800]))


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "is_about_application": {"type": "boolean", "description": "True if this email concerns a job application or recruiting conversation of the candidate's."},
            "job_uid": {"type": "string", "description": "uid of the applied job this is about, or empty if none / unsure."},
            "category": {"type": "string", "enum": ["interview", "assessment", "offer", "rejection", "acknowledgement", "request_info", "recruiter_outreach", "other"]},
            "summary": {"type": "string", "description": "One sentence, concrete: who wrote, what they want, any deadline."},
            "action_needed": {"type": "boolean"},
            "deadline": {"type": "string", "description": "ISO date if the email gives one, else empty."},
        }, "required": ["index", "is_about_application", "job_uid", "category", "summary", "action_needed", "deadline"]}}
    },
    "required": ["results"],
}

CLASSIFY_SYSTEM = """You triage a job-seeker's inbox. For each email decide whether it is about one of their job
applications (or inbound recruiter interest), which applied job it belongs to if any, and what kind of message it is.
Automated job-alert digests, marketing and newsletters are NOT about an application. An ATS auto-acknowledgement
('we received your application') is 'acknowledgement'. Be literal; do not infer an interview from a vague email."""


def classify(llm: LLM, msgs: list[MailMessage], applied: list[dict]) -> list[dict]:
    if not msgs:
        return []
    applied_txt = "\n".join(f"- uid={a['uid']} | {a['title']} at {a['company']} (applied {a.get('applied_at', '')[:10]})" for a in applied) or "(none)"
    out = []
    for start in range(0, len(msgs), 6):
        batch = msgs[start:start + 6]
        emails_txt = "\n\n".join(
            f"<email index=\"{i}\">\nFROM: {m.from_name} <{m.from_addr}>\nSUBJECT: {m.subject}\nDATE: {m.date}\nBODY: {m.body[:2500]}\n</email>"
            for i, m in enumerate(batch))
        user = f"<applied_jobs>\n{applied_txt}\n</applied_jobs>\n\n<emails>\n{emails_txt}\n</emails>\n\nClassify every email."
        try:
            data = llm.structured(system=CLASSIFY_SYSTEM, user=user, schema=CLASSIFY_SCHEMA, name="triage", max_tokens=3000)
        except Exception as e:  # noqa: BLE001
            log.error("mail classify failed: %s", e)
            continue
        by = {r["index"]: r for r in data.get("results", [])}
        for i, m in enumerate(batch):
            r = by.get(i)
            if r:
                r["msg"] = m
                out.append(r)
    return out


def send_self_notification(*, smtp_host: str, smtp_port: int, user: str, password: str, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)


def default_since(days: int = 30) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
