"""Offline end-to-end test: fixture listings + a stub LLM, no network, no API key.
Run with:  python -m pytest tests/   (or just python tests/test_pipeline.py)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobagent.prescreen import prescreen  # noqa: E402
from jobagent.profile import CandidateProfile  # noqa: E402
from jobagent.report import render  # noqa: E402
from jobagent.scorer import score_jobs  # noqa: E402
from jobagent.sources.base import Job  # noqa: E402
from jobagent.store import Store  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


class StubLLM:
    """Deterministic stand-in: scores by crude keyword rules so the test is reproducible."""
    model = "stub"
    input_tokens = output_tokens = 0

    def structured(self, *, system, user, schema, name, max_tokens=0):
        import re
        results = []
        for m in re.finditer(r'<listing index="(\d+)">\nTITLE: (.*?)\n', user):
            i, title = int(m.group(1)), m.group(2).lower()
            fit = 80 if "xva" in title or "cva" in title else 60 if "quant" in title else 30
            if "senior" in title or "vp" in title:
                fit = 20
            results.append({"index": i, "fit": fit, "verdict": "apply" if fit >= 65 else "consider" if fit >= 40 else "skip",
                            "role_type": "test", "fits": ["stub"], "gaps": [], "hard_blocker": fit < 25,
                            "emphasise": "stub", "seniority_match": "match"})
        return {"results": results}

    def usage_summary(self):
        return "stub"


def load_fixture_jobs() -> list[Job]:
    return [Job(**d) for d in json.loads((FIX / "listings.json").read_text())]


def test_prescreen_and_score():
    prof = CandidateProfile.load(FIX / "profile.json")
    jobs = load_fixture_jobs()
    scr = prescreen(jobs, prof)
    reasons = {j.title: why for j, why in scr.rejected}
    assert "Senior Quantitative Analyst" in reasons and "senior" in reasons["Senior Quantitative Analyst"]
    assert "Actuarial Analyst" in reasons
    assert any(why == "duplicate" for why in reasons.values())
    assert "Marketing Executive" in reasons and reasons["Marketing Executive"] == "no must-have keyword"
    kept_titles = {j.title for j in scr.kept}
    assert {"XVA Quant Analyst", "Quantitative Analyst, Rates", "Junior Quant Developer"} <= kept_titles

    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.sqlite")
        new = store.upsert_jobs(scr.kept)
        assert len(new) == len(scr.kept)
        assert store.upsert_jobs(scr.kept) == []          # second run: nothing new
        scored = score_jobs(StubLLM(), prof, store.unscored(), batch_size=2)
        assert len(scored) == len(scr.kept)
        for r in scored:
            j = r.pop("job")
            store.save_score(j.uid, r["fit"], r["verdict"], r)
        rows = store.top(min_fit=40)
        assert rows[0]["title"] == "XVA Quant Analyst"
        md = render(rows, screen_stats={"fetched": len(jobs), "sources": 1, "new": len(new), "kept": len(scr.kept), "scored": len(scored)})
        assert "XVA Quant Analyst" in md and "| Fit |" in md
        store.set_status(rows[0]["uid"], "applied")
        assert rows[0]["uid"] not in {r["uid"] for r in store.top(min_fit=40)}
        print(md)


if __name__ == "__main__":
    test_prescreen_and_score()
    print("\nOK")
