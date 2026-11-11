"""Markdown report of the ranked shortlist."""

from __future__ import annotations

from datetime import date

VERDICT_LABEL = {"apply": "APPLY", "consider": "CONSIDER", "stretch": "STRETCH", "skip": "skip"}


def render(rows: list[dict], title: str = "Job shortlist", min_fit: int = 40, screen_stats: dict | None = None) -> str:
    lines = [f"# {title}", "", f"_Generated {date.today().isoformat()}_", ""]
    if screen_stats:
        lines.append(
            f"Fetched {screen_stats.get('fetched', 0)} listings from {screen_stats.get('sources', '?')} sources, "
            f"{screen_stats.get('new', 0)} new, {screen_stats.get('kept', 0)} passed pre-screen, "
            f"{screen_stats.get('scored', 0)} scored. {screen_stats.get('usage', '')}"
        )
        lines.append("")
    rows = [r for r in rows if r["fit"] >= min_fit]
    if not rows:
        lines.append("Nothing above the fit threshold this run.")
        return "\n".join(lines)

    lines += ["| # | Fit | Verdict | Role | Company | Location | Posted |", "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        p = r["payload"]
        blocker = " ⚠" if p.get("hard_blocker") else ""
        lines.append(f"| {i} | {r['fit']} | {VERDICT_LABEL.get(r['verdict'], r['verdict'])}{blocker} | "
                     f"[{r['title']}]({r['url']}) | {r['company']} | {r['location']} | {r['posted'] or ''} |")
    lines.append("")

    for i, r in enumerate(rows, 1):
        p = r["payload"]
        lines += [f"## {i}. {r['title']} at {r['company']}  (fit {r['fit']}, {VERDICT_LABEL.get(r['verdict'])})",
                  f"{r['location']} · {r['salary'] or 'salary n/a'} · {r['source']} · id `{r['uid'][-12:]}`", "",
                  f"**Role type:** {p.get('role_type', '')} · **Seniority:** {p.get('seniority_match', '')}"
                  + (" · **Hard blocker flagged**" if p.get("hard_blocker") else ""), "",
                  "**Why it fits**"]
        lines += [f"- {x}" for x in p.get("fits", [])]
        lines += ["", "**Gaps**"]
        lines += [f"- {x}" for x in p.get("gaps", [])] or ["- none flagged"]
        lines += ["", f"**Lead with:** {p.get('emphasise', '')}", "", f"[Listing]({r['url']})", ""]
    return "\n".join(lines)
