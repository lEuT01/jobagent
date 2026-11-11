"""SQLite store so repeated runs only surface new listings and keep every score."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .sources.base import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid TEXT PRIMARY KEY,
    key TEXT,
    source TEXT, title TEXT, company TEXT, location TEXT, url TEXT,
    salary TEXT, posted TEXT, description TEXT,
    first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS scores (
    uid TEXT PRIMARY KEY,
    scored_at TEXT,
    fit INTEGER, verdict TEXT, payload TEXT,
    FOREIGN KEY(uid) REFERENCES jobs(uid)
);
CREATE TABLE IF NOT EXISTS status (
    uid TEXT PRIMARY KEY,
    status TEXT,        -- applied | ignored | saved
    note TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS replies (
    msg_id TEXT PRIMARY KEY,
    job_uid TEXT,
    from_name TEXT, from_addr TEXT, subject TEXT, date TEXT,
    category TEXT, summary TEXT, action_needed INTEGER, deadline TEXT,
    seen INTEGER DEFAULT 0, notified INTEGER DEFAULT 0, found_at TEXT
);
CREATE TABLE IF NOT EXISTS mail_seen (msg_id TEXT PRIMARY KEY, checked_at TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_jobs_key ON jobs(key);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- jobs ----
    def upsert_jobs(self, jobs: list[Job]) -> list[Job]:
        """Insert new jobs, bump last_seen on known ones. Returns the jobs never seen before."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new: list[Job] = []
        for j in jobs:
            row = self.conn.execute("SELECT uid FROM jobs WHERE uid=? OR key=?", (j.uid, j.key)).fetchone()
            if row:
                self.conn.execute("UPDATE jobs SET last_seen=? WHERE uid=?", (now, row["uid"]))
                continue
            self.conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (j.uid, j.key, j.source, j.title, j.company, j.location, j.url, j.salary, j.posted,
                 j.description, now, now),
            )
            new.append(j)
        self.conn.commit()
        return new

    # ---- scores ----
    def save_score(self, uid: str, fit: int, verdict: str, payload: dict) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?)",
                          (uid, now, fit, verdict, json.dumps(payload)))
        self.conn.commit()

    def unscored(self) -> list[Job]:
        rows = self.conn.execute(
            "SELECT j.* FROM jobs j LEFT JOIN scores s ON j.uid=s.uid WHERE s.uid IS NULL").fetchall()
        return [_row_to_job(r) for r in rows]

    def top(self, min_fit: int = 0, limit: int = 50, since: str | None = None, exclude_status: bool = True) -> list[dict]:
        q = ("SELECT j.*, s.fit, s.verdict, s.payload, s.scored_at, st.status FROM jobs j "
             "JOIN scores s ON j.uid=s.uid LEFT JOIN status st ON j.uid=st.uid WHERE s.fit>=?")
        args: list = [min_fit]
        if since:
            q += " AND s.scored_at>=?"
            args.append(since)
        if exclude_status:
            q += " AND (st.status IS NULL OR st.status='saved')"
        q += " ORDER BY s.fit DESC, j.posted DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.conn.execute(q, args).fetchall():
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            out.append(d)
        return out

    def set_status(self, uid: str, status: str, note: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute("INSERT OR REPLACE INTO status VALUES (?,?,?,?)", (uid, status, note, now))
        self.conn.commit()

    # ---- mail ----
    def applied_jobs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT j.uid, j.title, j.company, st.updated_at AS applied_at FROM jobs j JOIN status st ON j.uid=st.uid "
            "WHERE st.status='applied' ORDER BY st.updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def mail_already_checked(self, msg_ids: list[str]) -> set[str]:
        if not msg_ids:
            return set()
        q = ",".join("?" * len(msg_ids))
        return {r[0] for r in self.conn.execute(f"SELECT msg_id FROM mail_seen WHERE msg_id IN ({q})", msg_ids)}

    def mark_mail_checked(self, msg_ids: list[str]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.executemany("INSERT OR IGNORE INTO mail_seen VALUES (?,?)", [(m, now) for m in msg_ids])
        self.conn.commit()

    def save_reply(self, r: dict) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT OR REPLACE INTO replies (msg_id, job_uid, from_name, from_addr, subject, date, category, summary, "
            "action_needed, deadline, seen, notified, found_at) VALUES (?,?,?,?,?,?,?,?,?,?,"
            "COALESCE((SELECT seen FROM replies WHERE msg_id=?),0), COALESCE((SELECT notified FROM replies WHERE msg_id=?),0), ?)",
            (r["msg_id"], r.get("job_uid") or "", r["from_name"], r["from_addr"], r["subject"], r["date"], r["category"],
             r["summary"], int(bool(r["action_needed"])), r.get("deadline") or "", r["msg_id"], r["msg_id"], now))
        self.conn.commit()

    def replies(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT r.*, j.title AS job_title, j.company AS job_company FROM replies r LEFT JOIN jobs j ON r.job_uid=j.uid "
            "ORDER BY r.date DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def replies_for(self, job_uid: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM replies WHERE job_uid=? ORDER BY date DESC", (job_uid,))]

    def unnotified_replies(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM replies WHERE notified=0 ORDER BY date DESC")]

    def mark_replies(self, msg_ids: list[str], *, seen: bool | None = None, notified: bool | None = None) -> None:
        for m in msg_ids:
            if seen is not None:
                self.conn.execute("UPDATE replies SET seen=? WHERE msg_id=?", (int(seen), m))
            if notified is not None:
                self.conn.execute("UPDATE replies SET notified=? WHERE msg_id=?", (int(notified), m))
        self.conn.commit()

    def link_reply(self, msg_id: str, job_uid: str) -> None:
        self.conn.execute("UPDATE replies SET job_uid=? WHERE msg_id=?", (job_uid, msg_id))
        self.conn.commit()

    def meta_get(self, k: str) -> str | None:
        r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def meta_set(self, k: str, v: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
        self.conn.commit()

    def find_uid(self, prefix: str) -> str | None:
        rows = self.conn.execute("SELECT uid FROM jobs WHERE uid LIKE ?", (f"%{prefix}%",)).fetchall()
        return rows[0]["uid"] if len(rows) == 1 else None

    def counts(self) -> dict:
        c = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {"jobs": c("SELECT COUNT(*) FROM jobs"), "scored": c("SELECT COUNT(*) FROM scores"),
                "applied": c("SELECT COUNT(*) FROM status WHERE status='applied'"),
                "replies": c("SELECT COUNT(*) FROM replies"), "unseen_replies": c("SELECT COUNT(*) FROM replies WHERE seen=0"),
                "action_needed": c("SELECT COUNT(*) FROM replies WHERE action_needed=1 AND seen=0")}


def _row_to_job(r: sqlite3.Row) -> Job:
    uid: str = r["uid"]
    ext = uid.split(":", 1)[1] if ":" in uid else ""
    return Job(source=r["source"], title=r["title"], company=r["company"], location=r["location"],
               url=r["url"], description=r["description"] or "", salary=r["salary"] or "",
               posted=r["posted"] or "", external_id=ext if ext != r["url"] else "")
