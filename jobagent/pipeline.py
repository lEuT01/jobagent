"""The run pipeline, callable from the CLI and the web server."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings, load_yaml
from .llm import LLM
from .prescreen import prescreen
from .profile import CandidateProfile
from .scorer import score_jobs
from .sources import AdzunaSource, CareerPagesSource, EFinancialCareersSource, ReedSource
from .sources.base import Job
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    fetched: int = 0
    kept: int = 0
    new: int = 0
    scored: int = 0
    per_source: dict = field(default_factory=dict)
    rejects: list = field(default_factory=list)   # (title, company, reason)
    usage: str = ""
    errors: list = field(default_factory=list)


def build_sources(cfg: dict, settings: Settings, companies_path: Path, log_fn: Callable[[str], None] = log.info) -> list:
    enabled = cfg.get("sources", {})
    out = []
    if enabled.get("adzuna", True):
        if settings.adzuna_app_id and settings.adzuna_app_key:
            out.append(AdzunaSource(settings.adzuna_app_id, settings.adzuna_app_key))
        else:
            log_fn("Adzuna skipped: no API keys set")
    if enabled.get("reed", True):
        if settings.reed_api_key:
            out.append(ReedSource(settings.reed_api_key))
        else:
            log_fn("Reed skipped: no API key set")
    if enabled.get("careers", True) and companies_path.exists():
        cos = load_yaml(companies_path).get("companies", [])
        out.append(CareerPagesSource(cos, location_terms=cfg.get("careers_location_terms")))
    if enabled.get("efinancialcareers", False):
        out.append(EFinancialCareersSource())
    return out


def run_pipeline(*, cfg: dict, settings: Settings, profile: CandidateProfile, store: Store, companies_path: Path,
                 score: bool = True, limit: int = 0, log_fn: Callable[[str], None] = log.info,
                 progress: Callable[[int, int], None] | None = None) -> RunStats:
    stats = RunStats()
    srcs = build_sources(cfg, settings, companies_path, log_fn)
    if not srcs:
        stats.errors.append("No sources configured. Add API keys or enable career pages.")
        return stats

    fetched: list[Job] = []
    for s in srcs:
        log_fn(f"Fetching from {s.name} ...")
        try:
            got = s.search(profile.search_queries, profile.locations)
        except Exception as e:  # noqa: BLE001
            log_fn(f"{s.name} failed: {e}")
            stats.errors.append(f"{s.name}: {e}")
            got = []
        stats.per_source[s.name] = len(got)
        log_fn(f"{s.name}: {len(got)} listings")
        fetched.extend(got)
    stats.fetched = len(fetched)

    screen = prescreen(fetched, profile)
    stats.kept = len(screen.kept)
    stats.rejects = [(j.title, j.company, why) for j, why in screen.rejected]
    new = store.upsert_jobs(screen.kept)
    stats.new = len(new)
    log_fn(f"{stats.fetched} fetched, {stats.kept} passed pre-screen, {stats.new} new")

    to_score = store.unscored()
    if limit:
        to_score = to_score[:limit]
    if to_score and score:
        if not settings.anthropic_api_key:
            stats.errors.append("ANTHROPIC_API_KEY not set; listings saved but not scored.")
            return stats
        llm = LLM(settings.anthropic_api_key, settings.score_model)
        log_fn(f"Scoring {len(to_score)} listings with {settings.score_model} ...")
        done = 0
        batch = 8
        for start in range(0, len(to_score), batch):
            chunk = to_score[start:start + batch]
            for r in score_jobs(llm, profile, chunk, batch_size=batch):
                j = r.pop("job")
                store.save_score(j.uid, int(r["fit"]), r["verdict"], r)
                done += 1
            if progress:
                progress(done, len(to_score))
            log_fn(f"scored {done}/{len(to_score)}")
        stats.scored = done
        stats.usage = llm.usage_summary()
    return stats
