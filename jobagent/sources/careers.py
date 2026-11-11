"""Employer career pages via their ATS public endpoints.

Supported ATSs:
  greenhouse  https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true
  lever       https://api.lever.co/v0/postings/<slug>?mode=json
  workday     POST https://<host>/wday/cxs/<tenant>/<site>/jobs   (most big banks)

Targets come from companies.yaml. Slugs drift; a failing company is logged and skipped,
never fatal. Listings are filtered locally by keyword because these endpoints return
the whole board.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from .base import Job, client, strip_html

log = logging.getLogger(__name__)


class CareerPagesSource:
    name = "careers"

    def __init__(self, companies: list[dict], location_terms: list[str] | None = None):
        self.companies = companies
        self.location_terms = [t.lower() for t in (location_terms or [])]

    # ---- public -------------------------------------------------------------------------------
    def search(self, queries: Iterable[str], locations: Iterable[str]) -> list[Job]:
        terms = _query_terms(queries)
        loc_terms = self.location_terms or [l.lower() for l in locations]
        jobs: list[Job] = []
        with client(timeout=45.0) as c:
            for co in self.companies:
                ats = co.get("ats")
                try:
                    if ats == "greenhouse":
                        found = self._greenhouse(c, co)
                    elif ats == "lever":
                        found = self._lever(c, co)
                    elif ats == "workday":
                        found = self._workday(c, co, terms)
                    else:
                        log.warning("careers: unknown ats %r for %s", ats, co.get("name"))
                        continue
                except Exception as e:  # noqa: BLE001
                    log.warning("careers: %s failed: %s", co.get("name"), e)
                    continue
                kept = [j for j in found if _matches(j, terms, loc_terms)]
                log.info("careers: %s -> %d listings, %d kept", co.get("name"), len(found), len(kept))
                jobs.extend(kept)
        return jobs

    # ---- ATS adapters -------------------------------------------------------------------------
    def _greenhouse(self, c, co) -> list[Job]:
        r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{co['slug']}/jobs", params={"content": "true"})
        r.raise_for_status()
        out = []
        for it in r.json().get("jobs", []):
            out.append(Job(
                source=self.name, title=it.get("title", ""), company=co["name"],
                location=(it.get("location") or {}).get("name", ""),
                url=it.get("absolute_url", ""), description=strip_html(it.get("content", "")),
                posted=(it.get("updated_at") or "")[:10], external_id=str(it.get("id", "")),
                extra={"ats": "greenhouse"},
            ))
        return out

    def _lever(self, c, co) -> list[Job]:
        r = c.get(f"https://api.lever.co/v0/postings/{co['slug']}", params={"mode": "json"})
        r.raise_for_status()
        out = []
        for it in r.json():
            cats = it.get("categories") or {}
            out.append(Job(
                source=self.name, title=it.get("text", ""), company=co["name"],
                location=cats.get("location", ""), url=it.get("hostedUrl", ""),
                description=strip_html(it.get("descriptionPlain") or it.get("description", "")),
                external_id=str(it.get("id", "")), extra={"ats": "lever", "team": cats.get("team", "")},
            ))
        return out

    def _workday(self, c, co, terms: list[str]) -> list[Job]:
        """Workday needs a search term per request; we run one per query term and merge."""
        host, tenant, site = co["host"], co["tenant"], co["site"]
        base = f"https://{host}/wday/cxs/{tenant}/{site}"
        out: dict[str, Job] = {}
        for term in terms[:12]:   # cap requests per company
            r = c.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term},
                       headers={"Accept": "application/json", "Content-Type": "application/json"})
            if r.status_code != 200:
                log.debug("workday %s %r -> %s", co["name"], term, r.status_code)
                continue
            for it in r.json().get("jobPostings", []):
                path = it.get("externalPath", "")
                if path in out:
                    continue
                out[path] = Job(
                    source=self.name, title=it.get("title", ""), company=co["name"],
                    location=it.get("locationsText", ""), url=f"https://{host}/{site}{path}",
                    description=it.get("title", ""),   # detail fetch is a second call; title + bullets suffice for pre-screen
                    posted=it.get("postedOn", ""), external_id=path, extra={"ats": "workday", "query": term},
                )
        # fetch descriptions for the kept ones (cheap, usually < 20)
        for path, job in list(out.items()):
            try:
                d = c.get(f"{base}{path}", headers={"Accept": "application/json"})
                if d.status_code == 200:
                    info = d.json().get("jobPostingInfo", {})
                    job.description = strip_html(info.get("jobDescription", "")) or job.description
                    job.url = info.get("externalUrl") or job.url
            except Exception:  # noqa: BLE001
                pass
        return list(out.values())


# ---- helpers ----------------------------------------------------------------------------------
def _query_terms(queries: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for q in queries:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def _matches(job: Job, terms: list[str], loc_terms: list[str]) -> bool:
    text = f"{job.title} {job.description}".lower()
    hit = any(_term_in(t, text) for t in terms)
    if not hit:
        return False
    if loc_terms and job.location:
        return any(l in job.location.lower() for l in loc_terms)
    return True


def _term_in(term: str, text: str) -> bool:
    # all words of the term present (order-free), e.g. "quant analyst" matches "Quantitative Analyst"
    words = [w for w in re.split(r"\W+", term.lower()) if len(w) > 2]
    words = [w[:5] if w.startswith("quant") else w for w in words]   # quant ~ quantitative
    return all(w in text for w in words)
