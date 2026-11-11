"""Reed.co.uk jobseeker API. Free key at https://www.reed.co.uk/developers/jobseeker. UK only."""

from __future__ import annotations

import logging
from typing import Iterable

from .base import Job, client, strip_html

log = logging.getLogger(__name__)


class ReedSource:
    name = "reed"

    def __init__(self, api_key: str, per_query: int = 50, max_days_old: int = 30):
        self.api_key = api_key
        self.per_query = per_query
        self.max_days_old = max_days_old

    def search(self, queries: Iterable[str], locations: Iterable[str]) -> list[Job]:
        jobs: list[Job] = []
        uk_locs = [l for l in locations if any(k in l.lower() for k in ("london", "uk", "united kingdom", "england", "edinburgh", "manchester"))]
        if not uk_locs:
            return jobs
        with client() as c:
            for loc in uk_locs:
                for q in queries:
                    try:
                        r = c.get(
                            "https://www.reed.co.uk/api/1.0/search",
                            params={"keywords": q, "locationName": loc, "distanceFromLocation": 15,
                                    "resultsToTake": self.per_query},
                            auth=(self.api_key, ""),
                        )
                        r.raise_for_status()
                    except Exception as e:  # noqa: BLE001
                        log.warning("reed %r failed: %s", q, e)
                        continue
                    for it in r.json().get("results", []):
                        sal = ""
                        if it.get("minimumSalary") or it.get("maximumSalary"):
                            sal = f"{it.get('minimumSalary', '?')}-{it.get('maximumSalary', '?')}"
                        jobs.append(Job(
                            source=self.name,
                            title=it.get("jobTitle", ""),
                            company=it.get("employerName", ""),
                            location=it.get("locationName", loc),
                            url=it.get("jobUrl", ""),
                            description=strip_html(it.get("jobDescription", "")),
                            salary=sal,
                            posted=_reed_date(it.get("date", "")),
                            external_id=str(it.get("jobId", "")),
                            extra={"query": q},
                        ))
        return jobs


def _reed_date(s: str) -> str:
    # Reed returns dd/mm/yyyy
    try:
        d, m, y = s.split("/")
        return f"{y}-{m}-{d}"
    except ValueError:
        return s
