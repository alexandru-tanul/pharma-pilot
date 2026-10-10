# Pharma Pilot

AI-powered pharmaceutical research assistant. Ask questions in plain English about clinical trials, drugs, targets, and protein biology; the app translates them into SQL against three biomedical databases, runs the queries locally, and streams back an answer with tables and sources.

Live demo: https://clinical-trials-scout.alexandrutanul.com

## What it does

- Answers questions such as "Find phase 3 trials for semaglutide", "What drugs target GPER?", or "Show adverse events reported for metformin"
- Decides which database to query, or combines several in one answer
- Streams the response while it is generated, with every SQL query shown and inspectable
- Keeps chat history, lets you rate answers, and exports a conversation as a PDF report
- Works on mobile

## Data sources

| Database | Content | Tables |
|---|---|---|
| ClinicalTrials.gov (AACT) | Registered clinical studies, interventions, outcomes, results | 49 |
| DrugCentral | Approved drugs, targets, mechanisms, interactions, FAERS adverse events | 67 |
| Pharos (NIH TCRD) | Protein targets, development levels, ligands, disease associations | 92 |

The data is stored locally as Parquet files (about 4 GB) and queried in-process with DuckDB. The files are published monthly as GitHub releases of [scriptogre/biomedical-data](https://github.com/scriptogre/biomedical-data). The app downloads and updates them itself, see below.

## Built with

- FastAPI, Jinja2, HTMX, Tailwind CSS, Server-Sent Events
- DuckDB over Parquet for the biomedical data
- PostgreSQL (Tortoise ORM, Aerich migrations) for chat history
- LiteLLM, so the model is configurable: Claude, Gemini, OpenAI, DeepSeek
- WeasyPrint for PDF reports
- Docker Compose, uv

## Run it

Requirements: Docker with Compose, and [just](https://github.com/casey/just).

```bash
git clone https://github.com/alexandru-tanul/clinical-trials-scout.git
cd clinical-trials-scout
just init          # creates .env, builds the image, downloads the datasets
```

Edit `.env` and set at least `SECRET_KEY`, `POSTGRES_PASSWORD`, and the API key for the model you chose in `MODEL`. Then:

```bash
just up            # http://localhost:8000
```

If you skipped the download step, the app opens its **Data** page on first visit, where each dataset can be downloaded with a progress bar. The chat becomes available as soon as the data is installed; no restart is needed.

## Keeping the data current

The **Data** page in the sidebar shows the installed version of each dataset next to the latest monthly release and offers an **Update** button. The same works from a terminal:

```bash
just data-status   # local vs latest versions
just update-data   # update everything to the newest release
```

Outside Docker: `python -m app.data_sync status|download|update`. Downloads are verified against the published SHA-256 checksum and installed atomically, so an interrupted update never leaves a half-replaced dataset.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | yes | Session signing |
| `POSTGRES_PASSWORD` | yes | Chat history database |
| `MODEL` | no | LiteLLM model name, default `gemini/gemini-3-flash-preview` |
| `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` | one | Key for the chosen model |
| `DEBUG` | no | Template auto-reload, default `True` |
| `PARQUET_DIR` | no | Where the datasets live, default `data/parquet` |
| `GITHUB_TOKEN` | no | Raises the GitHub API rate limit for release checks |

## How it works

1. A user message is saved and a background task starts generating the answer.
2. The model receives the schema of the available views and decides which tool to call: `query_clinical_trials`, `query_drugcentral`, or `query_pharos`, each taking SQL.
3. Queries run read-only in DuckDB, results are returned to the model, and the final answer is synthesised.
4. Progress is streamed to the browser over SSE: structural changes go through PostgreSQL `LISTEN/NOTIFY`, text chunks go straight to the connected client.

## Development

```bash
just makemigrations <name>   # new Aerich migration
just migrate                 # apply migrations
just logs                    # follow container logs
```

## License

Free to use for learning and personal projects. The datasets keep the licenses of their publishers (AACT, DrugCentral, Pharos).
