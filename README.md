# beom-workbench

**English** | [한국어](README.ko.md) | [日本語](README.ja.md) | [简体中文](README.zh-CN.md)

**A desktop workbench for coding agents: plan a feature, build it from a request or from a reference repository, and verify it against acceptance criteria you approved.**


> The app's interface is available in Korean, English, Japanese and Simplified Chinese. It follows your system language on first start (Korean when the system language is not one of these) and can be changed from the language button in the sidebar. Agents follow their own rules for the language they write in: when the language of your request is clear they are told to use it; when it is not (only an attachment or a link), or when you set a response language in the agent's own settings (Claude Code `language`), the agent's setting applies. Command-line help (`--help`) and the installers' messages follow the same language.

![version](https://img.shields.io/badge/version-0.3.1-blue) [![smoke](https://github.com/ParkBeomsoo/beom-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/ParkBeomsoo/beom-workbench/actions/workflows/ci.yml) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-use%20only%2C%20no%20modification-lightgrey)

Instead of letting an agent build until it says "done", the workbench fixes the requirements and acceptance cases first, has the
agent work on its own branch, and checks by program that the result meets them — or, when porting, that it behaves like the
original (every analysis conclusion carries evidence, file:line).
It ships a skill, MCP tools and a CLI so that AI agents such as Claude Code follow the procedure, and it blocks
completion of anything an AI should not decide alone until a human approves it.

## Contents

- [What it does](#what-it-does)
- [Four modes — which one when](#four-modes--which-one-when)
- [Supported platforms](#supported-platforms)
- [Key concepts](#key-concepts)
- [Requirements](#requirements)
- [Installation](#installation)
- [Desktop app and automatic runs](#desktop-app-and-automatic-runs)
- [Quick start 1: analyze a whole repository](#quick-start-1-analyze-a-whole-repository)
- [Quick start 2: port a feature and verify it](#quick-start-2-port-a-feature-and-verify-it)
- [Quick start 3: build from a request](#quick-start-3-build-from-a-request)
- [Customizing the original in place](#customizing-the-original-in-place)
- [Case file format](#case-file-format)
- [Command summary](#command-summary)
- [Data and storage](#data-and-storage)
- [Configuration](#configuration)
- [Safety principles](#safety-principles)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Security and contributions](#security-and-contributions)
- [License](#license)

## What it does

| You want to | The toolkit |
|---|---|
| **Understand every feature** | Classifies every file, splits every function into review units, and attaches evidence (file:line) to each conclusion. Anything it cannot confirm goes to an "unknowns" list. |
| **Get the real API list** | Finds HTTP and CLI entry points with per-framework rules and expands URLs assembled in code into the actual route list. |
| **See impact before changing** | Shows what a function you plan to change affects (callers, implementations, entry points, tests) and where you can plug in without touching the core (extension points). |
| **Follow upstream updates** | Compares two versions and finds where upstream changes collide with your modifications (removed, renamed or re-signed functions). |
| **Port only the feature** | Cuts out the code, state, configuration and external dependencies one entry point needs, and records in a ledger where each item went. The target may be a different language. |
| **Verify the port** | Feeds the same inputs to the original and to your code and compares the results (HTTP, CLI or function level). Intentionally changed behavior is checked against expected values, and new behavior that does not exist in the original can be checked the same way. |
| **Build what you ask for** | Starting from "build a feature like this" with no original, it gets requirements and acceptance criteria approved first, then verifies the build meets them and survives odd input. |
| **Block AI shortcuts** | Accepted differences, omitted parts, reduced test cases and expected values cannot complete without human approval. Code changed after verification, or records edited outside the toolkit, are detected. |

## Four modes — which one when

| Mode | Use it when | You get | Human checks |
|---|---|---|---|
| **Build from a request** | You know what you want ("a shop search page", "add an API") — new or inside an existing repo | Requirements → acceptance criteria → plan → code → checks against the criteria | Once at the start (criteria + plan together), or once at the end |
| **Build from a reference** | Another repository (open source or in-house) already does it and you want it ported, or rebuilt your way | Source analysis → scope → cases (from the source's behaviour or your request) → code → comparison | Same as above; accepted differences and omissions need approval |
| **Plan** | The idea is not settled yet, or design/QA need a spec before anyone codes | Plan, policy table (cross-checked with your existing policy documents), clickable screen prototype, test scenarios | Answer a few questions; nothing is built |
| **Free chat** | Small edits, questions, one-off jobs | The agent CLI as-is in that folder (edits files and runs commands under the same permission rules and work branch) | None — no procedure |

They hand off to each other: *Plan* → **Build from this plan** opens a *request* run with the whole package attached, and a
*request* or *reference* run can be continued by message at any time. Point any mode at an existing repository (or a parent
folder holding several) and it reads that code, its rules and its look first, and works on a `feature/<task>` branch.

## Supported platforms

Windows 10/11 is the primary platform (used daily). macOS and Linux run the same code, and CI checks install, core behaviour and app start on all three;
native desktop-app screen checks (Qt · WinForms · WPF) are Windows-only, and Claude Code's OS sandbox is available on macOS/Linux only.

## Key concepts

**Evidence levels** — how certain a conclusion is.

| Level | Meaning | How |
|---|---|---|
| L0 | Read the code and confirmed with the structure map | default |
| L1 | Calls/references confirmed by a language server | `lsp` |
| L2 | Where a value flows from and to is confirmed | `dataflow` |
| L3 | Confirmed to actually execute | coverage report, differential verification |

**Ledger** — analysis and porting state accumulates in a per-project SQLite ledger (`~/.beom-workbench/projects/<id>/ledger.db`).
New sessions and several agents working in parallel continue the same record.

**Porting gates (7)** — `port-finish` issues a completion verdict only when all are met.

1. Every item to port is handled (ported / replaced / omitted)
2. Ported locations still exist (down to the line content)
3. Differential verification passes — all cases of the last capture, **against the current code** (invalid if the code changed afterwards)
4. Cases execute every function being ported (original coverage, or your code's coverage when the original cannot be instrumented)
5. Every item needing human review is approved (accepted differences, omissions, case changes, expected values; invalid if the content changes after approval)
6. Records were not changed outside the toolkit (ledger hash chain, golden hash)
7. No restrictive license on the code being ported — or you confirmed it yourself (the app shows a card; the agent cannot confirm it). A missing license is only noted

Every porting command response includes `next_step` (what to do next).

## Requirements

| Category | Details |
|---|---|
| Required | Python 3.11+, git |
| Required engines | structure-map engine and entry-point rule engine — installed by `setup` (checksum verified) |
| Optional | language servers (L1), data-flow engine (L2, JDK 21+), coverage tools (L3), container engine (isolated execution of third-party code) |
| Language servers (L1) | Rust · Java · Kotlin · TypeScript/JavaScript · Python · Go · C/C++ · C# · Dart · Ruby (other languages up to L0; file and symbol classification is language independent) |
| Entry-point rules | Axum · Spring · Express · FastAPI/Flask · Gin · clap · Next.js (frameworks without rules are automatically recorded as unknowns) |

Engine names, licenses and install locations are listed in [THIRD_PARTY.md](THIRD_PARTY.md).

## Installation

```bash
# 1. Install the toolkit so the beom-workbench command works from any folder
pipx install git+https://github.com/ParkBeomsoo/beom-workbench.git
# (or) uv tool install git+https://github.com/ParkBeomsoo/beom-workbench.git

# 2. Check status — engines, language servers, skill, MCP registration
beom-workbench doctor

# 3. Install engines — look at the plan first, then install only what you pick
beom-workbench setup --languages python typescript
beom-workbench setup --apply codebase-memory-mcp ast-grep lsp:python lsp:typescript coverage
```

- Without options, `setup` only **shows a plan** and changes nothing. Only the items you list after `--apply` are installed.
- Running third-party code in isolation needs a container engine. On Windows, `setup --apply sandbox` installs a container
  engine inside a WSL distribution (no desktop app required). An existing `docker` is used if present.
- Install language runtimes (JDK, Node.js, Go, Rust) yourself.

### AI agent (Claude Code) integration

Use one of the two.

```bash
# A. Register the skill + MCP with setup
beom-workbench setup --apply skill mcp-register
beom-workbench setup --apply skill mcp-register --scope project --project-dir <project>   # one project only

# B. Claude Code plugin (install the toolkit with step 1 first)
claude plugin marketplace add ParkBeomsoo/beom-workbench
claude plugin install beom-workbench@beom-workbench
```

- The installed skill copy records its version, so `doctor` reports `outdated` after a toolkit update.
- A skill with the same name you created yourself, or an MCP registration pointing at another installation, is never overwritten — you are told instead.
- You can use the CLI alone without MCP. Rules such as approval and completion verdicts behave the same in the CLI.

### Update and removal

```bash
pipx upgrade beom-workbench
beom-workbench setup --update                      # refresh only the skill you already installed (installs nothing new)
beom-workbench setup --remove skill mcp-register   # undo only what this toolkit installed or registered
pipx uninstall beom-workbench
```

## Desktop app and automatic runs

**Windows** (PowerShell) — installs the app and asks whether to add Start menu and desktop shortcuts:

```powershell
irm https://raw.githubusercontent.com/ParkBeomsoo/beom-workbench/main/install.ps1 | iex
```

**macOS · Linux**: `curl -fsSL https://raw.githubusercontent.com/ParkBeomsoo/beom-workbench/main/install.sh | sh`

Or by hand:

```bash
pipx install "beom-workbench[app] @ git+https://github.com/ParkBeomsoo/beom-workbench.git"
beom-workbench                    # open the app
beom-workbench app --shortcut          # add Start menu (search) + desktop shortcuts; --remove-shortcut removes them
```

The first time you open the app it also offers to create the shortcuts. On Linux it writes a `.desktop` entry.

**Pick a mode on the home screen**

| Mode | When | Stages |
|---|---|---|
| **Request based** | Build a feature like this (no original to follow) | requirements → acceptance criteria → plan → build → verify → handoff |
| **Reference based** | Bring a feature of another repository into my project | analyze → scope → record original → plan → build → compare → handoff |
| **Planning** | Turn a feature or service into a planning package for dev, design and QA | discover → spec → policy → screens → scenarios → check & handoff → *Build from this plan* |
| **Free chat** | Use Claude Code or Codex directly, without this process | message → work → message … |

**Attachments** — attach spec documents or links to any request: md · txt · pdf · images, and Word · PowerPoint · Excel · HWPX
(converted to text alongside the original). Old binary formats (.hwp · .doc · .ppt · .xls) cannot be read and are refused at attach
time with a note on how to re-save them (detected by content, not just extension). Links are saved so the agent can understand
them, not just as flat text: the page is converted to Markdown that keeps headings, lists and tables (merged cells included) and
collapsed sections, and its images and attached files are downloaded next to it (Word, PowerPoint and Excel attachments are also
converted to text) — a spec page is often half tables and screen mock-ups. For a page behind a login (e.g. Confluence, Jira) the app
asks you to sign in: it opens its own browser window, you log in there and press **Get this document**. For Confluence it reads the
page through the site's own REST API with that signed-in session, together with its child pages, comments and attachments; for Jira,
the issue with its description, sub-tasks, linked issues, comments and attachments. Other sites use the main content of the page.
Sign-ins are kept in a separate app profile (clear them under **Storage → Document sign-ins**). The agent reads attachments first. Files can also be dropped onto the window, and a screenshot can be pasted (Ctrl+V); messages sent to a running or finished task can carry attachments too — e.g. a screenshot of what to fix.

**Planning package** — planning mode produces the set of documents a product planner hands over, not a single page (in `.beom/brief/`):
기획서.md (goals and success metrics, users, feature list P0/P1/P2, requirements R1…, out of scope, open questions, release steps),
정책서.md (decision tables, calculation order with worked numbers incl. edge values, state changes, exceptions and user-facing messages),
화면설계.md + wireframes/ (screens S1… with numbered callouts and states, grayscale HTML wireframes that the toolkit screenshots) and
테스트시나리오.md (T1… — normal, edge and error scenarios per requirement; they become acceptance cases when you build from the plan).
Before handoff the toolkit checks that the documents line up (`wb_brief_check`): requirements without scenarios, screens without
wireframes or missing documents block the handoff. *Build from this plan* attaches the whole package, wireframes included.
The screen design is a **clickable prototype**: feature tabs on top (the 기능 column of the screen list), the screen in the middle as real
HTML you can click through, and that screen's notes on the right — clicking a button, a tab or a screen chip switches the screen and the notes
follow. *Export* writes a shareable **single HTML file** (one tab per document, the same prototype inside) or an **A4 PDF** (screens as
images with notes). Prototype screens run in an isolated frame and cannot reach the app. Planning on top of existing policy documents or an existing product cites those rules, writes only the
changes as "current → new", and draws screen examples in the existing look.

**Building on what exists** — when the target folder already has code or documents (recorded when the run is created), or a repository to
fit into or port from is attached, the agent first learns and follows its language, structure, business rules (policy docs, validation, copy)
and look (screenshots of existing screens, existing design tokens) — in every mode.

*Build from a reference* (formerly "port from open source") only needs the reference repository URL or path (open source or
in-house). Describe the feature in the text box, or leave it empty — the agent then lists the repository's features and asks you to pick
one. Besides faithful ports it handles "build this our way, inspired by that" — design and behaviour come from the source (cited as
file:line), acceptance criteria from your request.

**Folder** — leave *where to build* empty and a new folder under Documents is used. To change existing code pick that repository, or a
parent folder holding several repositories — the agent finds the right one for the request.

**Rules and policies** — no need to paste them into the chat. When a run is created the target folder is scanned for rule hubs (README of
folders named like rules, policies or guide), AGENTS.md / CLAUDE.md and policy, guide and storyboard folders; the agent is told to read and
follow them first (in the hub's reading order if it has one). Existing policy wins; new rules are marked as recommendations with a
conflict check. Plan mode writes an "existing policy" comparison table and the brief check requires it when policy documents exist.
*Free chat* is the agent CLI as-is in that folder (it edits files and runs commands too, under the same permission rules).

**Agent, model, effort** — one button next to the send button.

| Agent | Notes |
|---|---|
| Claude Code | asks per action through the toolkit's permission rules. Models: default · Opus · Sonnet · Haiku (aliases always resolve to the latest), effort low → max |
| Codex | blocks with its sandbox instead of per-action questions (writes only inside the target folder). Models and effort levels come from the list Codex keeps for your account, so new models appear automatically |
| Gemini (Antigravity CLI `agy`) | experimental. Since June 2026 consumer Gemini CLI has been replaced by Antigravity CLI — `agy` is used when present, otherwise an Enterprise `gemini`. Shell commands that need confirmation are refused headless, so it works through edits and toolkit tools. MCP is attached via the target folder's `.agents/mcp_config.json` |

Any other model name can be typed in. Model and effort can be changed during a run (applied from the next round).

**Permissions** — pick a mode; the per-kind rules are under *Options → Permissions*.

| Mode | Behaviour |
|---|---|
| Ask everything | every edit and command is confirmed |
| Allow edits (default) | edits inside the target folder, build/test commands and read-only commands run directly; other commands (running the project's own scripts, `python -c`, network, paths outside the folder …) and the web are confirmed |
| Auto | commands and web research run without asking; only files outside the folder are confirmed — use for trusted work. CLI: `beom-workbench run --permission auto` |

Push, publish, deleting outside the folder and system changes are always refused. Commands the agent passes to verification tools
(including commands inside case files) go through the same rules. Auth: *my login* (subscription) or *API key* (reads the key from an
environment variable, billed per use) — both can do the same things.

**What you decide** — only these appear as cards:

- **Once at the start — Before you start**: the agent drafts requirements, acceptance criteria and the plan in one go (no screens or code
  before approval) and one card shows what gets built and how to open it, what will be built (requirements), **decisions to make first**
  (web, desktop app or API only; data storage; how it runs — each a recommendation with alternatives and a reason), the design concept in a
  line or two, and the order of work. Acceptance criteria show what is checked; how it was tested lives in the *Checks* tab. Pick an
  alternative before approving and the changed decision is passed to the agent; requirement and case edits made for that decision are
  approved automatically instead of asking again. Approve them
  once and the run goes on until every completion condition is met. Per card: *Approve*, *Edit* (change requirements, cases or plan steps yourself, then
  *Save and approve* — recorded in the ledger as written by you and passed on to the agent), or type a correction in the message box
  (the agent revises without approval).
- The agent's **questions** and **permission requests** (`Y` allow · `N` refuse).
- The **handoff** at the end: accept, or keep going by sending a message — sending a message to a stopped or finished run continues it.
  A follow-up builds on the agreed criteria (fix, then re-verify the affected cases; criteria change only if the request changes the scope),
  and an accepted run asks to be accepted again once the follow-up is done. To start over from planning or analysis, start a new task.
- In reference mode, a **license card** when the original carries a restrictive license (e.g. copyleft): confirm you may use the code,
  and the run finishes; the agent waits instead of spending rounds. No license at all (common in internal repos) does not block — it is shown as a note.

Approvals are written to the ledger inside the app only, bound to exactly what was shown; there is no approval endpoint the agent can reach.

**Work branch** — when the chosen folder is an existing git repository, the run creates `feature/<task name>` from `main`
(or `master`) and works only there, leaving the current branch alone. In a parent folder holding several repositories, each repository the agent changes
gets a branch with the same name at the end of the round, carrying those changes; untouched repositories stay as they are. Program changes are committed to that branch after every round — the toolkit's own work folder `.beom/` is never committed; uncommitted
changes that were already there go into a separate first commit. Commands that leave the branch (switch, checkout <branch>, merge, pull,
rebase, reset --hard, branch deletion) are refused by the *leave the work branch* permission kind, and the branch is checked before and
after each round and restored if it moved (the run stops if it cannot be). Turn off *Work on a new branch* on the new-task screen to work
on the current branch. Folders that are not repositories (new projects) are unaffected. Agents can never merge; merging is yours —
press the branch name in the run header (or run `beom-workbench merge <run> develop|main`).

**Branch flow** — feature branches always start from a clean `main` (or `origin/main` when there is no local `main`). `develop` is an
experiment branch: merge a feature into `develop` to try it, and apply it by merging the *feature* into `main`; `develop` itself is never
merged into `main` or into a feature. New work is never blocked by where you are: on `develop`, or on a branch that already contains
`develop` commits, the app still creates a clean feature from `main` — uncommitted changes there are experiments, so they are not
carried over but kept in a named stash (`git stash pop` on that branch brings them back). Uncommitted changes on a clean branch are
carried over as before. The merge menu on the branch name shows whether the work branch is already in `develop` and in `main`, and
offers **Merge into develop to test** and **Apply to main**. Merging never touches your checked-out folder (the result is computed
with `git merge-tree` and only the target branch moves), stops without changing anything on a conflict and lists the files, refuses
to put a branch with `develop` commits into `main`, and never pushes.

**Right panel**

| Tab | Shows |
|---|---|
| Status | progress, plan steps, requirements, completion conditions |
| Criteria | what was agreed — requirements, acceptance cases (input → expected), plan, and who approved them when |
| Checks | what was verified — per requirement, each case's input → expected → actual result, and what every completion condition means |
| Changes | changed files and diffs, per-file revert, checkpoints (the target folder is snapshotted every round; restore any point) |

**Rounds** — a round is one agent session; the runner keeps going round after round until every completion condition is met.
There is no limit by default: a run that makes no progress (ledger, plan or target folder) for three rounds stops by itself. You can set a limit under *Options*, or
choose *confirm once at the end* to have it build and verify before you look at anything.

**Round rules** — set under the run options (applies to every run, and running runs pick up a change at their next decision) or with
`beom-workbench rounds`: `stall_rounds` (no-progress rounds before stopping, default 3), `hard_rounds` (safety cap even when unlimited, 50),
`round_idle_min` (end a round after this many silent minutes, 30), `round_max_min` (end a round that keeps producing output but never finishes, 120 — time spent waiting for your answers is not counted), `notify` (`all`, `important` = done/failed only, or `off`; a run notifies once until you look at the window again, then only flashes the taskbar), `on_limit` (`wait` or `stop` on a usage limit), `limit_wait_max_min`
(stop instead of waiting when the reset is later than this, or when back-to-back limit waits add up past it — e.g. a free plan's weekly limit; default 360), `transient_retries` (5), `fail_rounds` (2) and `cost_budget_usd` (stop a run once it has spent this much — Claude Code only, which reports cost; 0 = no cap; continuing counts again from there).
`beom-workbench rounds --set stall_rounds=5 on_limit=stop`, `--reset` restores the defaults.

**When a round ends in an error** — it is handled apart from "no progress", and the failed round is not counted.

| Cause | Handling |
|---|---|
| Usage limit (usage limit · 429 · quota) | reads the reset time from the message (otherwise 5 → 10 → 20 → 30 → 60 min), waits, then **reruns the same round**. Status *waiting for limit* with time left; sending a message retries at once |
| Transient (overloaded · 5xx · network · timeout) | retries after 30 s → 1 → 2 → 5 → 10 min, then stops and tells you |
| Expired login · bad key | stops at once as *login needed* with how to log in; continue after logging in |
| Conversation too long (context exceeded) | continues in a new conversation — progress is re-read from the ledger |
| Unknown error that ends a round before doing anything | stops after two in a row and shows the error (no endless repeats) |

If you answer a question while approval is pending, a round that folds the answer into the criteria/plan runs first, so you never approve
criteria that miss your answer and get stopped again later. Commands written in an approved plan (e.g. `npm run test:coverage`) are not
asked about again when they run — you saw them when you approved. Commands that were not in the plan follow the permission rules.

**Screens and design** — for work with a UI the plan carries a design direction (tone, colors, type, references, what to avoid) that you
approve with the criteria, and agents follow design rules that avoid the generic "AI look". Screen cases open the page in a real browser
engine, check required text, elements and script errors, and keep a screenshot you can open in the Checks tab. Native desktop apps (Qt · WinForms · WPF · WinUI …, Windows for now) are launched,
read through UI Automation (text and controls), driven (type values, click buttons) and captured as a window screenshot.

**Where data lives** — everything stays on your computer. See [Data and storage](#data-and-storage).

Without the app: `beom-workbench run --mode request --target-root <folder> --request "<request>"` (`--agent codex`, `--resume <run>`); answer in the app.

Install Claude Code by the [official guide](https://code.claude.com/docs) and run `claude auth login`; Codex with `npm i -g @openai/codex`
and `codex login` (check with `beom-workbench agent-status`). Use a subscription login for your own use on your own computer; use an API key
to serve other people ([Anthropic terms](https://code.claude.com/docs/en/legal-and-compliance)).

## Quick start 1: analyze a whole repository

**Delegating to an AI agent** — in Claude Code:

> Analyze this whole repository: https://github.com/<org>/<repo>

The skill follows the procedure below. For large repositories several sub-agents split the units and analyze in parallel.

**Using the CLI directly**

```bash
beom-workbench open https://github.com/<org>/<repo> --ref v1.2.0   # analysis-only clone pinned to a commit → project_id
beom-workbench inventory <pid>                    # file classes, symbols, structure map, entry points, analysis units
beom-workbench features <pid>                     # actual URL and CLI entry-point list

# analysis loop (repeat until no units are left)
beom-workbench next <pid> --agent a1              # take the next unit
beom-workbench context <pid> <unit_id> --agent a1 --raw
beom-workbench finding <pid> <unit_id> --agent a1 --json finding.json   # conclusion + evidence
beom-workbench unknown-add <pid> --agent a1 --json unknown.json        # what could not be confirmed
beom-workbench complete <pid> <unit_id> --agent a1 --summary "what this unit does"

beom-workbench coverage <pid>                     # completion on both axes (files, symbols) and failing items
beom-workbench unknowns <pid>                     # remaining unknowns
beom-workbench export <pid>                       # Markdown + JSON report
```

`finding.json` example:

```json
{"text": "Order creation locks stock first and releases it when payment fails", "severity": "important",
 "evidence": [{"path": "src/order/service.rs", "start_line": 120, "end_line": 158}],
 "symbol_ids": ["s_1a2b3c"]}
```

## Quick start 2: port a feature and verify it

Example: porting the original's `POST /api/v1/orders` into your repository in another language.

```bash
# 1. See the scope → create a porting ledger
beom-workbench slice <pid> "POST /api/v1/orders"
beom-workbench port-create <pid> --name orders --targets "POST /api/v1/orders" --language javascript --target-root <your repo>

# 2. Save the original's responses (golden) — the original runs isolated in a container
beom-workbench port-capture <pid> <port_id> --cases cases.json \
    --source-image <image that runs the original> --source-url http://127.0.0.1:18001 --source-cmd "<command that starts the original server>" \
    --coverage-cmd "<command that writes the coverage report>" --coverage-report cov.json --explore 40

# 3. Write your code → record where each item went
beom-workbench port-map <pid> <port_id> --json map.json

# 4. Differential verification (including concurrent requests)
beom-workbench port-verify <pid> <port_id> --target-url http://127.0.0.1:18002 --target-cmd "node server.js" --concurrency 8

# 5. Progress and next step
beom-workbench port-status <pid> <port_id>

# 6. Human approval — the user runs this in a terminal (refused when an agent runs it)
beom-workbench port-approve <pid> <port_id>

# 7. Completion verdict — issued only when all 7 gates are met
beom-workbench port-finish <pid> <port_id>
```

`map.json` example — `ported` needs a `path:line` in your code; `replaced` and `omitted` need a reason.

```json
[{"item": "s_1a2b3c", "status": "ported", "target": "src/orders/create.js:12"},
 {"item": "s_4d5e6f", "status": "replaced", "note": "use the existing message service instead of the original queue"},
 {"item": "s_7a8b9c", "status": "omitted", "note": "admin statistics are out of scope"}]
```

**Porting with changes**

| Situation | How | Verdict |
|---|---|---|
| Must match the original | just write the case | compared with the original response |
| Intentionally different (value can be specified) | `expect` on the case | compared with the expected value instead of the original; expected value needs human approval |
| New behavior not in the original | `"new": true` + `expect` | the original is not run; judged by the expected value |
| Only the format differs, value cannot be specified | `port-accept --case <name> --reason "<reason>"` | proposed accepted difference → human approval |
| Part of the feature is dropped | `omitted` + reason in `port-map` | human approval |

- The approval screen shows the original response next to the expected value (or the actual difference). If the content changes after approval, it must be approved again.
- If the original cannot be instrumented for coverage, measure your code instead with `port-verify --target-coverage-cmd ... --target-coverage-report lcov.info`.
- When edits to your code shift lines, update the ported locations with `port-map --follow`.

## Quick start 3: build from a request

Pick **Request based** in the app, or with the CLI:

```bash
beom-workbench make-create --name order-total --request "Total from quantity. 2000 each, 10% off for 10 or more, 400 for 0 or less" --target-root <folder>
beom-workbench make-req <pid> <port_id> --json reqs.json          # [{"text": "total = qty x 2000"}, …] → R1, R2 …
beom-workbench port-capture <pid> <port_id> --cases cases.json --explore 20   # expect + req for each requirement
beom-workbench port-approve <pid> <port_id>                       # approve requirements and criteria before building
beom-workbench port-map <pid> <port_id> --json map.json           # requirement → implementation "path:line"
beom-workbench port-verify <pid> <port_id> --target-url http://127.0.0.1:8080 --target-cmd "<command that starts the server>"
beom-workbench port-finish <pid> <port_id>
```

- Each case needs `req` (which requirement it checks) and `expect`; every requirement needs at least one case.
- Automatically varied inputs are judged by **no crash and no 5xx** instead of expected values (odd input must be rejected with a 4xx).
- If you build into an existing repository, `open` and `inventory` it first and pass `--context <its project_id>` so the build follows its structure.

## Customizing the original in place

```bash
beom-workbench extpoints <pid>                                  # first, places to plug in without touching the core
beom-workbench impact <pid> pricing.calc_total --verify         # entry points, tests and conclusions affected by a change
beom-workbench custom-add <pid> --description "change discount calculation" --kind modify --targets pricing.calc_total

# when a new upstream version is released
beom-workbench open <repo> --ref v1.3.0 && beom-workbench inventory <new pid>
beom-workbench compare <base pid> <new pid> --save              # what changed + where it collides with your modifications
```

To verify the modified original against the unmodified one, use the modified repository as `--target-root` and follow [Quick start 2](#quick-start-2-port-a-feature-and-verify-it).

## Case file format

```json
{
  "defaults": {"headers": {"Content-Type": "application/json"}},
  "cases": [
    {"name": "createOrder", "method": "POST", "path": "/api/v1/orders", "json": {"sku": "A1", "qty": 2},
     "match": {"id": "[0-9a-f]{32}"}, "ignore": ["createdAt"]},
    {"name": "getOrder", "method": "GET", "path": "/api/v1/orders/{{createOrder.id}}"},
    {"name": "zeroQty", "method": "POST", "path": "/api/v1/orders", "json": {"sku": "A1", "qty": 0}, "compare": "status"},
    {"name": "bulkDiscount", "method": "POST", "path": "/api/v1/orders", "json": {"sku": "A1", "qty": 10},
     "expect": {"status": 201, "equals": {"total": 18000}}},
    {"name": "coupon_new", "new": true, "method": "POST", "path": "/api/v1/orders",
     "json": {"sku": "A1", "qty": 1, "coupon": "WELCOME"}, "expect": {"equals": {"discount": 1000}}}
  ]
}
```

| Field | Meaning |
|---|---|
| `method`·`path`·`json`·`headers` | HTTP case |
| `argv`·`stdin` | CLI case |
| `call: {source, target}`·`args`·`kwargs` | function-level comparison (`path:function`, languages may differ) |
| `{{caseName.field}}` | use a value from an earlier case's result (no spaces in the name) |
| `ignore` | paths not compared (`items.*.t`) |
| `match` | check with a regex instead of the value (ids, timestamps) |
| `compare: "status"` | compare the status code only |
| `expect` | compare with an expected value instead of the original — `status`·`body`·`equals` (path: value, arrays as `items.0.price`)·`match` |
| `new: true` | behavior not in the original (`expect` required; not run against the original) |
| `screen: {path \| file, mobile}` · `expect: {visible, selectors, no_errors}` | screen case — opens the page, checks text, elements and script errors, keeps a screenshot |
| `screen: {app, actions, title}` · `expect: {visible, selectors: ["Button:Calculate"], no_errors}` | native app screen (Windows) — `app` is an executable or command, `actions` are `{"set": "Edit:Qty", "value": "10"}` · `{"click": "Button:Calculate"}`; selectors are "Type:Name"; no_errors means the app did not die and stderr has no exception. The launch command goes through the command rules |
| `description` | what the case checks, in plain words — shown instead of the name |
| `explore: false`·`concurrent: false` | exclude from automatic input variation / concurrency checks |

Keys outside this table are rejected when the file is saved (a misspelled key such as `args` instead of `argv` would otherwise run the case with different input). In request mode every case also needs `req`.

## Command summary

| Group | Commands |
|---|---|
| Setup | `doctor` `setup` `open` `list` `inventory` `classify` `agent-status` |
| App and runs | `app` `run` `runs` `data` |
| Analysis loop | `next` `claim` `renew` `context` `finding` `withdraw` `mark` `unknown-add` `unknown-update` `complete` `review` `pending` |
| Deeper checks | `lsp` (L1) `dataflow` (L2) `runtime-ingest` `runtime-run` `runtime-runs` (L3) |
| Results | `coverage` `features` `units` `unknowns` `export` `license` |
| Customizing | `extpoints` `impact` `custom-add` `custom-list` `custom-update` `compare` |
| Request-based build | `make-create` `make-req` (then the shared port-* commands) |
| Porting | `slice` `port-create` `port-map` `port-capture` `port-verify` `port-accept` `port-status` `port-approve` `port-finish` |

Options for each command: `beom-workbench <command> --help`. Every command prints `{ok, data, warnings, errors}` JSON;
on failure `errors[0].recovery` tells you how to fix it. The detailed agent procedure is in the skill documents (SKILL.md and CLI.md of the `beom-workbench` skill).

## Data and storage

Like Claude Code (`~/.claude`) and Codex (`~/.codex`), everything lives in one folder in your home directory
(`~/.beom-workbench`, change with `BEOM_WORKBENCH_HOME`), split by what it is. Nothing is sent anywhere, and no secrets are stored —
keys and tokens are read from environment variables only and masked in output.

| Folder | What | Delete? |
|---|---|---|
| `settings.json` | your preferences (cleanup, defaults) | yes — back to defaults |
| `projects/<id>/` | analysis/porting ledger `ledger.db` (SQLite, versioned schema, hash chain), the pinned clone of the original, golden responses | **no** — the record of approvals and verification |
| `runs/<run>/` | one automatic run = one conversation: `run.json` state, `events.jsonl` append-only timeline, `inbox/` questions and approval requests, `shadow.git` checkpoints of the target folder, `agent/` agent settings regenerated every round | move to the trash instead |
| `trash/` | deleted runs, emptied automatically after 7 days | yes |
| `app/` | app icon used by the shortcuts | no |
| `cache/` · `cbm-cache/` | agent status, verification harness, structure-map index | yes — rebuilt |
| `work/` | temporary copies for verification and coverage, removed after use (leftovers older than 6 hours are swept at start-up) | yes |
| `bin/` · `engines/` · `node/` | analysis engines installed by `setup` | yes — reinstall with `setup` |

The agent's own conversation stays where its CLI keeps it (`~/.claude/projects`, `~/.codex/sessions`) and is resumed by session id.
In your target folder the toolkit keeps its work files in `.beom/` (acceptance cases, planning documents, screenshots). They are never committed —
only the program is — and in a git repository the folder is listed in the repository's local exclude file so `git status` stays clean.

| | Claude Code | Codex | this toolkit |
|---|---|---|---|
| Conversation | `projects/<folder>/<session>.jsonl` | `sessions/YYYY/MM/DD/*.jsonl` | `runs/<run>/events.jsonl` (+ the agent's own transcript) |
| Settings | `settings.json` | `config.toml` | `settings.json` |
| State | file-history checkpoints | versioned SQLite | per-project SQLite ledger (versioned, hash chain) + git checkpoints per run |
| Cleanup | old conversations deleted after 30 days by default | kept | kept by default; optional automatic cleanup of finished or hidden runs after 30/90 days; trash emptied after 7 days |

Deleting a run never removes approvals or verification results — those stay in the project ledger. Emptying the trash also removes
the request-mode ledgers that only those runs used; other ledgers no run uses are listed under **Storage** with their size, to delete
yourself (an agent may have analyzed them directly). At start-up the app also removes downloaded link documents older than 14 days
(they were copied into the target folder when the run started) and other leftovers.

```bash
beom-workbench data                        # where everything is and how big
beom-workbench data --trash <run>          # move a run to the trash (--restore <name>, --empty-trash)
beom-workbench data --prune 30 --dry-run   # finished or hidden runs untouched for 30 days
beom-workbench data --set-cleanup 30       # clean those up automatically at start-up (0 = off)
beom-workbench data --delete-project <id>  # delete a ledger no run uses
beom-workbench data --clear-browser        # sign out of sites used for document links
beom-workbench data --revoke-sandbox       # Windows: remove the folder permissions given to the sandbox
```

In the app: **Storage** at the bottom of the sidebar, and **Move to trash** in a run's ⋯ menu.

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `BEOM_WORKBENCH_HOME` | `~/.beom-workbench` | location of ledgers, clones, engines and backups |
| `BEOM_WORKBENCH_CONTAINER` | `auto` | isolated execution engine: `auto`·`docker`·`wsl`·`none` |
| `BEOM_WORKBENCH_WSL_DISTRO` | `beom-sandbox` | WSL distribution used on Windows |
| `BEOM_WORKBENCH_SANDBOX_DIR` | `%LOCALAPPDATA%\beom-workbench-sandbox` | Windows: where the built-in isolation records which folders it granted |
| `BEOM_WORKBENCH_NO_UPDATE_CHECK` | — | set to `1` to stop the app checking the public release list for a new version |
| `BEOM_WORKBENCH_LEASE_SECONDS` | `3600` | analysis unit lease time |
| `BEOM_WORKBENCH_UNIT_MAX_LINES` / `_UNIT_MAX_CHARS` | `1500` / `45000` | analysis unit size limits |
| `BEOM_WORKBENCH_CONTEXT_MAX_CHARS` | `24000` | page size of `context` |
| `BEOM_WORKBENCH_LSP_TIMEOUT_SECONDS` / `_LSP_IDLE_SECONDS` | `900` / `600` | language server response timeout / idle shutdown |
| `BEOM_WORKBENCH_RULEPACKS` | — | additional entry-point rule folder |
| `BEOM_WORKBENCH_CBM` · `_ASTGREP` · `_JOERN` · `_JAVA_HOME` | auto-detected | explicit engine executables |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code config folder the skill is installed into |

## Safety principles

- **The analyzed code is never executed.** Language server build scripts and macro expansion are turned off as well. Verification that needs execution
  (coverage, porting originals) runs in a container with the network blocked, or in a temporary copy only for repositories the user says they trust.
- **Secret files are not read**, and secrets inside ordinary files (passwords, keys, tokens, URL credentials) are masked as `****` in output.
  Tokens and keys are also stripped from the environment passed to executed commands.
- Analysis runs only on **a dedicated clone at a pinned commit**. Uncommitted changes in your working folder are not included.
- Human approval for porting is taken only in the desktop app, the MCP confirmation dialog or the user's own terminal input — never from an agent during an automatic run.
- **The agent cannot approve its own work.** It runs as your user, so code it writes and runs (tests, builds) could edit the toolkit's
  records directly — no key or signature can stop that on the same account. The app is therefore the only authority: it remembers
  every decision you make in it, compares plan approval, permission rules, acceptance and ledger approvals before and after each
  round, and if something changed that the app did not do, it restores what it can and stops the run until you confirm.
- **Isolated execution (sandbox).** Commands the agent runs are confined by the operating system to the target folder, the temp
  folder and the usual tool caches (npm, pip, cargo, Gradle, Maven, Go, NuGet) — writing anywhere else, including the toolkit's
  records, fails with "access denied". It is part of first-time setup: the agent list shows **Isolated execution · Ready**, or
  **Set up** when something is missing (also covered by **Install all**).

  | Platform | Claude Code | Codex | Gemini (agy) |
  |---|---|---|---|
  | Windows | Claude's own sandbox is off on Windows, so the app has Claude use PowerShell and runs every allowed command in its built-in Windows sandbox — a write-restricted token, no admin rights and nothing to install (works on Windows Home) | own sandbox | `--sandbox` |
  | macOS | Claude Code's built-in sandbox (Seatbelt), turned on by the app — nothing to install | own sandbox | `--sandbox` |
  | Linux | Claude Code's sandbox (needs `bubblewrap` and `socat`; setup installs them) | own sandbox | `--sandbox` |

  How the Windows sandbox works: commands get a write-restricted copy of your own token (all privileges removed), so a write must also be
  allowed for a sandbox-only identity; the app grants that identity *modify* on the target folder, the temp folder and the tool caches it
  uses (the grant stays on those folders and is recorded in `%LOCALAPPDATA%\beom-workbench-sandbox\granted.txt`). Reading is unchanged,
  so builds and tests work. The network cannot be limited this way on Windows — the *web* permission rule decides. Git Bash cannot start
  under a restricted token, which is why Claude uses PowerShell there. Checked end to end on Windows: inside writes work, outside writes
  and writes to `~/.beom-workbench` are denied (also when the path is built at run time so the permission rules cannot see it),
  `npm install`, `node`, `python`, `pip` and `git` work, and no window appears. On macOS and Linux, retrying outside the sandbox is
  disabled and network access (when web is allowed) is limited to package registries. Global installs (`npm -g`, `pip --user`) are
  refused by design. Turn it off per run in the run options.
- **Permission rules** classify every agent tool call and command: edits inside the target, build/test commands, other commands,
  web, outside the folder, publish (push, package publish, PR creation), delete outside the folder, system changes (registry,
  scheduled tasks, git config, the agent's own `.claude/`/`.git/` settings), and leaving the work branch. Publish, outside deletes,
  system changes and branch changes are refused in every mode.
- **Existing repositories are worked on a new branch** (`feature/<task>` from `main`, never from `develop`) and committed per round; the app's own commits
  skip repository hooks. Uncommitted work you had goes into a separate first commit.
- Logged commands and tool inputs are masked (tokens, keys, connection-string passwords).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| An agent shows **install needed** although you installed it | Usually PATH: e.g. `nvm use` switched Node and the npm-installed CLI is no longer on it. The app re-reads PATH on every check and runs Codex's native binary inside the npm package directly, so it keeps working across Node versions. If it is really missing, press **Install** (official stand-alone installers, no Node needed) or **Install all missing**. |
| **Login needed** | Press **Login** — a terminal opens; finish there and come back, the app re-checks. |
| **Isolated execution · Set up** | Linux only: `bubblewrap` and `socat` are missing — press **Set up**. Windows needs nothing (built in) and macOS has it built in. |
| **Isolated execution · Set up** again on Ubuntu 24.04 or later | `bubblewrap` is installed but AppArmor blocks the user namespaces it needs (on 26.04 also inside `bubblewrap` itself). The app checks this by actually starting a sandbox. **Set up** lifts the restriction system-wide (admin password; it persists): sets `kernel.apparmor_restrict_unprivileged_userns=0` and, where present, disables the `bwrap-userns-restrict` profile. |
| **Analysis engines · Install** fails with *rate limit* | Many PCs behind one address (an office network) share GitHub's hourly API limit. The installer then downloads the same release files directly, still checksum-verified; setting `GITHUB_TOKEN` also raises the limit. |
| Installing an agent fails with *Get-FileHash is not recognized* | The app was started from a PowerShell 7 terminal, whose module path breaks Windows PowerShell. The app now clears it at start; restart the app once. |
| **Analysis engines · Install** | Reference mode analyzes the original repository with two engines (structure map, entry-point rules). Press **Install** — the official releases are downloaded and checksum-verified. |
| Where did my uncommitted `develop` changes go? | Starting a task from `develop` keeps them in a stash named after the run (`git stash list`); switch back to `develop` and run `git stash pop`. |
| **Merge** says *conflict* | Nothing was changed. Merge by hand (or fix the work branch) — the message lists the conflicting files. |
| An attached link says **login needed** | The app opens its own browser window for that page — sign in there, then press **Get this document**. Starting a run with such a link still attached asks for the sign-in first. |
| A terminal window flashes up | Child processes are started without windows. The one known source was an agent CLI's own auto-updater opening a console when the app checked logins (about every 15 minutes); the app now disables CLI auto-update for the processes it starts. Update the CLIs yourself from a terminal. |
| macOS: agents not found when opened from Finder | The app reads your login shell's PATH at start (Homebrew, nvm, `~/.local/bin`). |
| Reference analysis fails with a permission (DACL) error or *could not load saved runtime settings* | The structure-map engine refuses folders whose parent folders another account can write to. The app keeps its engine folders under `~/.beom-workbench`; this only happens if the data folder was moved (e.g. `BEOM_WORKBENCH_HOME` under `AppData`) — keep it under your user folder. |
| A build needs a tool you don't have (e.g. OpenSSL for a Rust project on Windows) | When you pick a folder, the new-task screen lists missing build tools (Rust, OpenSSL, Node.js, JDK, Go) with an **Install** button (winget / Homebrew); the agent is told up front instead of stopping mid-run. Once OpenSSL is installed, `OPENSSL_DIR` is set for the agent automatically. |
| Checks fail with "could not connect to the server" | The server was not running during the check — usually it could not be built (missing build prerequisites). The Checks tab groups failures by cause. |
| A run stopped with "decisions changed outside the app" | See *Safety principles* — review the Changes tab, then press **Confirmed** to continue. |

## Known limitations

| Limitation | Mitigation |
|---|---|
| Differential verification proves only the inputs that were run | Automatic input variation and coverage widen the scope; the remaining scope is recorded in the evidence limitations. Performance is not compared |
| Coverage proves only that code ran | Which branch or input must be checked with cases |
| Name-based call relations are candidates | Need a language server (L1) or execution (L3) to count as evidence |
| Dynamic dispatch, reflection, macro-generated code | Automatically recorded as unknowns |
| The data-flow engine is slow the first time (about 15 minutes for 10k lines of Java) | Cached; build ahead with `dataflow --build-only` |
| The CLI starts language servers per call | Queued across processes per language; MCP mode reuses servers |
| Routes that depend on configuration values | Expanded with defaults and marked as unknowns |
| A local program cannot stop someone with the same permissions from deliberately forging records | Forgery attempts surface as gate failures; the final verdict can be re-run in a clean environment |

## Security and contributions

- Report vulnerabilities privately via GitHub's **Report a vulnerability** (Security tab) — see [SECURITY.md](SECURITY.md).
- Bug reports and feature requests are welcome as issues. Pull requests are welcome as suggestions but are not merged as they are —
  the license does not allow modified versions, so the maintainer applies accepted changes. See [CONTRIBUTING.md](CONTRIBUTING.md).
- Branches: `main` holds releases (each tagged `vX.Y.Z`). Work happens on short-lived `feature/…` or `fix/…` branches created from
  `main`; a branch is tried on `develop` (the experiment branch) and applied by merging that same branch into `main` through a pull
  request with CI on Windows, macOS and Linux. `develop` is never merged into `main` — a required *branch-policy* check fails any pull
  request into `main` that comes from `develop` or contains `develop`-only commits, and only `feature/`, `fix/` and `hotfix/` branches
  may be merged into `develop`. `main` and `develop` are protected and only the maintainer can change them.

## License

**Use permitted, no modification** — [LICENSE](LICENSE) (this is not an open source license). The Korean text of the LICENSE governs.

- Anyone may install and use the unmodified software (including business and commercial use). Output you create with the toolkit is yours.
- Modifying it, creating derivative works, distributing modified versions or putting any part of the code into other software requires the author's written permission.
- Suggestions are welcome as issues or pull requests in this repository (pull requests are applied by the maintainer, not merged as they are).

Libraries and external engines keep their own licenses — [THIRD_PARTY.md](THIRD_PARTY.md).
