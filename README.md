# Sectors IDX Filing Pipeline

Harvests IDX insider ownership announcements, parses the filings out of their PDFs, repairs what it can, and writes the result to Supabase as filings and news.

It runs unattended every two hours. The design goal is to minimize manual intervention, requiring it only when genuinely necessary. Anything the pipeline can determine on its own, it handles automatically.

## How a filing moves through it

```
ingestion   →  fetch announcements in a time window from the IDX API
downloader  →  download each attachment, classify it idx / non_idx
parser      →  extract the filing, repair it, or hand back a reason
dedup       →  drop anything already in idx_filings
generate    →  build title, body, context and highlights from 6 months of history
insert      →  push to idx_filings + idx_news
```

```bash
uv run python -m idx_pipeline.pipeline run
```

## The two parsers

IDX serves its standard ownership form as `LK-DDMMYYYY-NNNN-NN.pdf`. The downloader keys on that filename, not the announcement title — one announcement can carry both a standard form and a differently shaped lampiran, so the title cannot tell them apart.

| document | parser |
|---|---|
| `idx` — the standard layout | `parser/core.py`, regex over PyMuPDF text |
| `non_idx` — anything else | `parser/llm_parser.py`, structured extraction into `FilingPayload` |

**The regex parser is the default and the LLM is the fallback**, not the other way round. The regex parser is deterministic and free; the LLM is neither, and it will happily produce a plausible wrong number. So when `core` fails, `runner` looks at *why* before spending a call:

- *the data is on the page and we misread it* → retry with the LLM
- *a stale `company_map`, or a filing about warrants* → no retry, the LLM cannot help either

## Repairing a filing instead of emailing about it

A filing is rejected when `holding_before + net_shares != holding_after`. Often the document itself is inconsistent, and `parser/amend.py` can work out which number is wrong.

It needs two questions answered.

**Is `holding_before` real?** Everything else is computed from it, so it needs backing from outside the document — the holder's previous filing in Supabase, or failing that their position in the monthly securities report (`parser/securities_report.py`, the LBRE that issuers publish around the 10th of each month).

**Is `holding_after` wrong, or did we miss a transaction row?** These call for opposite fixes, and the share percentage is the only thing that can tell them apart — because it is the only number in the filing not derived from the transaction rows:

```
shares_outstanding = holding_before / (share_percentage_before / 100)
implied_after      = shares_outstanding * (share_percentage_after / 100)
expected_after     = holding_before + net_shares
```

If `expected_after` lands on `implied_after`, the rows are complete and `holding_after` is the liar — rewrite it. If it does not, a row went missing — add it back without a price.

The percentage carries two decimals, so on a company with 55 billion shares outstanding one tick is ~5.5 million shares. A discrepancy smaller than that is invisible to it, and `amend` will not guess: the filing goes in as filed.

## What still reaches email

Everything below genuinely needs manual review. Everything else is repaired, retried, or skipped in silence.

| | |
|---|---|
| non-common share classification | the filing is about warrants or preferred shares — not a parse error |
| share transfer | needs a manual call on UID generation |
| symbol missing from `company_map` | a new IDX listing — run the refresh workflow |
| the LLM also failed | last resort exhausted |

Alerts collect in `data_v2/alert/not_inserted.json` and go out by SES.

Parsers never alert on their own. They hand `(filings, reasons)` back and `runner` decides — a filing the LLM later recovers must not have already emailed you about itself.

## Layout

```
src_v2/idx_pipeline/
  pipeline.py              typer cli, chains every stage
  ingestion/               idx announcement api, time windows, run state
  downloader/              pdf download, idx vs non_idx classification
  parser/
    core.py                regex parser for the standard idx layout
    llm_parser.py          llm fallback for everything else
    amend.py               repairs holding mismatches
    securities_report.py   monthly LBRE, the anchor of last resort
    runner.py              routing, retry, tagging, alerting
  alerts/                  the three validity checks, email template, SES
  generate/                title, body, context, highlights, news
  llm/                     provider client with key rotation, prompts
  utils/                   dedup, insert, shared helpers
```

## Running it

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
# a window (WIB). omit both dates to resume from the last run
uv run python -m idx_pipeline.pipeline run \
  --start-date "2026-07-14 08:50" --end-date "2026-07-14 11:45"

# dry run - parse everything, write nothing
uv run python -m idx_pipeline.pipeline run --no-is-push-db --no-is-send-alert
```

Each stage writes its output to `data_v2/` as it goes (`ingestion/result.json`, `downloader/downlod_ingestion.json`, `parser/pdf_parsed.json`), so any stage can be inspected after a run.

## Configuration

`.env`:

```
PROXY
SUPABASE_URL / SUPABASE_KEY
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION
SES_FROM_EMAIL / ALERT_TO_EMAIL
GROQ_API_KEY_DEV
GEMINI_API_KEY / GEMINI_API_KEY_BACKUP
```

Models live in `MODEL_CONFIG` (`utils/constant.py`) — Groq and Gemini are wired. `llm/client.py` rotates API keys on rate limits, and gives up on request-level errors instead of burning the rest of the pool.

## Scheduled workflows

| | |
|---|---|
| `idx_filings_v2.yaml` | every 2 hours. Commits run state and the securities report cache back to the repo, so a fresh runner does not re-scrape a month of announcements to anchor a single filing. |
| `refresh_company_map.yaml` | monthly. Pulls the company list from Supabase. Dispatch it by hand when a new listing alerts. |

## Conventions

- Times are WIB (UTC+7) throughout — ingestion windows, filing timestamps, cron.
- `data_v2/state/last_run.json` carries the watermark between runs; a run with no explicit dates resumes from it.
