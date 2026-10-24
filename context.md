# Pharma Pilot — Project Context

> Reference for anyone, human or AI, who needs to understand what this project is, how it is built, and where things live. Written from the code on 2026-10-10. When code and this file disagree, the code is right and this file needs an edit.

## 1. What it is

Pharma Pilot (repository `alexandru-tanul/pharma-pilot`, live at https://pharma-pilot.alexandrutanul.com) is an AI research assistant for pharmaceutical questions. A user asks in plain English about drugs, diseases, clinical trials, or protein targets. An LLM turns the question into SQL against three biomedical databases held locally as Parquet files, DuckDB runs the queries, and the model writes an answer with tables and source attribution. Answers stream to the browser while they are generated.

It is a full-stack monolith: FastAPI serving Jinja2 templates with HTMX for interactivity, Server-Sent Events for live updates, no JavaScript framework and no front-end build step.

## 2. Technology stack

| Layer | Technology | Notes |
|---|---|---|
| Web framework | FastAPI on Uvicorn | `app/main.py`; `fastapi[standard]` brings uvicorn |
| Language | Python 3.13 | `.python-version`; `pyproject.toml` allows >= 3.10 |
| Package manager | uv | `uv.lock`; package name `pharma-pilot` |
| Biomedical data | DuckDB, in-memory, views over Parquet | `app/duckdb.py`; one global connection, queries run in `asyncio.to_thread` |
| Chat history | PostgreSQL 17, Tortoise ORM, Aerich migrations | `app/models.py`, `migrations/` |
| LLM gateway | LiteLLM | any provider it supports; default `gemini/gemini-3-flash-preview` |
| Settings | pydantic-settings | `app/config.py`, loads `.env`, ignores unknown keys |
| Templates | Jinja2 with jinja2-fragments | block rendering for partial responses |
| Front end | HTMX 2.0.4, Idiomorph 0.3.0, Tailwind CSS v4 browser build, Iconify, Inter font | all from CDNs in `templates/base.html` |
| Markdown | markdown2, server side | `md()` filter in `main.py`; no client-side renderer |
| PDF export | WeasyPrint | `templates/report.html`, `static/report.css` |
| Data downloads | httpx, tarfile | `app/data_sync.py` |
| Containers | Docker Compose, multi-stage Dockerfile | `docker-compose.local.yml`, `docker-compose.production.yml` |
| Task runner | just | `Justfile` |

## 3. Repository layout

```
app/
  main.py                 FastAPI app, middleware, Jinja filters, all routes
  config.py               Settings (Postgres, model, paths)
  lifespan.py             startup/shutdown: DuckDB init, Tortoise init, PG LISTEN task
  deps.py                 get_session_key dependency (signed session cookie)
  models.py               Chat and Message models
  prompts.py              SYSTEM_PROMPT, LLM_TOOLS, EXAMPLE_PROMPTS
  duckdb.py               DuckDB connection, Parquet views, simplified views, reload
  data_sync.py            download/update datasets from GitHub releases; CLI; jobs
  services/
    llm/__init__.py       agentic loop: streaming, tool calls, persistence
    llm/context.py        token budgeting and context trimming
    clinical_trials/      query_clinical_trials + schema summary (AACT)
    drugcentral/          query_drugcentral + schema summary
    pharos/               query_pharos + schema summary (TCRD)
    sql_errors.py         friendly messages for DuckDB errors
    sse/__init__.py       subscriber registry, notify_chat_update, notify_content_stream
  templates/
    base.html             sidebar (New Chat, About, Data, history, search), layout
    index.html            landing page with example prompts
    chat.html             conversation, SSE client, feedback, export, tool-result modal
    about.html            methodology page
    data.html             dataset download/update page
    report.html           PDF report layout
    partials/             tool_results.html, feedback_buttons.html, data_rows.html
  static/report.css       A4 stylesheet for the PDF
migrations/
  models/0_*.py           Aerich initial migration (ignored by the .gitignore pattern */migrations/*.py)
  003_add_usage_to_messages.sql, 004_add_feedback_to_messages.sql   hand-written, applied manually
data/parquet/<dataset>/   Parquet files, ignored by git, downloaded by data_sync
data/*.sql                legacy PostgreSQL view DDL, no longer used
Dockerfile, docker-compose.*.yml, Justfile, pyproject.toml, uv.lock, .env.example
```

## 4. The three data sources

Published monthly as GitHub releases of `scriptogre/biomedical-data`, tagged `<dataset>/<YYYY-MM-DD>`, each with `<dataset>.tar.gz` and `<dataset>.sha256`. About 4 GB in total. Installed under `data/parquet/<dataset>/`, with the release tag in `data/parquet/<dataset>/.release` and the archive checksum in `data/parquet/<dataset>.sha256`.

At startup `duckdb.init()` opens an in-memory connection and, for every `*.parquet` file, creates a view `<dataset>_<file>` over `read_parquet(...)`. A dataset with no files is skipped with a warning. Then it creates unprefixed aliases and simplified views that the LLM targets:

**ClinicalTrials.gov (AACT), 49 files.** Aliases: `studies, interventions, conditions, browse_conditions, sponsors, outcomes, outcome_analyses, eligibilities, design_groups, outcome_measurements, result_groups`. Views: `v_studies, v_study_drugs, v_study_conditions, v_drug_trials, v_condition_trials, v_drug_condition_trials, v_trial_summary, v_trial_outcomes, v_trial_results, v_trial_eligibility, v_trial_arms, v_trial_measurements`. All AACT columns are VARCHAR; DuckDB casts implicitly.

**DrugCentral, 67 files.** Aliases: `structures, synonyms, act_table_full, approval, active_ingredient, product, struct2atc, atc, omop_relationship, target_dictionary, pharma_class, ddi, ddi_risk, faers, property, property_type, identifier, id_type`. Views: `v_drug_info, v_drug_indications, v_drug_targets, v_disease_drugs, v_drug_classification, v_drug_interactions, v_adverse_events` (FAERS rows with `llr > llr_threshold` only), `v_drug_approvals, v_drug_properties, v_drug_identifiers, v_drug_atc`.

**Pharos / TCRD, 92 files.** Aliases only: `protein, target, t2tc, disease, ncats_disease, ncats_ligands, ncats_ligand_activity, drug_activity, ncats_ppi, tinx_novelty, alias`. Key joins: `protein.id = t2tc.protein_id`, `t2tc.target_id = target.id`, `disease.protein_id = protein.id`, `ncats_ligand_activity.target_id = protein.id`, `tinx_novelty.protein_id = protein.id`. Other files, for example `cmpd_activity`, are reachable only by their prefixed name `pharos_cmpd_activity`.

Each service module exposes `get_schema_summary()`, which is embedded into the tool descriptions in `prompts.py`. That summary is the single source of truth the model sees; keep it aligned with `duckdb.py`.

## 5. Runtime behaviour

### 5.1 Startup (`app/lifespan.py`)
1. `duckdb.init()` builds the views and records a disk signature (release tag and file count per dataset).
2. `data_sync.set_reload_hook(duckdb.reload)` so a finished download reloads DuckDB in the process that ran it.
3. `Tortoise.init()` with the Postgres config, then `generate_schemas(safe=True)`; failures are printed, not fatal.
4. A background task opens an asyncpg connection and listens on channel `chat_updates`.
Shutdown cancels the listener, closes Tortoise, closes DuckDB.

`duckdb.ensure_current()` compares the disk signature with the one recorded at init and reloads if it changed. It is called on `/`, on the `/data` routes, and before every query, so every worker process picks up datasets installed by another.

### 5.2 Sending a message
- `POST /send` takes the message from the form; the active chat comes from the `chat_id` cookie, which `GET /chats/{id}` sets, and `GET /` and delete clear.
- A new chat gets the title `message[:50]` plus an ellipsis, and the response carries `HX-Redirect: /chats/{id}`. A follow-up returns 204; the open SSE stream updates the page.
- The user message is saved, `notify_chat_update` fires, and `generate_response` runs as a background task.

### 5.3 The agentic loop (`services/llm/__init__.py`)
- Builds the message list from the chat (system prompt first), strips non-API keys (`msg_id`, `feedback`), and calls `litellm.acompletion` with `stream=True`, `timeout=60`, `max_tokens=4096`, `tool_choice="auto"`.
- An empty assistant row is created before the call and updated while streaming (at most every 50 ms or 5 characters) so the UI shows text as it arrives. Tool calls are saved as soon as their name is known so the UI can show "Querying X".
- Each tool call runs `execute_tool`, which dispatches to `query_clinical_trials`, `query_drugcentral`, or `query_pharos`; the result is saved as a `tool` message and the loop repeats.
- Error handling: a tool result starting with a known error prefix counts as a strike; after 3 consecutive strikes the model is told to rethink. JSON argument failures become an error tool result, not a crash.
- Budget: when cumulative tokens pass 200k, one final call with `tool_choice="none"` forces a synthesis.
- `execute_tool` currently returns `usage=None`, so `Message.usage` is never written and the per-tool token counters in `chat.html` never render. Known gap.

### 5.4 Context management (`services/llm/context.py`)
- Limits come from `litellm.get_model_info` (fallback 128k context, 4096 output), minus a 1024-token margin. `max_result_chars` is 40 % of the available budget times 4, and caps every tool result.
- Tokens are counted with `litellm.token_counter`, falling back to 4 characters per token.
- Trimming stages: (1) shrink old tool results, (2) drop middle turns while keeping the system prompt, the first turn, and the most recent turns, inserting a "[N earlier conversation turn(s) removed]" marker, (3) halve the largest tool result at a newline boundary, up to 10 times, never below 300 characters.

### 5.5 SQL validation and execution (each `services/<db>/query.py`)
- Only a single `SELECT` or `WITH` is allowed; forbidden keywords: `INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, DETACH, VACUUM, REINDEX, TRUNCATE`. A trailing semicolon is stripped.
- Queries run through `duckdb.execute_query_raw`, results are cut to 200 rows and formatted as a pipe-delimited text table for the model, then capped at `max_result_chars`.
- DuckDB errors are translated by `sql_errors.py` into messages the model can act on.

### 5.6 Live updates (SSE)
- `GET /chat/{id}/updates` is a `StreamingResponse`; each client registers an `asyncio.Queue` in `chat_update_subscribers`.
- Structural changes (new message, tool call, stream end) go through PostgreSQL: `NOTIFY chat_updates, '<chat_id>'`. The listener in `lifespan.py` pushes a `{"type": "full"}` event to every subscriber of that chat; the endpoint re-renders the `content` block of `chat.html` and sends it as event `chat-update`.
- Streamed text bypasses Postgres: `notify_content_stream` pushes `{type: "stream", msg_id, content}` to in-process queues; the endpoint renders the Markdown and sends event `content-stream` with `{msgId, html}`.
- Queued events are coalesced; a keepalive comment goes out every 15 s.
- `chat.html` uses a native `EventSource`, applies `chat-update` with `Idiomorph.morph`, then `htmx.process`; `content-stream` replaces the HTML of one message.

## 6. Routes (`app/main.py`)

| Method, path | Purpose |
|---|---|
| GET `/` | landing page; redirects to `/data` if any dataset is missing |
| GET `/about` | methodology page |
| GET `/data` | dataset status with Download/Update buttons |
| GET `/data/status` | rows partial, polled every 2 s while a job runs; `?refresh=true` re-queries GitHub |
| POST `/data/{name}/update` | start a background download of the newest release |
| GET `/chats/{id}` | conversation page, sets the `chat_id` cookie |
| GET `/chats/{id}/export` | PDF report of the conversation (WeasyPrint) |
| POST `/chats/{id}/delete` | delete; `HX-Redirect: /` if it was the active chat, else `HX-Trigger: refreshChats` |
| GET `/chat?search=` | sidebar chat list partial |
| GET `/chat/{id}/updates` | SSE stream |
| POST `/send` | send a message |
| POST `/messages/{id}/feedback` | toggle positive/negative feedback, returns the button partial |
| GET `/tool-results/{tool_call_id}` | full tool result for the modal |
| `/static` | static files mount |

All chat routes are scoped by `session_key`, a value kept in the signed session cookie (`SessionMiddleware` with `SECRET_KEY`).

## 7. Database schema (PostgreSQL)

`app_chat`: `id`, `title VARCHAR(200)`, `session_key VARCHAR(40)` nullable, `created_at`, `updated_at`.

`app_message`: `id`, `chat_id` FK with `ON DELETE CASCADE`, `role VARCHAR(20)`, `content TEXT` nullable, `tool_calls JSON` nullable, `tool_call_id VARCHAR(100)` nullable, `name VARCHAR(100)` nullable, `usage JSON` nullable (never populated, see 5.3), `feedback VARCHAR(10)` nullable, `created_at`.

`Message.as_llm_dict()` returns the OpenAI message shape plus `msg_id` and `feedback`; both are stripped before an LLM call. `Chat.as_openai_api_format()` returns the ordered list.

Migrations: Aerich manages `migrations/models/`. The two `.sql` files (`usage`, `feedback` columns) were applied by hand; `generate_schemas(safe=True)` does not add columns to existing tables, so a fresh database gets them, an old one needs the SQL.

## 8. Prompts (`app/prompts.py`)

`SYSTEM_PROMPT` (about 200 lines) sets the persona, the tool-use rules, and the answer format. Rules of note: the model must write a short plan (2 to 4 sentences) before its first tool call; answers are short paragraphs with tables where useful; every claim cites its database. `LLM_TOOLS` defines the three SQL tools, each with the schema summary from its service. `EXAMPLE_PROMPTS` are the landing-page cards, not part of the prompt.

Known staleness: parts of the prompt still describe the earlier API-based tools (parameter-style searches, locations, references, participant flow). Those are not exposed as views today; when editing the prompt, derive capabilities from `duckdb.py` and the schema summaries.

## 9. Front end

- Dark theme (`#0a0a0a`), Tailwind utility classes, Inter font, Iconify icons.
- Sidebar: logo, New Chat, About, Data, searchable history with delete (with `confirm()`).
- Chat page: user and assistant bubbles, assistant Markdown rendered server side, tool calls as `<details>` with a database badge (ClinicalTrials blue, DrugCentral amber, Pharos purple) and a 2000-character preview, a modal (`<dialog>`) for the full result, thumbs up/down on each assistant message, an Export PDF button, status text "Thinking…", "Querying {database}…", "Synthesizing results…".
- Data page: one card per dataset with installed version, latest release, size, a progress bar while downloading, and a "Check for new releases" link.

## 10. Dataset download and update (`app/data_sync.py`)

- Independent of `app.config`; reads `PARQUET_DIR` from the environment, default `<repo>/data/parquet`.
- `fetch_latest_releases()` lists the releases through the GitHub API (optional `GITHUB_TOKEN` raises the rate limit), keeps the newest tag per dataset, caches for 10 minutes.
- `download_dataset(name)` streams the archive to `data/parquet/.tmp/`, hashes while downloading, compares with the published checksum, extracts with `tarfile` (`filter="data"`), stages the Parquet files, swaps the live directory atomically, writes `.release` and `<name>.sha256`, and cleans up.
- Jobs: `start_update(name)` schedules `_run_job` as an asyncio task; progress is written to `data/parquet/.tmp/<name>.job.json` so other worker processes can show it; a job with no progress write for 120 s is reported as interrupted. After install the reload hook reloads DuckDB in the owning process; other processes catch up through `ensure_current()`.
- CLI: `python -m app.data_sync status | download | update [names]`. `download` fetches only missing datasets, `update` also replaces outdated ones. The Justfile wraps these as `download-data`, `update-data`, `data-status` (run inside the container).

## 11. Docker and deployment

- `Dockerfile`: builder stage from `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`, venv installed into `/usr/local`, `uv sync --locked --no-dev`; runtime stage `python:3.13-slim-bookworm` with the WeasyPrint system libraries and Liberation fonts, non-root user with `UID`/`GID` build args, no `CMD`: the command comes from compose.
- `docker-compose.local.yml`: `fastapi` (uvicorn with `--reload`, source bind-mounted, Parquet directory bind-mounted read-write), `postgres:17-bookworm` tuned for speed over durability, `pgweb` on 8081.
- `docker-compose.production.yml`: same services, uvicorn with `--workers 4`, `restart: always`, env passthrough for API keys, `MODEL` default `claude-haiku-4-5`, `DEBUG` default False, `pgweb` included. Whether the services join an external `proxy` network depends on the host; see the compose file in use.
- Multi-worker caveat: `chat_update_subscribers` and `notify_content_stream` are per process, so streamed text reaches only SSE clients connected to the worker that runs the generation task; structural updates still reach everyone through Postgres. Dataset jobs and DuckDB reloads are handled across workers as described in section 10.

## 12. Configuration (`app/config.py`, `.env.example`)

| Variable | Default | Notes |
|---|---|---|
| `POSTGRES_HOST` | none, required | `postgres` in compose |
| `POSTGRES_PORT` | 5432 | |
| `POSTGRES_USER` | `postgres` | |
| `POSTGRES_PASSWORD` | none, required | |
| `POSTGRES_DB` | `app_data` | |
| `MODEL` | `gemini/gemini-3-flash-preview` | any LiteLLM model name |
| `GEMINI_API_KEY` | empty | the only key declared in Settings |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` | not declared | read by LiteLLM from the process environment |
| `SECRET_KEY` | `your-secret-key-here` | insecure default, set it |
| `DEBUG` | True | template auto-reload and cache |
| `PARQUET_DIR` | `<repo>/data/parquet` | used by both `config.py` and `data_sync.py` |
| `GITHUB_TOKEN` | unset | optional, for release checks |

Derived: `DATABASE_URL`, `TORTOISE_ORM` (pool 5 to 20), `BASE_DIR`, `TEMPLATES_DIR`.

## 13. Dependencies (`pyproject.toml`)

`fastapi[standard]`, `jinja2`, `jinja2-fragments`, `python-multipart`, `tortoise-orm[asyncpg]`, `asyncpg`, `aerich`, `pydantic`, `pydantic-settings`, `litellm`, `httpx[http2]`, `markdown2`, `itsdangerous`, `duckdb`, `weasyprint`. No dev dependency group.

## 14. Known issues and next steps

- Token usage per tool call is never recorded (5.3), so the UI counters are dead.
- The system prompt still mentions capabilities from the earlier API design (8).
- `LLM_TOOLS` advertises all three tools even when a dataset is missing; the `/` redirect to `/data` prevents chats until all are present, but a direct `/chats/{id}` visit would still offer them.
- Streamed text is per worker in production (11).
- `data/*.sql` are dead files from the PostgreSQL era.
