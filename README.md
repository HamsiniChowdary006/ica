# Automated News Discovery & Summarization

A command-line Python workflow that takes a topic, discovers recent news from multiple sources, fetches and cleans the articles, writes a grounded summary for each, and exports JSON, CSV and a Markdown digest.

**No LLM is used for discovery or retrieval.** All URLs come from RSS feeds fetched by the script. Summarization is non-LLM (extractive) by default; an LLM is optional and used for summarization only.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Requires Python 3.9+.

Optional keys: `cp _.env.example .env` and add the keys you need. `.env` is git-ignored, so secrets are never committed.

## Tests

```bash
python -m pytest -q tests
```

## Run

```bash
python main.py --topic "cybersecurity" --limit 5
python main.py --topic "electric vehicles" --limit 8 --days 7
python main.py --topic "AI regulation" --out results
python main.py                      # prompts for a topic
```

| Flag | Meaning | Default |
|---|---|---|
| `--topic` | topic / keywords (prompted if omitted) | – |
| `--limit` | max number of articles | 5 |
| `--days` | recency window in days | 7 |
| `--out` | output directory | `output` |
| `--llm` | summarize with Claude (optional) | off |
| `-v` | debug logging | off |

### Environment variables (optional, only for `--llm`)
- `ANTHROPIC_API_KEY` – set in your shell or in a local `.env` (see `.env.example`); never committed.
- `SUMMARY_MODEL` – override the model (default `claude-haiku-5-5`).
- `GUARDIAN_API_KEY` – enables full-text Guardian Open Platform articles.
- Without `--llm`, no keys are needed.

## Web UI (optional)

```bash
streamlit run app.py
```
Opens a browser page where you enter a topic, set the article limit and recency window, and view summary cards with source links. JSON, CSV and Markdown downloads are included. It uses the same pipeline as the CLI.

## Output
Each run writes three files in `output/`, named `<topic>_<timestamp>`:
- `.json` – full structured results (title, source, url, published_at, retrieved_at, excerpt, summary, flags, plus per-source status)
- `.csv` – flat version without the full excerpt
- `.md` – human-readable digest with summaries and source links

## Note: sources, method, limitations

**Sources and collection method**
- Topic search feeds: Google News RSS and Bing News RSS (these surface articles from many publishers).
- Direct publisher RSS feeds: BBC (News, Technology, Business), The Guardian, NPR, Al Jazeera, TechCrunch, Ars Technica, The Hacker News, Wired, The Verge, CNBC Tech, Sky News, DW, France 24, ABC Australia, BleepingComputer, Engadget, and The Independent. Items are keyword-filtered locally.
- API sources: Guardian Open Platform (with `GUARDIAN_API_KEY`, full text), Hacker News Algolia, and GDELT DOC.
- Article pages are fetched with `requests` (timeouts, 2 retries with backoff, a polite delay, and a robots.txt check). Text is extracted with `trafilatura`, falling back to BeautifulSoup.
- Paywalls, logins and CAPTCHAs are not bypassed. Blocked pages (e.g. HTTP 403) are flagged and logged in `fetch_failures`. Articles with full text are preferred; excerpt-only articles are used only to fill any remaining gap.

**Cleaning**
- Date window filter (undated items are kept but flagged).
- Dedupe by normalized URL and fuzzy title similarity.
- Relevance check on title and body.
- Round-robin across publishers so results aren't dominated by one source.

**Summarization**
- Default: extractive. The 3 highest-scoring sentences (term frequency, title overlap, lead position) are taken verbatim from the article text, so nothing unsupported can be added.
- Optional `--llm`: Claude gets only the collected text with an instruction to use no outside facts; it falls back to extractive on any error.

**Known limitations**
- Some publishers block scrapers, so a few articles fall back to the feed excerpt (flagged in the output).
- Google News links need decoding to reach the original publisher; if decoding fails, the item may be skipped or use the excerpt.
- Relevance is keyword-based, so very short or ambiguous topics can let through weak matches.
- Extractive summaries can read choppily and don't paraphrase.
- Niche topics may produce fewer than 5 articles; the digest says so explicitly.

**With more time**
- Add embeddings-based relevance and story clustering across sources.
- Add async fetching, caching, unit tests (pytest with mocked feeds), scheduled runs, and a small web UI.
