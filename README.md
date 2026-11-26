# Briefly: automated news discovery and summarization

Briefly discovers recent news with Python, retrieves article text, and writes a source-linked digest. **No LLM is used to discover, search for, select, or fetch news.** Claude is an optional post-collection summarizer only.

## Setup

Python 3.10+ (tested on 3.10 and 3.13).

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

PowerShell activation:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

No API keys are needed for default mode. To use optional keys, copy `.env.example` to `.env` and set values there, or export them in your shell. `.env` is ignored by Git.

## Run

```bash
python main.py --topic "cybersecurity" --limit 5
python main.py --topic "AI regulation" --limit 8 --days 7 --out output
python main.py
streamlit run app.py
```

The CLI prompts for a topic when `--topic` is omitted. The Streamlit UI supports topic suggestions, article limit, recency window, optional Claude summaries, and downloads.

| Flag | Meaning | Default |
|---|---|---|
| `-h, --help` | Show help and exit | — |
| `--topic TOPIC` | Topic or keywords; prompt if omitted | — |
| `--limit LIMIT` | Maximum number of articles | `5` |
| `--days DAYS` | Recency window in days | `7` |
| `--out OUT` | Output directory | `output` |
| `--llm` | Use Claude for summaries only | off |
| `--funnel-json` | Write candidate diagnostics to `funnel.json` | off |
| `-v, --verbose` | Enable debug logging | off |

## Environment variables

- `ANTHROPIC_API_KEY` — optional; required only with `--llm`.
- `SUMMARY_MODEL` — optional model override for `--llm`; default `claude-haiku-5-5`.
- `GUARDIAN_API_KEY` — optional; enables Guardian Open Platform search and full-text results.

The default extractive mode requires no keys.

## Sources and collection

Topic-search RSS sources are **Google News RSS** and **Bing News RSS**. The public APIs are **Guardian Open Platform API** (requires `GUARDIAN_API_KEY`), **Hacker News Algolia API**, and **GDELT DOC API**.

Direct publisher RSS feeds (the `PUBLISHER_FEEDS` configured in `main.py`) are:

There are 32 configured publisher RSS feeds. Section feeds are grouped under their normalized publisher for publisher counts, so the BBC feeds count as **BBC News**, Guardian feeds as **The Guardian**, and NPR feeds as **NPR**. Known domains use their publisher name; other domain labels are normalized to a readable main-domain name.

**BBC News, BBC Technology, BBC Business, BBC Science and Environment, BBC Politics, BBC World; The Guardian, The Guardian Technology, The Guardian Business, The Guardian Environment, The Guardian Politics, The Guardian Science; NPR, NPR Technology, NPR Politics, NPR Science, NPR Business; Al Jazeera; TechCrunch; Ars Technica; The Hacker News; Wired; The Verge; MIT Technology Review; CNBC Tech; Sky News; DW; France 24; ABC Australia; BleepingComputer; Engadget; The Independent.**

## Output

Each run writes `<topic>_<timestamp>.json`, `.csv`, and `.md` files in the selected output directory.

The JSON contains run metadata plus `topic` and `articles`. Each article includes the required fields `title`, `source`, `url`, `published_at`, `retrieved_at`, `excerpt`, and `summary`, along with `summary_method`, `discovered_via`, and `flags`. Dates are ISO-8601; missing publication dates are `null` and flagged.

```json
{
  "topic": "cybersecurity",
  "articles": [
    {
      "title": "Example report title",
      "source": "Example publisher",
      "url": "https://example.org/report",
      "published_at": "2026-11-25T10:00:00+00:00",
      "retrieved_at": "2026-11-26T10:15:00+00:00",
      "excerpt": "A source excerpt of up to about 1,200 characters...",
      "summary": "A grounded summary sentence. A second grounded sentence."
    }
  ]
}
```

CSV columns: `title`, `source`, `url`, `published_at`, `retrieved_at`, `summary`, `summary_method`, `flags`. The `excerpt` field contains the first approximately 1,200 characters, cut at a sentence boundary when possible. The Markdown digest contains run details, shortfall notes, summaries, source links, and fetch diagnostics. `sample_output/README.md` records metadata for a historical run; generated publisher article text is not archived.

Use `--funnel-json` to write the per-source candidate funnel to `funnel.json`; the CLI also prints source health, filtering counts, and drop reasons.

## Method and design

The program queries the two news RSS search feeds, the publisher RSS feeds, and the listed APIs. Guardian API and feed content can provide full text directly. Otherwise, candidate pages are fetched with a timeout after a `robots.txt` check. Requests use bounded retries with backoff; page fetches include a polite delay. The program does not bypass paywalls, login walls, or CAPTCHAs.

Cleaning applies the requested date window (undated items are retained and flagged; timestamps up to one day in the future are tolerated), URL and fuzzy-title deduplication, keyword relevance checks, and round-robin ordering by normalized publisher. Direct-feed relevance accepts long keywords separated by a hyphen or space. Known promotional domains and paths (such as case studies, customer pages, pricing, and product pages) are excluded. Distinct-publisher counts group section feeds and recognized domain labels under their publisher name. A brief reports its within-window and undated article counts.

Extracted text preserves line breaks so headings, bylines, and captions can be separated from sentences. Default summaries are extractive: up to three source sentences are scored by term frequency, title-token overlap, and a lead-sentence boost, then retained in source order. Leading repeated headlines and boilerplate are removed, and unbalanced curly-quote summaries are rejected. Optional Claude summaries use the configured `SUMMARY_MODEL`, receive only the collected article text, and are instructed to add no outside facts. Responses outside the required two-to-four sentence range or with unbalanced curly quotes, and API failures, fall back to the extractive summary. The archived sample used **extractive** summaries.

## Known limitations

- Pages disallowed by `robots.txt`, paywalled, or otherwise blocked are skipped; feed/API text may still be available.
- If `robots.txt` cannot be reached, the crawler currently continues. Google News links are resolved with the unofficial `googlenewsdecoder` package and can fail or be rate-limited; Google items are lowest priority. Other external rate limits can also reduce results.
- Keyword relevance can admit weak matches, especially for broad or short topics.
- Extractive summaries may read choppily, and niche topics may return fewer than five articles; the shortfall is reported rather than filled with invented records.

## What I would improve

Add embeddings-based relevance, cross-publisher story clustering, caching, asynchronous fetching, and scheduled runs.

## Testing

From the repository root:

```bash
python -m pytest -q
```
