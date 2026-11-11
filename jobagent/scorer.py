"""Score pre-screened jobs against the candidate profile with Claude, in batches.

Each job gets: fit (0-100), verdict, why it fits, why it doesn't, what the
application would need to emphasise, and a flag for 'CV currently fails a
hard requirement'. Batching ~8 jobs per call keeps cost low and gives the
model a comparison set, which produces better-calibrated scores than one at a time.
"""

from __future__ import annotations

import logging

from .llm import LLM
from .profile import CandidateProfile
from .sources.base import Job

log = logging.getLogger(__name__)

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "verdict": {"type": "string", "enum": ["apply", "consider", "stretch", "skip"]},
                    "role_type": {"type": "string",
                                  "description": "e.g. desk quant, XVA quant, risk quant, quant dev, strats, model validation, trading analyst, other"},
                    "fits": {"type": "array", "items": {"type": "string"}, "description": "2-4 concrete reasons, citing the CV."},
                    "gaps": {"type": "array", "items": {"type": "string"}, "description": "Concrete requirements the CV does not meet."},
                    "hard_blocker": {"type": "boolean", "description": "True if a stated must-have (e.g. PhD, 5+ years, specific language) is clearly not met."},
                    "emphasise": {"type": "string", "description": "One sentence: what the application should lead with."},
                    "seniority_match": {"type": "string", "enum": ["too_junior", "match", "too_senior"]},
                },
                "required": ["index", "fit", "verdict", "role_type", "fits", "gaps", "hard_blocker", "emphasise", "seniority_match"],
            },
        }
    },
    "required": ["results"],
}

SYSTEM = """You are a sceptical finance recruiter screening job listings for one candidate.
Score each listing for FIT: the probability-weighted value of this candidate applying, 0-100.

Calibration:
  85-100  realistic target, strong match, apply now
  65-84   worth applying, one or two gaps they can argue around
  40-64   stretch: a gap that will usually get them screened out, apply only if time allows
  0-39    skip: wrong seniority, wrong function, or a hard blocker

Rules:
- Seniority dominates. 'Senior', 'VP', 'Director', '5+ years', 'PhD required' for a candidate
  with ~1 year experience is a skip or at best a stretch, however good the skills match.
- Do not reward the listing for being in the target sector if the role itself is wrong.
- Cite the CV when stating fits. Cite the listing when stating gaps. No generic praise.
- 'hard_blocker' is for explicit stated must-haves the candidate clearly lacks, not for preferences.
- Insurance / actuarial roles are to be scored as skip unless the listing is explicitly a market-risk,
  ALM, investment or quant role that is a stepping stone toward the target sectors.
Return one result per listing, using the listing's index."""


def score_jobs(llm: LLM, profile: CandidateProfile, jobs: list[Job], batch_size: int = 8) -> list[dict]:
    brief = profile.scoring_brief()
    out: list[dict] = []
    for start in range(0, len(jobs), batch_size):
        batch = jobs[start:start + batch_size]
        listing_text = "\n\n".join(
            f"<listing index=\"{i}\">\nTITLE: {j.title}\nCOMPANY: {j.company}\nLOCATION: {j.location}\n"
            f"SALARY: {j.salary or 'n/a'}\nPOSTED: {j.posted or 'n/a'}\nSOURCE: {j.source}\n"
            f"DESCRIPTION: {j.short(2500)}\n</listing>"
            for i, j in enumerate(batch)
        )
        user = f"<candidate>\n{brief}\n</candidate>\n\n<listings>\n{listing_text}\n</listings>\n\nScore every listing."
        try:
            data = llm.structured(system=SYSTEM, user=user, schema=SCORE_SCHEMA, name="scores", max_tokens=6000)
        except Exception as e:  # noqa: BLE001
            log.error("scoring batch at %d failed: %s", start, e)
            continue
        by_idx = {r["index"]: r for r in data.get("results", [])}
        for i, j in enumerate(batch):
            r = by_idx.get(i)
            if not r:
                log.warning("no score returned for %s @ %s", j.title, j.company)
                continue
            r["job"] = j
            out.append(r)
        log.info("scored %d/%d", min(start + batch_size, len(jobs)), len(jobs))
    return out
