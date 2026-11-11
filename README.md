# jobagent

An agent that reads your CV and aspirations, builds a search profile, pulls listings from
several job sources, throws away the obvious misses for free, and has Claude score the rest
against *your* profile with specific reasons and gaps. Repeated runs only surface new listings.

```
CV + profile.yaml ──► Claude (once) ──► candidate_profile.json
                                              │
   Adzuna ─┐                                  ▼
   Reed ───┼──► dedupe + keyword prescreen ──► SQLite ──► Claude scores new ones ──► report.md
   Careers ┤      (no API cost)                            (Haiku, ~8 jobs/call)
   eFC ────┘
```

## The app

Double-click `start.command` (Mac), `start.sh` (Linux) or `start.bat` (Windows). The first run
installs everything into a local `.venv`; after that it just starts the server and opens
http://127.0.0.1:8765 in your browser. Needs Python 3.10+ on the machine. Nothing leaves your
machine except calls to the job boards and to Claude.

Inside the app:

1. **Settings**: paste your Anthropic key (required) and Adzuna / Reed keys (free, optional).
   Tick the sources you want. Hit *Check which work* on the career-page list and delete the BAD ones.
2. **Profile**: upload your CV, write your aspirations in plain English, click *Build*. Then edit the
   search queries / must-have / reject keywords it produced. That editing is where the quality comes from.
3. **Run a search**: one click. Watch the log; open the pre-screen rejects to tune keywords.
4. **Shortlist**: ranked table, click a row for why it fits / gaps / what to lead with. Mark applied,
   saved or ignored.
5. **Score a listing**: paste anything from LinkedIn, a recruiter email or a bank portal and get the
   same analysis.

6. **Replies**: connect your mailbox in Settings (Gmail: 2-step verification on, then an
   [app password](https://myaccount.google.com/apppasswords); Outlook: `outlook.office365.com` /
   `smtp.office365.com`). While the app is open it checks the inbox every 10 minutes for mail from
   companies you've marked *Applied* and anything that reads like recruiter traffic, has Claude
   classify each one (interview, assessment, offer, rejection, acknowledgement, request for info,
   recruiter outreach), links it to the job, and shows a badge. Optional: email yourself a digest
   when new replies land. Browser notifications fire if you allow them. Nothing is sent from your
   account unless you turn the email digest on. Job-alert digests and newsletters are filtered out
   before anything reaches Claude, so the cost is a fraction of a penny per real email.

Everything the app stores lives in `data/` (profile, SQLite job store, keys). Delete the folder to reset.

The CLI below does the same things from a terminal, for cron.

## CLI setup

```bash
cd jobagent
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env         # fill in keys, then: export $(grep -v '^#' .env | xargs)
```

Keys:
- `ANTHROPIC_API_KEY` from console.anthropic.com (required)
- `ADZUNA_APP_ID` / `ADZUNA_APP_KEY`: free at https://developer.adzuna.com (250 calls/day)
- `REED_API_KEY`: free at https://www.reed.co.uk/developers/jobseeker (UK only)

Career pages and eFinancialCareers need no key.

## Use

```bash
jobagent probe                 # check which career-page slugs in companies.yaml actually work
jobagent profile               # CV + profile.yaml -> data/candidate_profile.json  (one Sonnet call)
jobagent run -v --no-score     # dry run: fetch + prescreen, see what gets rejected and why
jobagent run                   # full run: scores new listings, writes data/report.md
jobagent run --all             # report everything ever scored, not just today's
jobagent status careers:3 applied --note "sent 12 Nov"    # id fragment is shown in the report
jobagent report                # re-render without any API calls
jobagent serve                 # the web app, same as the start scripts
```

**Read `data/candidate_profile.json` after the first `profile` run and edit it.** The model's
`search_queries`, `reject_keywords` and `must_have_keywords` are what everything else keys off;
ten minutes tuning them is worth more than anything else. Re-running `profile` overwrites it.

## Cost

Profile build: one Sonnet call, a few pence. Scoring: Haiku, roughly 8 listings per call,
~3k input tokens each. A weekly run that scores 100 new listings is well under £0.10. The
prescreen is deterministic and free, and nothing already in the SQLite store is re-scored,
so the steady-state cost is tiny. Cap a run with `--limit 50` if you want a hard ceiling.

## Running it weekly

cron on a Mac/Linux box:

```
0 8 * * 1  cd /path/to/jobagent && . .venv/bin/activate && set -a && . ./.env && set +a && jobagent run > data/last_run.log 2>&1
```

Or run it from a Claude scheduled task that executes the CLI on your machine and summarises
`data/report.md` back to you.

## Sources, honestly

| Source | Coverage | Reliability |
|---|---|---|
| Adzuna | broad UK + CH/EU, aggregates many boards | API, stable |
| Reed | UK, lots of recruiter-posted finance roles | API, stable |
| Career pages | the firms in `companies.yaml` via Greenhouse / Lever / Workday public JSON | stable per ATS, but slugs drift: run `probe` |
| eFinancialCareers | best finance-specific coverage | HTML scrape, will break occasionally; off by default |

Not included: LinkedIn (no individual API, scraping risks your account), Indeed (no API since 2023),
and the in-house portals at JPM / GS / MS / Citi / DB (no stable JSON; Adzuna and eFC pick most of
those up anyway).

## Tuning the scorer

The rubric lives in `jobagent/scorer.py` (`SYSTEM`). Seniority is deliberately dominant. If you
find it too harsh or too generous, change the calibration bands there rather than the model.

## Tests

```bash
python tests/test_pipeline.py      # offline, stub LLM, no keys needed
```

## Layout

```
jobagent/
  cli.py         commands
  server.py      FastAPI backend for the web app (search runs, mailbox sync loop)
  mail.py        IMAP fetch, candidate filter, Claude triage, SMTP self-notification
  web/index.html the web app (single file, no build step)
  pipeline.py    fetch -> prescreen -> score, shared by CLI and server
  config.py      env + yaml
  cv.py          pdf/docx/txt -> text
  llm.py         Anthropic wrapper, forced-JSON tool output
  profile.py     CandidateProfile + builder prompt
  prescreen.py   free filters
  store.py       SQLite
  scorer.py      batch scoring prompt
  report.py      markdown
  sources/       adzuna, reed, careers (greenhouse/lever/workday), efc
profile.yaml     your aspirations and source toggles
companies.yaml   employers polled directly
```
