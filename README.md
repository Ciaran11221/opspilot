# OpsPilot

**An agentic IT-operations assistant with a live tool-use trace panel.**

Ask it to find risky accounts or triage tickets, and watch it actually work
the problem — plan, tool call, result, next step — in real time, instead of
staring at a spinner and hoping for the best. Runs against a bundled
synthetic dataset out of the box, or against your own CSV export.

Built as a standalone interview/portfolio demo — project #1 in a planned
series covering agentic AI/automation engineering. It is **a demo, not a
production deployment**, and it is built and documented that way
consistently throughout this README and the code itself.

---

## Table of contents

- [Why this project exists](#why-this-project-exists)
- [What it does](#what-it-does)
- [Honesty notes](#honesty-notes)
- [Features](#features)
- [Tech stack](#tech-stack)
- [Architecture](#architecture)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Usage / example prompts](#usage--example-prompts)
- [Uploading your own data](#uploading-your-own-data)
- [Demo mode / auto-shutdown](#demo-mode--auto-shutdown)
- [Packaging as a standalone .exe](#packaging-as-a-standalone-exe)
- [Design decisions](#design-decisions)
- [Known limitations](#known-limitations)
- [Testing / validation](#testing--validation)
- [Roadmap](#roadmap)
- [License](#license)

---

## Why this project exists

I'm making a career pivot from IT operations/support (8+ years, enterprise
environments) into AI/automation engineering. The rest of my portfolio
(Panoptic, OpsMCP, Ticket Analyzer, ACMS) is dashboards and classifiers —
genuinely useful tools, but not *agentic*. None of them show a model
actually deciding what to do next.

OpsPilot is the piece that fills that gap: a real tool-use loop, where the
model chooses which tool to call, reads the result, and decides the next
step itself — with every step of that reasoning visible in a live trace
panel rather than hidden behind a loading spinner.

It also ties the rest of the portfolio's concepts together conceptually:
account hygiene auditing and ticket triage — the kinds of problems
Panoptic/OpsMCP and Ticket Analyzer solve as standalone scripts — become
*tools* an agent chooses to call as part of solving a broader request.

## What it does

Ask it things like:

- *"Find inactive accounts with elevated permissions and draft offboarding
  tickets for each."*
- *"Which open tickets are at SLA risk this week?"*
- *"Give me an account hygiene summary for elevated accounts."*

It queries an account directory and a ticket queue, reasons about what it
finds, and can draft a report or offboarding-ticket artifact as a final
output — either against the bundled synthetic dataset (reliable, seeded,
always finds something) or against a CSV you upload yourself (see
[Uploading your own data](#uploading-your-own-data)).

## Honesty notes

These notes exist because I'd rather over-disclose than have anyone
(including a future me, re-reading this in six months) mistake a demo for
something it isn't. They're also the framing I use consistently across my
CV, LinkedIn, and in interviews when this project comes up.

> **All data is synthetic — none of it comes from a real organization.**
> `backend/data/generate_data.py` generates an Okta/M365-style account
> export and a Jira-style ticket export from a seeded random generator. The
> seed guarantees a reliable, reproducible set of "interesting" findings
> (currently 12 inactive+elevated accounts, 16 SLA-risk tickets) so the demo
> behaves consistently every time it's run — that reliability is a *demo*
> requirement, not a claim about real-world detection rates.

> **This tool has never connected to a real Okta, M365, or Jira tenant.**
> The CSV upload feature (see below) reads a file you provide locally on
> your own machine — it does not call out to any identity provider or
> ticketing system's API, and it never will as currently scoped.

> **The agent loop is Claude's native tool-use loop, made visible — not a
> custom multi-agent framework.** There's one model, three tools, and a
> loop that shows its own steps. If asked in an interview, the accurate
> description is "an agentic tool-use loop with a live trace panel," not
> "multi-agent system" — that distinction matters and I'd rather be
> precise about it than oversell it.

> **`draft_report` produces a draft only, every time.** Nothing this tool
> does is ever submitted to a real ticketing or identity system. The UI
> says so explicitly on every drafted artifact, and so does the tool's own
> description sent to the model.

> **Performance claims are scoped to what was actually tested.** Any
> detection accuracy, time-saved, or "found X issues" claim about this
> project refers to validated results against the seeded synthetic dataset
> or a specific named test file — never framed as a production or
> real-world benchmark. Percentage claims stay under 100%; larger multiples
> are described as "Nx smaller/faster," not compounded percentages.

> **The CSV ingestion is conservative by design.** When it can't confidently
> map a column or parse a value, it flags that fact rather than guessing —
> see [Uploading your own data](#uploading-your-own-data) for exactly how.

## Features

- 🔧 **Real tool use, not a scripted demo.** The model picks from three
  tools (`query_accounts`, `query_tickets`, `draft_report`) based on the
  request, with the ability to chain multiple calls before answering.
- 📊 **Live agent trace panel.** Every plan step, tool call, tool result,
  and final answer streams into the UI as it happens, over Server-Sent
  Events — not a "thinking..." spinner.
- 📁 **Bring-your-own-data CSV upload.** Upload a real-shaped (but messy)
  export and watch the ingestion pipeline map unfamiliar columns, normalize
  dates/statuses/priorities, and flag anything it couldn't confidently
  parse — see the dedicated section below.
- 🌱 **Seeded synthetic demo dataset**, so the built-in demo prompts always
  produce the same reliable, reproducible findings.
- 🖥️ **Zero-install frontend.** Plain HTML + React/Tailwind via CDN, no
  Node build step, so the whole thing packages into a single portable
  folder (and eventually a single `.exe`).
- 🛑 **Clean demo teardown.** Closing the browser tab shuts the local
  server down automatically — no terminal window to remember to kill.
- 💸 **Cost-conscious by default.** Runs on Claude Haiku by default (cheapest
  current model), with a one-line environment variable override to switch
  to a stronger model for a polished live demo.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.12+ / FastAPI | Async support for SSE streaming; minimal boilerplate |
| Agent / LLM | Anthropic Claude API (native tool use) | Direct tool-use loop, no agent framework overhead |
| Frontend | Plain HTML + React (CDN) + Tailwind (CDN) | Zero build step; packages cleanly into a standalone exe |
| Data ingestion | Python stdlib `csv` + `difflib` | No pandas dependency; keeps the packaged app lightweight |
| Packaging | PyInstaller (planned/in progress) | Proven toolchain, already used on an earlier project |

## Architecture

```
opspilot/
  start_opspilot.bat        Double-click launcher for demos (Windows) - auto-shutdown enabled
  README.md
  LICENSE
  .gitignore
  backend/
    main.py                 FastAPI app - routes, SSE streaming, static file serving
    agent.py                 The tool-use loop against the Claude API + tool/schema definitions
    tools.py                  Tool implementations: query_accounts, query_tickets, draft_report
    csv_ingest.py               CSV -> normalized schema: header matching, date/status/priority parsing
    dataset_store.py             In-memory registry for uploaded datasets
    launcher.py                 Entry point for the packaged .exe (opens a browser, runs uvicorn)
    requirements.txt
    data/
      generate_data.py           Synthetic dataset generator (seeded)
      accounts.json                Generated Okta/M365-style account export
      tickets.json                   Generated Jira-style ticket export
    sample_uploads/
      messy_accounts_sample.csv      Deliberately messy test file (mixed headers/date formats)
      messy_tickets_sample.csv         Deliberately messy test file (Jira-style vocab, bad priority value)
  frontend/
    dist/index.html          Static frontend - React (via CDN, no build step) + Tailwind CDN
```

**Request flow, end to end:**

1. Browser sends a chat message (+ API key, + optional `dataset_id`) to `POST /api/chat`.
2. `main.py` opens an SSE stream and hands off to `agent.run_agent()`.
3. `agent.py` runs Claude's tool-use loop: each turn, the model either calls
   a tool or gives a final answer.
4. Tool calls are executed by `tools.py` against whichever dataset is active
   (bundled demo data, or an uploaded dataset via `dataset_store.py`).
5. Every step (plan / tool_call / tool_result / final) is yielded as a
   structured event and streamed to the browser immediately.
6. The frontend renders each event into the live trace panel as it arrives.

## Getting started

**Prerequisites:** Python 3.10+ (3.12 recommended), an Anthropic API key.

```bash
git clone https://github.com/Ciaran11221/opspilot.git
cd opspilot/backend
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8420
```

Open `http://localhost:8420`, paste in your Anthropic API key, and try one
of the example prompts.

> Your API key is only ever held in browser memory for the session and sent
> directly to your own local backend, which forwards it to the Anthropic
> API. It is never written to disk, logged, or stored anywhere.

**On Windows**, once installed, you can also just double-click
`start_opspilot.bat` at the repo root instead of using the commands above —
see [Demo mode / auto-shutdown](#demo-mode--auto-shutdown).

## Configuration

Both of these are optional — sensible defaults are used if unset.

| Environment variable | Default | Purpose |
|---|---|---|
| `OPSPILOT_MODEL` | `claude-haiku-4-5-20251001` | Which Claude model runs the agent loop. Override to `claude-sonnet-5` for a more polished live demo. |
| `OPSPILOT_AUTO_SHUTDOWN` | unset (off) | When set to `1`, the server shuts itself down when the browser tab closes. Set automatically by `start_opspilot.bat`; leave unset for normal development so an idle tab never kills your dev server. |

To regenerate the bundled synthetic datasets (reshuffles which accounts/
tickets get flagged, while keeping the same guaranteed count of "interesting"
findings):

```bash
cd backend/data
python generate_data.py
```

## Usage / example prompts

Try these against the bundled demo dataset first — they're guaranteed to
find something, every time:

- `Find inactive accounts with elevated permissions and draft offboarding tickets for each.`
- `Which open tickets are at SLA risk this week?`
- `Give me an account hygiene summary for elevated accounts.`

Watch the trace panel on the right as it works: you'll see the model choose
a tool, see the raw result come back, and see it decide whether it has
enough information yet or needs another tool call.

## Uploading your own data

Click **"Upload your own data"** in the header, then choose a CSV for
accounts and/or tickets — real exports work, not just the two sample files.

**What it does with an unfamiliar file:**

- **Column matching** — tries an exact match against a list of known
  aliases per field first (e.g. `SamAccountName`, `UPN`, `login` all map to
  `username`), then falls back to fuzzy string matching for typos or
  unlisted synonyms. Every match is shown with its confidence level
  (`exact` or `fuzzy`) so nothing is silently assumed.
- **Date parsing** — tries 11 common formats (ISO, US `mm/dd/yyyy`,
  `dd/mm/yyyy`, `Jan 5, 2026`, etc.). Unparseable dates are set to `null`
  and flagged in a warning, never guessed.
- **Status normalization** — collapses varied vocabularies (`Enabled`/
  `Active`/`1` → `ACTIVE`; `To Do`/`New`/`Backlog` → `Open`; `Done`/`Closed`/
  `Resolved` → `Resolved`) so filters work the same way regardless of which
  system exported the data.
- **Priority normalization** — maps common vocab (`Critical`, `Highest`,
  `Urgent` → `P1`; `Low`, `Minor`, `Trivial` → `P4`, etc.) to the P1–P4
  buckets the SLA-risk logic uses. Anything unrecognized defaults to `P3`
  **with an explicit warning**, rather than silently miscategorizing it.
- **Transparent reporting, always.** Every upload returns a full column
  mapping report and a list of warnings for anything ambiguous — shown
  directly in the UI before you ever run a query against it.

**Try it yourself** with the two intentionally messy files in
`backend/sample_uploads/` — different headers than the schema (`Severity`
instead of `priority`, `SamAccountName` instead of `username`), mixed date
formats, and one row with a made-up priority value the tool has never seen
before, to prove out the fallback path.

## Demo mode / auto-shutdown

`start_opspilot.bat` opens the browser and starts the server with
auto-shutdown enabled via `OPSPILOT_AUTO_SHUTDOWN=1`. In that mode:

- The frontend pings `/api/heartbeat` every 3 seconds while its tab is open.
- Closing the tab fires an immediate `/api/shutdown` request via
  `navigator.sendBeacon`, so the server exits within a fraction of a second
  of a normal close.
- A background thread also force-exits if heartbeats stop arriving for more
  than 8 seconds, covering an unclean disconnect (browser crash, force-quit)
  that the beacon wouldn't catch.

Running the server directly via `uvicorn --reload` (i.e. normal development,
not via the `.bat` file) never triggers any of this — closing a tab during
regular dev work won't kill your dev server.

## Packaging as a standalone .exe

Not yet done — planned next step. The approach (already proven on an
earlier project, the AI Test Agent):

```bash
pip install pyinstaller
pyinstaller --onefile --distpath exe-dist \
  --add-data "frontend/dist;frontend/dist" \
  --add-data "backend/data;data" \
  backend/launcher.py
```

(`--distpath exe-dist` avoids a naming collision with the tracked
`frontend/dist/` folder, which is source, not a build artifact.)

This would bundle the backend, the static frontend, and the synthetic
datasets into a single executable that starts a local server and opens the
browser automatically. A real Anthropic API key would still be required to
run live agent calls — the packaging gets zero-install and
offline-until-you-ask-Claude-something, **not** a bundled API key. Shipping
a key inside a distributable is never the plan.

## Design decisions

A few choices worth explaining, since they weren't the "obvious" option:

- **No Node.js/build step for the frontend.** The UI is plain HTML with
  React and Tailwind loaded from CDN, using `React.createElement` calls
  instead of JSX. This keeps the eventual PyInstaller packaging simple —
  there's nothing to compile, `frontend/dist/` just needs to exist on disk.
- **Keyword-based "elevated account" detection, not an exact group-name
  list.** Early on this matched a fixed list of the demo dataset's own group
  names (`Domain-Admins`, `Billing-Admins`, etc.), which would have silently
  failed to generalize to a real organization's own naming conventions.
  It's now keyword-based (`admin`, `root`, `super`, `owner`, `billing`,
  etc.) matched against both title and group membership, so it works on
  real exports with unfamiliar naming.
- **Ingestion fails loud, never silent.** Anywhere the CSV ingestion can't
  confidently identify a column or parse a value, it surfaces that as an
  explicit warning rather than making a best guess that could silently
  corrupt a filter result later (e.g. an account wrongly excluded from
  "elevated" because a `groups` column was mis-mapped).
- **In-memory dataset storage, no database.** This is a local, single-user
  demo tool, not a multi-tenant service — a process-local dict is the right
  amount of engineering for that scope, not an under-engineered shortcut.
- **Cheapest model by default.** Defaults to Claude Haiku rather than a
  larger model, since the task (structured tool calls against small JSON
  datasets) doesn't need frontier reasoning. `OPSPILOT_MODEL` makes swapping
  to a stronger model for a live demo a one-line change.

## Known limitations

- Date parsing assumes US-style `mm/dd/yyyy` before falling back to
  `dd/mm/yyyy` for ambiguous numeric dates (e.g. `03/04/2026`) - correct for
  most US-originated exports, but worth knowing if testing with UK/EU-style
  files where day comes first.
- Uploaded datasets live in server memory only - restarting the server
  clears them. There's no persistence layer, by design (see Design
  decisions above).
- The agent loop is capped at 6 tool-use turns (`MAX_TURNS` in `agent.py`)
  to prevent a runaway loop during a live demo; a genuinely complex request
  could in theory need more steps than that.
- No automated test suite yet - validation so far has been manual/scripted
  smoke testing against both the bundled dataset and the sample messy CSVs
  (see below).

## Testing / validation

There's no CI or automated test suite yet (see Roadmap), but the following
has been manually verified end-to-end:

- The bundled demo dataset consistently returns its seeded ground-truth
  counts (12 inactive+elevated accounts, tickets at various SLA-risk
  thresholds) through the actual HTTP API, not just at the function level.
- CSV ingestion tested against two deliberately messy sample files
  (different headers, mixed date formats, an unrecognized priority value)
  with correct column mapping, correct fallback behavior, and correct
  warning generation.
- Full upload → dataset summary → chat request pipeline tested against a
  running server, confirming an uploaded dataset is actually used by the
  agent loop instead of the bundled demo data.
- Heartbeat/shutdown endpoints tested directly: health check, heartbeat
  acknowledgment, and explicit shutdown all confirmed working, including
  the server actually terminating on request.

## Roadmap

- [ ] PyInstaller build + smoke test on a clean Windows machine
- [ ] Automated test suite (currently manual/scripted validation only)
- [ ] A couple more seeded ground-truth scenarios for demo variety
- [ ] Short screen recording for the README / LinkedIn post
- [ ] Interview prep: precise language for the tool-use loop (single model +
      tools, not multi-agent), why the demo data is synthetic, and how the
      CSV ingestion handles ambiguous/messy input

## License

MIT — see [LICENSE](LICENSE).
