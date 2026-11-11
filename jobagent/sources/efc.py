"""eFinancialCareers scraper. Best finance-specific coverage, but it is HTML scraping of a site
with no public API: expect it to break when they change their markup, and keep request volume low.

Disable with `sources.efinancialcareers: false` in profile.yaml if it misbehaves.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterable
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from .base import Job, client

log = logging.getLogger(__name__)

SITE_FOR = {"london": "uk", "uk": "uk", "zurich": "ch", "geneva": "ch", "switzerland": "ch",
            "paris": "fr", "frankfurt": "de", "germany": "de", "amsterdam": "nl", "dublin": "ie"}
BASE = {"uk": "https://www.efinancialcareers.co.uk", "ch": "https://www.efinancialcareers.ch",
        "fr": "https://www.efinancialcareers.fr", "de": "https://www.efinancialcareers.de",
        "nl": "https://www.efinancialcareers.nl", "ie": "https://www.efinancialcareers.ie"}


class EFinancialCareersSource:
    name = "efinancialcareers"

    def __init__(self, delay_s: float = 2.0, max_queries: int = 12):
        self.delay_s = delay_s
        self.max_queries = max_queries

    def search(self, queries: Iterable[str], locations: Iterable[str]) -> list[Job]:
        jobs: list[Job] = []
        qs = list(queries)[: self.max_queries]
        with client(timeout=30.0) as c:
            c.headers["Accept"] = "text/html"
            for loc in locations:
                site = next((v for k, v in SITE_FOR.items() if k in loc.lower()), None)
                if not site:
                    continue
                for q in qs:
                    url = f"{BASE[site]}/jobs-{quote_plus(q.replace(' ', '-'))}/in-{quote_plus(loc.split(',')[0])}"
                    try:
                        r = c.get(url)
                        if r.status_code != 200:
                            log.warning("efc %s -> %s", url, r.status_code)
                            continue
                        jobs.extend(self._parse(r.text, q, loc, BASE[site]))
                    except Exception as e:  # noqa: BLE001
                        log.warning("efc %r failed: %s", q, e)
                    time.sleep(self.delay_s)
        return jobs

    def _parse(self, html: str, q: str, loc: str, base: str) -> list[Job]:
        """Try the JSON-LD job list first (stable), fall back to anchor scraping."""
        soup = BeautifulSoup(html, "html.parser")
        out: list[Job] = []
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except json.JSONDecodeError:
                continue
            items = data if isinstance(data, list) else [data]
            for d in items:
                if isinstance(d, dict) and d.get("@type") == "ItemList":
                    for el in d.get("itemListElement", []):
                        it = el.get("item", el)
                        if it.get("@type") == "JobPosting":
                            out.append(self._from_posting(it, q, loc))
                elif isinstance(d, dict) and d.get("@type") == "JobPosting":
                    out.append(self._from_posting(d, q, loc))
        if out:
            return out
        # fallback: any link that looks like a job detail page
        for a in soup.select("a[href*='/jobs-']"):
            href = a.get("href", "")
            title = a.get_text(" ", strip=True)
            if not title or not re.search(r"\.id\d+", href):
                continue
            out.append(Job(source=self.name, title=title, company="", location=loc,
                           url=href if href.startswith("http") else base + href,
                           external_id=re.search(r"\.id(\d+)", href).group(1), extra={"query": q, "thin": True}))
        return out

    def _from_posting(self, it: dict, q: str, loc: str) -> Job:
        org = it.get("hiringOrganization") or {}
        jl = it.get("jobLocation") or {}
        addr = (jl[0] if isinstance(jl, list) and jl else jl).get("address", {}) if jl else {}
        url = it.get("url", "")
        return Job(
            source=self.name, title=it.get("title", ""), company=org.get("name", ""),
            location=addr.get("addressLocality", loc) if isinstance(addr, dict) else loc,
            url=url, description=BeautifulSoup(it.get("description", ""), "html.parser").get_text(" ", strip=True),
            posted=(it.get("datePosted") or "")[:10],
            external_id=(re.search(r"\.id(\d+)", url).group(1) if re.search(r"\.id(\d+)", url) else url),
            extra={"query": q},
        )
