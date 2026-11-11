"""Adzuna job search API. Free key at https://developer.adzuna.com/ (250 calls/day on the free tier)."""

from __future__ import annotations

import logging
from typing import Iterable

from .base import Job, client

log = logging.getLogger(__name__)

# Adzuna country codes keyed by the location words the profile is likely to use.
COUNTRY_FOR = {
    "london": "gb", "uk": "gb", "united kingdom": "gb", "england": "gb", "edinburgh": "gb",
    "zurich": "ch", "geneva": "ch", "switzerland": "ch", "zürich": "ch",
    "paris": "fr", "france": "fr",
    "frankfurt": "de", "germany": "de", "munich": "de",
    "amsterdam": "nl", "netherlands": "nl",
    "dublin": "ie", "ireland": "ie",
    "milan": "it", "italy": "it",
    "new york": "us", "usa": "us", "united states": "us",
}


def country_code(location: str) -> str | None:
    loc = location.lower()
    for k, v in COUNTRY_FOR.items():
        if k in loc:
            return v
    return None


class AdzunaSource:
    name = "adzuna"

    def __init__(self, app_id: str, app_key: str, max_days_old: int = 30, per_query: int = 50):
        self.app_id, self.app_key = app_id, app_key
        self.max_days_old = max_days_old
        self.per_query = per_query

    def search(self, queries: Iterable[str], locations: Iterable[str]) -> list[Job]:
        jobs: list[Job] = []
        with client() as c:
            for loc in locations:
                cc = country_code(loc)
                if not cc:
                    log.warning("adzuna: no country mapping for %r, skipping", loc)
                    continue
                for q in queries:
                    url = f"https://api.adzuna.com/v1/api/jobs/{cc}/search/1"
                    params = {
                        "app_id": self.app_id, "app_key": self.app_key,
                        "what": q, "where": loc, "results_per_page": self.per_query,
                        "max_days_old": self.max_days_old, "content-type": "application/json",
                    }
                    try:
                        r = c.get(url, params=params)
                        r.raise_for_status()
                    except Exception as e:  # noqa: BLE001
                        log.warning("adzuna %s/%r failed: %s", cc, q, e)
                        continue
                    for it in r.json().get("results", []):
                        sal = ""
                        if it.get("salary_min") or it.get("salary_max"):
                            sal = f"{it.get('salary_min', '?')}-{it.get('salary_max', '?')}"
                        jobs.append(Job(
                            source=self.name,
                            title=it.get("title", ""),
                            company=(it.get("company") or {}).get("display_name", ""),
                            location=(it.get("location") or {}).get("display_name", loc),
                            url=it.get("redirect_url", ""),
                            description=it.get("description", ""),
                            salary=sal,
                            posted=(it.get("created") or "")[:10],
                            external_id=str(it.get("id", "")),
                            extra={"query": q, "category": (it.get("category") or {}).get("label", "")},
                        ))
        return jobs
