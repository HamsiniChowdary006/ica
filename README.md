# Automated News Discovery & Summarization

A Python workflow that takes a topic, discovers recent news from public sources, fetches the articles, writes a short grounded summary for each, and exports JSON, CSV and a Markdown digest with links back to the originals. It runs from the command line and has an optional Streamlit web UI.

> **No LLM is used to find, select, search for or fetch news.** Every article URL comes from RSS feeds or public APIs queried by this script. Summaries are non-LLM (extractive) by default; an LLM is optional and only ever sees text that was already collected.

> **Note:** results come from live public sources, so they change from run to run. A real run is saved in [`sample_output/`](sample_output/).

## Overview

| Step | What it does |
|---|---|
| **Input** | A topic (`--topic` or prompt), a result limit (`--limit`) and a recency window (`--days`, default 7) |
| **Discover** | Queries Google News RSS, Bing News RSS, Hacker News (Algolia API), GDELT, the Guardian API (optional key) and 32 publisher RSS feeds |
| **Retrieve** | Fetches article pages with timeouts, retries and a robots.txt check; uses full text from APIs and feeds when they provide it |
| **Clean** | Date window, de-duplication, social and non-news filtering, relevance check, round-robin across publishers |
| **Summarize** | Extractive summary taken from the article text (optional Claude summary) |
| **Export** | `.json` and `.csv` (machine-readable) and `.md` (human-readable digest) |

Digest format (placeholders, not real news):

```
## 1. <article title>
**Source:** <publisher> · **Published:** <ISO date or unknown>
**Link:** <original article URL>

<2 to 4 sentence summary built from the article text>

_Data flags: <anything missing or noteworthy>_
```

## Quick start

Requires Python 3.10+ (tested on 3.10 and 3.13, Windows). Run from the project folder.

### Command line

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python main.py --topic "cybersecurity" --limit 8
```

A run takes about two minutes because requests are rate-limited on purpose. Output files appear in `output/`.

More examples:

```bash
python main.py --topic "AI regulation" --limit 5
python main.py --topic "electric vehicles" --limit 8 --days 7
python main.py --topic "climate policy" --out results
python main.py                          # prompts for a topic
python main.py --topic "cybersecurity" --funnel-json   # also writes funnel.json
```

| Flag | Meaning | Default |
|---|---|---|
| `--topic` | topic or keywords (prompted if omitted) | none |
| `--limit` | maximum number of articles | 5 |
| `--days` | only articles from the last N days | 7 |
| `--out` | output directory | `output` |
| `--llm` | summarize with Claude (optional, needs a key) | off |
| `--funnel-json` | write the per-source candidate funnel to `funnel.json` | off |
| `-v` | debug logging | off |

### Web UI (optional)

```bash
streamlit run app.py
```

Opens at http://localhost:8501. Enter a topic, set the article limit and recency window, and watch the pipeline stages run. Results appear as summary cards with source links, plus a sources tab, a data table and downloads for the digest, JSON and CSV. The UI calls the same `run()` function as the CLI.

If `streamlit` is not recognised on Windows, use `python -m streamlit run app.py`. If it asks for an email on first run, press Enter.

## Environment variables

All optional. The default mode needs no keys.

| Variable | Used for |
|---|---|
| `GUARDIAN_API_KEY` | Enables the Guardian Open Platform API, which returns full article text without scraping |
| `ANTHROPIC_API_KEY` | Enables `--llm` summaries (and the UI toggle) |
| `SUMMARY_MODEL` | Overrides the Claude model used by `--llm` |

Copy `.env.example` to `.env` and fill in what you need. `.env` is git-ignored, so secrets are never committed.

## Sources and collection method

- **Topic search:** Google News RSS and Bing News RSS. Google links are decoded with the unofficial `googlenewsdecoder` package.
- **APIs:** Hacker News (Algolia), GDELT DOC, and the Guardian Open Platform (with a key).
- **Publisher RSS feeds (32):** BBC (6 sections), The Guardian (6 sections), NPR (5 sections), Al Jazeera, TechCrunch, Ars Technica, The Hacker News, Wired, The Verge, MIT Technology Review, CNBC Tech, Sky News, DW, France 24, ABC Australia, BleepingComputer, Engadget, The Independent. Entries are keyword-filtered locally, and any full text included in the feed is used directly.
- **Article pages:** fetched with `requests` (timeouts, retries with backoff, a polite delay) after a robots.txt check. Text is extracted with `trafilatura`, with a BeautifulSoup fallback.
- **Responsible collection:** robots.txt is respected, paywalls, logins and CAPTCHAs are never bypassed, and the bot identifies itself with a clear User-Agent. Pages that cannot be used are counted and explained, not hidden.

### Cleaning rules

- Date window filter (undated items are kept but flagged).
- De-duplication by normalized URL and fuzzy title match.
- Relevance: all topic words must appear, in the title or repeatedly in the body. Common abbreviations are expanded (for example AI to artificial intelligence), and hyphen/space variants such as "cyber-security" match "cybersecurity".
- Social networks, PR wires and non-news pages (blogs, case studies, tool pages) are skipped.
- Publishers are interleaved round-robin so one source cannot dominate, and publisher names are normalized (section feeds count as one publisher).
- Articles with full text are preferred. Excerpt-only articles are used only to fill a gap, and are flagged.

## Summarization method

- **Default (extractive):** the 2 to 3 highest-scoring sentences are copied from the article text. Scoring uses term frequency, overlap with the title and position, with headings, boilerplate and near-duplicate sentences removed. Nothing is generated, so nothing unsupported can appear. If an article has fewer than two usable sentences, the summary is left empty and flagged instead of padded.
- **Optional (`--llm`):** Claude receives only the collected text and is told to use no outside facts. Its answer is accepted only if it has 2 to 4 sentences; otherwise the extractive summary is used. Without a key, `--llm` prints a warning and falls back to extractive.

## Output

Each run writes three files to `output/`, named `<topic>_<timestamp>`.

**JSON** (placeholders shown):

```json
{
  "topic": "<topic>",
  "generated_at": "<UTC timestamp>",
  "article_count": 8,
  "publishers": ["<publisher>"],
  "notes": [],
  "source_status": {},
  "fetch_failures": [],
  "failure_summary": {},
  "articles": [{
    "title": "<title>", "source": "<publisher>",
    "url": "https://example.com/article",
    "published_at": null,
    "retrieved_at": "<UTC timestamp>",
    "excerpt": "<first ~1,200 characters, cut at a sentence boundary>",
    "summary": "<grounded summary>",
    "summary_method": "extractive",
    "discovered_via": "<source name>",
    "flags": []
  }]
}
```

The real file also contains window and count fields (`window_days`, `requested_limit`, `within_window_count`, `undated_count`) and the per-source `funnel`.

**CSV:** `title, source, url, published_at, retrieved_at, summary, summary_method, flags`

**Markdown digest:** summaries with source links, recurring themes, a shortfall note when fewer than 5 articles qualify, and a summary of skipped pages.

Missing data is never invented. A missing publication date is `null` and flagged, and unusable pages are counted by reason:

| Skip reason | Meaning |
|---|---|
| robots.txt disallowed | The site asks bots not to fetch the page |
| HTTP 403 / blocked | The site refused the request or the page is paywalled |
| HTTP other / timeout | Network error or server failure |
| google redirect not resolved | A Google News link could not be decoded |
| headline-only | Only a headline was available, so there was nothing to summarize |
| social domain | Social network posts are not treated as news |
| non-news domain / page | Blog, PR wire, case study or tool page |

## Sample run

`python main.py --topic "cybersecurity" --limit 8` produced 8 articles from 8 distinct publishers, all with full text and all inside the 7-day window. Files and details are in [`sample_output/`](sample_output/).

## Mapping to the assignment

| Requirement | Where it is met |
|---|---|
| Topic and limit at runtime | `--topic` (or prompt) and `--limit` |
| At least two sources, no LLM for discovery | RSS feeds and public APIs only, queried by `main.py` |
| 5 or more articles from the past 7 days | `--days 7` default; shortfalls are reported with reasons, never padded |
| Metadata and honest gaps | title, source, URL, `published_at`, `retrieved_at`, excerpt, `flags` |
| 2 to 4 sentence grounded summaries | Extractive by default; LLM output validated |
| JSON/CSV plus readable digest | `output/*.json`, `.csv`, `.md` |
| Reliability | Per-source and per-article error isolation, timeouts, retries, de-duplication, relevance filter |
| Secrets in environment variables | `.env` is git-ignored; `.env.example` has placeholders |

## Repository structure

```
ica/
├── main.py                 # CLI pipeline: discover, retrieve, clean, summarize, export
├── app.py                  # Streamlit web UI (uses the same pipeline)
├── styles.css              # UI styling
├── .streamlit/config.toml  # UI theme
├── requirements.txt
├── .env.example            # optional API keys (copy to .env)
├── tests/                  # pytest suite (mocked network, no live calls)
├── sample_output/          # one real run: .json, .csv, .md and a README
├── output/                 # generated at run time (git-ignored)
├── LICENSE
└── README.md
```

## Testing

```bash
python -m pytest -q
```

Tests mock the network, so they run offline. They cover URL handling, de-duplication, relevance matching, summary grounding, quote handling, publisher normalization, failure isolation, date filtering and the UI render.

## Known limitations

- Some publishers block bots or disallow scraping in robots.txt. They are skipped by design, so yield varies by topic and by day.
- Public sources rate-limit: Google, GDELT and some feeds answer HTTP 429 or time out after repeated runs. Wait a few minutes and retry.
- Google News decoding relies on an unofficial package and can fail; Google items are the lowest priority.
- Relevance is keyword-based, so very short or ambiguous topics can let weak matches through, and aggregator sources (Hacker News, GDELT) occasionally surface non-news pages despite the filters.
- Extractive summaries copy sentences, so they can read choppily and never paraphrase.
- Niche topics may return fewer than 5 articles. The digest says so and lists the reasons.
- Summary quality was checked by reading samples, not scored against human references.

## With more time

- Embeddings-based relevance and cross-source story grouping.
- Async fetching and on-disk caching to cut run time and rate-limit pressure.
- Scheduled runs and email or Slack digests.
- A small evaluation set to score summary faithfulness.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError` | Activate the virtual environment, then `pip install -r requirements.txt` |
| PowerShell blocks `Activate.ps1` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then activate again |
| `streamlit` not recognised | Use `python -m streamlit run app.py` |
| Fewer than 5 articles | Try a broader topic, raise `--days` or `--limit`, or wait out rate limits and rerun |
| Guardian shows "GUARDIAN_API_KEY not set" | Optional: add a free key to `.env` for full-text API articles |
| `--llm` prints a warning | Set `ANTHROPIC_API_KEY` in `.env`; the run continues with extractive summaries |
| A source shows HTTP 429 or timeout | It is rate-limiting you; rerun later (other sources still work) |