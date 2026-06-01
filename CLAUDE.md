# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Evidence Fetcher by GDELT collects news articles from the GDELT BigQuery public dataset and runs them through a local LLM to extract commodity market demand signals. All LLM outputs are translated to Japanese.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the API server (main entry point)
python api_server.py
# Starts FastAPI + Uvicorn on 0.0.0.0:8000
# FastAPI auto-docs available at http://localhost:8000/docs

# Run evidence fetcher standalone (tests GDELT fetching without LLM)
python evidence_fetcher.py

# Preprocess GDELT theme files
python theme_converter.py <filepath>
```

No test suite or linter is configured.

## Architecture

The system is a three-stage pipeline:

```
GDELT BigQuery → Web Scraping → Local LLM → JSON Response
```

**Stage 1 — GDELT Query** ([evidence_fetcher.py](evidence_fetcher.py))
- `fetch_metadata()` queries `gdelt-bq.gdeltv2.gkg_partitioned` for articles matching user keywords and a fixed list of demand-related GDELT themes (ECON_DEMAND, ENV_GREEN_TECH, etc.)
- Searches the past 7 days, returns up to 10 articles per keyword

**Stage 2 — Web Scraping** ([evidence_fetcher.py](evidence_fetcher.py))
- `enrich_with_ai()` scrapes each article URL using Trafilatura
- Extracts up to 2000 characters of article body text
- Applies 1-second delay between requests

**Stage 3 — LLM Analysis** ([src/llm_local.py](src/llm_local.py))
- `LocalLLMHandler` loads `Qwen/Qwen2.5-7B-Instruct` from Hugging Face at startup
- Auto-selects CUDA (float16) or CPU (float32)
- Processes articles in batch with a structured demand analysis system prompt
- Outputs per-article JSON: demand signal (yes/no), trend (increase/decrease/neutral), drivers, causal relationships, Japanese translations

**API Layer** ([api_server.py](api_server.py))
- `POST /api/v1/news/collect` accepts `{"keywords": [...]}` (1–5 strings)
- Model is loaded once during lifespan startup via `LocalLLMHandler`
- Returns `CollectionResponse` with summary counts and per-article `details` array

## Required Credentials

- `gcp-key.json` — GCP service account key for BigQuery access (must be present in project root)
- `.env` — Contains `GEMINI_API_KEY` and `DATABASE_URL` (PostgreSQL on Render); `GEMINI_API_KEY` is currently unused in the codebase

## Key Behavioral Notes

- `test_limit=2` is the default in `execute_collection()` — only 2 articles per keyword are sent to the LLM to control inference cost and speed
- LLM prompts explicitly prohibit hallucination: analysis must be grounded only in article text
- Articles that fail LLM processing are returned with status `"UNCHECKED"` rather than raising errors
- GDELT theme filtering targets three categories: macro demand/growth, industry-specific, and commodity markets — these are hardcoded in `evidence_fetcher.py`
