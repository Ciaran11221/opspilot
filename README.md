# OpsPilot

An agentic IT-operations assistant with a live agent trace panel: plan, tool
call, result, next step - visible in real time instead of hidden behind a
spinner. Handles two common IT-ops workflows out of the box (account hygiene
auditing and ticket SLA triage) against either a bundled synthetic dataset or
your own uploaded CSV export.

Built as a standalone interview/portfolio demo - project #1 in a planned
portfolio series. It's a demo, not a production deployment, and it's built
and described that way throughout this README and in the code.

## Contents

- [Why this project exists](#why-this-project-exists)
- [What it does](#what-it-does)
- [Honesty notes](#honesty-notes-carry-these-into-any-cvlinkedin-copy)
- [Architecture](#architecture)
- [Running it locally](#running-it-locally)
- [Uploading your own data](#uploading-your-own-data)
- [Demo mode / auto-shutdown](#demo-mode--auto-shutdown)
- [Packaging as a standalone .exe](#packaging-as-a-standalone-exe-next-step)
- [MVP scope](#mvp-scope-locked)
- [Open items / next steps](#open-items--next-steps)
- [License](#license)

## Why this project exists

The rest of the portfolio (Panoptic, OpsMCP, Ticket Analyzer) is dashboards
and classifiers - useful, but not agentic. OpsPilot is the piece that shows
an actual tool-use loop: the model decides which tool to call, sees the
result, and decides what to do next, with every step visible in a live trace
panel.

It also ties the existing portfolio's concepts together: account auditing
and ticket triage become tools an agent chooses to call, rather than
standalone scripts.

## What it does

Ask it things like:

- "Find inactive accounts with elevated permissions and draft offboarding
  tickets for each."
- "Which open tickets are at SLA risk this week?"

It queries an account directory and a ticket queue, reasons about what it
finds, and can draft a report or offboarding-ticket artifact as a final
output - either against the bundled synthetic dataset, or against your own
uploaded CSV (see [Uploading your own data](#uploading-your-own-data)).

## Honesty notes (carry these into any CV/LinkedIn copy)

- **The bundled demo data is synthetic.** `backend/data/generate_data.py`
  generates an Okta/M365-style account export and a Jira-style ticket export,
  seeded for reproducible demo scenarios. No real organization's accounts or
  tickets are used in it.
- **No live business access.** This tool has never connected to a real Okta,
  M365, or Jira tenant. The CSV upload feature reads a file you provide
  locally - it does not call out to any identity provider or ticketing API.
- **The agent loop is Claude's native tool-use loop, made visible** - not a
  custom multi-agent framework. Describe it as "an agentic tool-use loop with
  a live trace panel," not "multi-agent system."
- **`draft_report` produces a draft only.** Nothing is submitted to a real
  ticketing system, ever. The UI and copy always make this explicit.
- Any performance claims (detection accuracy, time saved, etc.) must be
  framed as validated against the seeded synthetic dataset or a specific test
  file, not real-world/production scale. Cap "reduction" claims under 100%;
  use "Nx smaller/faster" for larger multiples.

## Architecture

```
opspilot/
  start_opspilot.bat   Double-click launcher for demos (Windows)
  backend/
    main.py            FastAPI app - routes, SSE streaming, static file serving
    agent.py            The tool-use loop against the Claude API + tool/schema definitions
    tools.py             Tool implementations: query_accounts, query_tickets, draft_report
    csv_ingest.py         CSV -> normalized schema: header matching, date/status/priority parsing
    dataset_store.py       In-memory registry for uploaded datasets
    launcher.py           Entry point for the packaged .exe (opens a browser, runs uvicorn)
    requirements.txt
    data/
      generate_data.py     Synthetic dataset generator (seeded)
      accounts.json         Generated Okta/M365-style account export
      tickets.json           Generated Jira-style ticket export
    sample_uploads/
      messy_accounts_sample.csv   Deliberately messy test file (mixed headers/date formats)
      messy_tickets_sample.csv     Deliberately messy test file (Jira-style vocab)
  frontend/
    dist/index.html    Static frontend - React (via CDN, no build step) + Tailwind CDN
```

**Why no Node build step:** the frontend is plain HTML with React and
Tailwind loaded from CDN, using JSX-free `React.createElement` calls. This
keeps PyInstaller packaging simple - there's nothing to compile,
`frontend/dist/` just needs to exist and get bundled alongside the Python
backend.

## Running it locally

```bash
cd backend
pip install -r requirements.txt   # or use a venv
python -m uvicorn main:app --reload --port 8420
```

Open `http://localhost:8420`, paste in an Anthropic API key, and try one of
the example prompts. The key is only held in browser memory for the session
and sent directly to your own backend, which forwards it to the Anthropic
API - it's never written to disk.

By default the agent runs on Claude Haiku (cheapest option, plenty capable
for this scope). Override the model via an environment variable:

```bash
# Windows (PowerShell)
$env:OPSPILOT_MODEL = "claude-sonnet-5"
# macOS/Linux
export OPSPILOT_MODEL=claude-sonnet-5
```

To regenerate the synthetic datasets (e.g. to reshuffle which accounts/
tickets get flagged):

```bash
cd backend/data
python generate_data.py
```

## Uploading your own data

Click "Upload your own data" in the header, then choose a CSV for accounts
and/or tickets. Column names, date formats, and status/priority wording
don't need to match any fixed schema - `csv_ingest.py` maps them
automatically (exact match against known aliases, then fuzzy matching for
typos/variants) and shows you exactly what it matched, with a confidence
level per field.

Anything it can't confidently parse is flagged rather than guessed:
unrecognized priority values default to P3 with a warning, unparseable dates
are skipped (not treated as very old or very new), and missing required
columns are called out explicitly. Try it yourself with the two
deliberately-messy files in `backend/sample_uploads/`.

## Demo mode / auto-shutdown

`start_opspilot.bat` (Windows) opens the browser and starts the server with
auto-shutdown enabled, so the whole thing tears itself down cleanly when you
close the tab - no terminal window to remember to Ctrl+C after a demo. This
is opt-in via the `OPSPILOT_AUTO_SHUTDOWN` env var, so running the server
directly via `uvicorn --reload` for development is unaffected - closing a
tab during normal dev work won't kill your server.

## Packaging as a standalone .exe (next step)

The plan (already proven with the earlier AI Test Agent project) is:

```bash
pip install pyinstaller
pyinstaller --onefile --distpath exe-dist --add-data "frontend/dist;frontend/dist" --add-data "backend/data;data" backend/launcher.py
```

(`--distpath exe-dist` avoids any collision with the tracked
`frontend/dist/` folder, which is source, not a build artifact.)

This bundles the backend, the static frontend, and the synthetic datasets
into a single executable that starts a local server and opens the browser. A
real Anthropic API key is still required to run live agent calls - the
packaging gets you zero-install and offline-until-you-ask-Claude-something,
not a bundled key (never ship a key inside a distributable).

## MVP scope (locked)

1. Chat-style input box
2. Agent loop with 3 tools (`query_accounts`, `query_tickets`, `draft_report`)
3. Live trace panel (plan / tool call / result / next step - not a spinner)
4. Bundled synthetic dataset with seeded ground truth
5. CSV upload with transparent column mapping and validation
6. One polished "wow" output view (the drafted report/ticket artifact card)

## Open items / next steps

- [ ] PyInstaller build + smoke test on a clean Windows machine
- [ ] Add a couple more seeded ground-truth scenarios for demo variety
- [ ] Optional: a short screen recording for the GitHub README / LinkedIn post
- [ ] Interview prep: be ready to explain the tool-use loop precisely (single
      model with tools, not multi-agent), why the demo data is synthetic, and
      how the CSV ingestion handles ambiguous/messy input

## License

MIT - see [LICENSE](LICENSE).
