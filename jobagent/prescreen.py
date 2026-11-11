"""Cheap, deterministic filtering before anything is sent to the model.

Three stages:
  1. dedupe across sources (same title + company)
  2. reject on title keywords (seniority, excluded functions)
  3. require at least one must-have keyword in title or description
Everything rejected here is logged with a reason so you can tune the profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .profile import CandidateProfile
from .sources.base import Job


@dataclass
class ScreenResult:
    kept: list[Job]
    rejected: list[tuple[Job, str]]


def _has(term: str, text: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text) is not None


def prescreen(jobs: list[Job], profile: CandidateProfile) -> ScreenResult:
    seen: set[str] = set()
    kept, rejected = [], []
    reject_kw = [k.lower() for k in profile.reject_keywords]
    must_kw = [k.lower() for k in profile.must_have_keywords]
    exclude_sec = [s.lower() for s in profile.exclude_sectors]

    for j in jobs:
        if j.key in seen:
            rejected.append((j, "duplicate"))
            continue
        seen.add(j.key)

        title = j.title.lower()
        hit = next((k for k in reject_kw if _has(k, title)), None)
        if hit:
            rejected.append((j, f"title contains '{hit}'"))
            continue

        text = f"{title} {j.description.lower()}"
        if must_kw and not any(_has(k, text) for k in must_kw):
            rejected.append((j, "no must-have keyword"))
            continue

        # sector exclusion only on title + company, descriptions mention everything
        head = f"{title} {j.company.lower()}"
        sec = next((s for s in exclude_sec if _has(s, head)), None)
        if sec:
            rejected.append((j, f"excluded sector '{sec}'"))
            continue

        kept.append(j)
    return ScreenResult(kept=kept, rejected=rejected)
