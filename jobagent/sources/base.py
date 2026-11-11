"""Common job record and source interface."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Iterable, Protocol

import httpx

USER_AGENT = "jobagent/0.1 (+personal job search tool)"


@dataclass
class Job:
    source: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    salary: str = ""
    posted: str = ""           # ISO date string if known
    external_id: str = ""      # source's own id
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable dedupe key across sources: normalised title + company."""
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
        return hashlib.sha1(f"{norm(self.title)}|{norm(self.company)}".encode()).hexdigest()[:16]

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.external_id or self.url}"

    def to_dict(self) -> dict:
        return asdict(self)

    def short(self, n: int = 1500) -> str:
        d = re.sub(r"\s+", " ", self.description).strip()
        return d[:n] + ("..." if len(d) > n else "")


class Source(Protocol):
    name: str

    def search(self, queries: Iterable[str], locations: Iterable[str]) -> list[Job]: ...


def client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def strip_html(s: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)
