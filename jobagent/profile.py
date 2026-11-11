"""Build a structured candidate profile from CV text + aspirations.

The profile is what every later stage keys off: search queries for the
job boards, hard filters for the pre-screen, and the fit rubric the scorer
uses. It is produced once by a stronger model and cached to JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .llm import LLM

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "3-4 sentence candidate summary written for a recruiter."},
        "years_experience": {"type": "number"},
        "current_title": {"type": "string"},
        "current_sector": {"type": "string"},
        "hard_skills": {"type": "array", "items": {"type": "string"}},
        "domain_knowledge": {"type": "array", "items": {"type": "string"}},
        "strengths": {"type": "array", "items": {"type": "string"},
                      "description": "What makes this candidate credible for the target roles."},
        "gaps": {"type": "array", "items": {"type": "string"},
                 "description": "Honest gaps versus the target roles."},
        "target_roles": {"type": "array", "items": {"type": "string"},
                         "description": "Role titles to target, most realistic first."},
        "stretch_roles": {"type": "array", "items": {"type": "string"}},
        "target_sectors": {"type": "array", "items": {"type": "string"}},
        "exclude_sectors": {"type": "array", "items": {"type": "string"}},
        "locations": {"type": "array", "items": {"type": "string"}},
        "seniority": {"type": "string", "enum": ["intern", "graduate", "junior", "mid", "senior"]},
        "search_queries": {"type": "array", "items": {"type": "string"},
                           "description": "10-20 short job-board search strings, as a recruiter would title the role."},
        "must_have_keywords": {"type": "array", "items": {"type": "string"},
                               "description": "A listing should contain at least one of these to be worth scoring."},
        "reject_keywords": {"type": "array", "items": {"type": "string"},
                            "description": "Title keywords that mean an instant reject (e.g. senior, head of, director, actuarial)."},
        "long_term_goal": {"type": "string"},
    },
    "required": ["summary", "years_experience", "current_title", "hard_skills", "strengths", "gaps",
                 "target_roles", "stretch_roles", "target_sectors", "exclude_sectors", "locations",
                 "seniority", "search_queries", "must_have_keywords", "reject_keywords", "long_term_goal"],
}

SYSTEM = """You are a blunt careers analyst who specialises in finance and quant hiring in London and Europe.
You turn a CV plus a candidate's stated aspirations into a structured search profile.
Be realistic: a candidate with 1-2 years' experience should target junior / analyst / associate roles,
not VP roles, and a 'quant' target should be split into the sub-roles they can actually get
(e.g. desk quant analyst, XVA / CCR quant, risk quant, model validation, quant developer, strats)
rather than one vague label. Search queries must be phrased the way job boards title roles.
Gaps must be specific and honest; do not flatter."""


@dataclass
class CandidateProfile:
    summary: str
    years_experience: float
    current_title: str
    hard_skills: list[str]
    strengths: list[str]
    gaps: list[str]
    target_roles: list[str]
    stretch_roles: list[str]
    target_sectors: list[str]
    exclude_sectors: list[str]
    locations: list[str]
    seniority: str
    search_queries: list[str]
    must_have_keywords: list[str]
    reject_keywords: list[str]
    long_term_goal: str
    current_sector: str = ""
    domain_knowledge: list[str] = field(default_factory=list)
    cv_text: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, s: str) -> "CandidateProfile":
        d = json.loads(s)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "CandidateProfile":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def scoring_brief(self) -> str:
        """Compact text handed to the scorer with every batch of jobs."""
        return (
            f"CANDIDATE: {self.summary}\n"
            f"Experience: {self.years_experience} years, currently {self.current_title} ({self.current_sector}).\n"
            f"Seniority target: {self.seniority}.\n"
            f"Hard skills: {', '.join(self.hard_skills)}.\n"
            f"Domain: {', '.join(self.domain_knowledge)}.\n"
            f"Strengths: {'; '.join(self.strengths)}.\n"
            f"Gaps: {'; '.join(self.gaps)}.\n"
            f"Target roles: {', '.join(self.target_roles)}. Stretch: {', '.join(self.stretch_roles)}.\n"
            f"Target sectors: {', '.join(self.target_sectors)}. Avoid: {', '.join(self.exclude_sectors)}.\n"
            f"Locations: {', '.join(self.locations)}.\n"
            f"Long-term goal: {self.long_term_goal}"
        )


def build_profile(llm: LLM, cv_text: str, aspirations: dict, extra_text: str = "") -> CandidateProfile:
    user = (
        "CV:\n<cv>\n" + cv_text.strip() + "\n</cv>\n\n"
        + ("ADDITIONAL MATERIAL (e.g. LinkedIn export, project write-ups):\n<extra>\n" + extra_text.strip() + "\n</extra>\n\n" if extra_text.strip() else "")
        + "ASPIRATIONS AND CONSTRAINTS (from the candidate, in their words):\n<aspirations>\n"
        + json.dumps(aspirations, indent=2)
        + "\n</aspirations>\n\nProduce the search profile."
    )
    data = llm.structured(system=SYSTEM, user=user, schema=PROFILE_SCHEMA, name="candidate_profile", max_tokens=4000)
    prof = CandidateProfile.from_json(json.dumps(data))
    prof.cv_text = cv_text
    return prof
