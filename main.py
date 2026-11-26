#!/usr/bin/env python3
"""Automated News Discovery & Summarization.

Usage:
    python main.py --topic "cybersecurity" --limit 5
"""
import argparse
import csv
import json
import logging
import math
import os
import re
import sys
import time
import urllib.robotparser
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qs, quote_plus, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import trafilatura
except ImportError:  # fallback to BeautifulSoup extraction
    trafilatura = None

try:
    from googlenewsdecoder import gnewsdecoder  # resolves Google News redirect links
except ImportError:
    gnewsdecoder = None

try:
    from dotenv import load_dotenv
    load_dotenv()  # reads ANTHROPIC_API_KEY / SUMMARY_MODEL from a local .env (never committed)
except ImportError:
    pass

log = logging.getLogger("newsdigest")

USER_AGENT = "NewsDigestBot/1.0 (educational assessment project)"
TIMEOUT = 15
REQUEST_DELAY = 0.7  # polite delay between article fetches

# Topic-search sources (the search engine does the matching)
SEARCH_SOURCES = {
    "Google News": "https://news.google.com/rss/search?q={q}+when:{days}d&hl=en-US&gl=US&ceid=US:en",
    "Bing News": "https://www.bing.com/news/search?q={q}&format=rss",
}

# Direct publisher feeds (we filter by keyword locally)
PUBLISHER_FEEDS = {
    "BBC News": "http://feeds.bbci.co.uk/news/rss.xml",
    "BBC Technology": "http://feeds.bbci.co.uk/news/technology/rss.xml",
    "BBC Business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    "BBC Science and Environment": "http://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "BBC Politics": "http://feeds.bbci.co.uk/news/politics/rss.xml",
    "BBC World": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "The Guardian Technology": "https://www.theguardian.com/uk/technology/rss",
    "The Guardian Business": "https://www.theguardian.com/uk/business/rss",
    "The Guardian Environment": "https://www.theguardian.com/uk/environment/rss",
    "The Guardian Politics": "https://www.theguardian.com/politics/rss",
    "The Guardian Science": "https://www.theguardian.com/science/rss",
    "NPR": "https://feeds.npr.org/1001/rss.xml",
    "NPR Technology": "https://feeds.npr.org/1019/rss.xml",
    "NPR Politics": "https://feeds.npr.org/1014/rss.xml",
    "NPR Science": "https://feeds.npr.org/1007/rss.xml",
    "NPR Business": "https://feeds.npr.org/1006/rss.xml",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "TechCrunch": "https://techcrunch.com/feed/",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
    "Wired": "https://www.wired.com/feed/rss",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "MIT Technology Review": "https://www.technologyreview.com/feed/",
    "CNBC Tech": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "Sky News": "https://feeds.skynews.com/feeds/rss/world.xml",
    "DW": "https://rss.dw.com/rdf/rss-en-all",
    "France 24": "https://www.france24.com/en/rss",
    "ABC Australia": "https://www.abc.net.au/news/feed/51120/rss.xml",
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "Engadget": "https://www.engadget.com/rss.xml",
    "The Independent": "https://www.independent.co.uk/news/uk/rss",
}

STOPWORDS = set("""a an the and or but if then than that this these those of to in on for with as at by from is are was
were be been being it its he she they them his her their we you i not no so such also into about over after before
more most other some any can could would should will may might has have had do does did said says say new just up out
who what which when where while their there here than very been how why all one two""".split())

BOILERPLATE = ("subscribe", "sign up", "newsletter", "cookie", "all rights reserved", "click here",
               "follow us", "read more", "advertisement", "privacy policy", "terms of use",
               "log in", "sign in", "share this", "related:", "read also",
               "listen to this article", "your privacy choices", "skip to main content")

BOILERPLATE_PREFIXES = (
    "log in", "sign in", "share this", "related:", "read also",
    "listen to this article", "skip to main content",
)

SOCIAL_DOMAINS = {
    "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com",
    "youtube.com", "youtu.be", "reddit.com", "linkedin.com",
}

ALIASES = {
    "ai": "artificial intelligence",
    "ev": "electric vehicle",
    "evs": "electric vehicles",
    "us": "United States",
    "uk": "United Kingdom",
    "eu": "European Union",
}

# --------------------------------------------------------------------------- Publisher normalization
FEED_PUBLISHER = {
    # BBC feeds -> "BBC News"
    "BBC News": "BBC News",
    "BBC Technology": "BBC News",
    "BBC Business": "BBC News",
    "BBC Science and Environment": "BBC News",
    "BBC Politics": "BBC News",
    "BBC World": "BBC News",
    # The Guardian feeds -> "The Guardian"
    "The Guardian": "The Guardian",
    "The Guardian Technology": "The Guardian",
    "The Guardian Business": "The Guardian",
    "The Guardian Environment": "The Guardian",
    "The Guardian Politics": "The Guardian",
    "The Guardian Science": "The Guardian",
    # NPR feeds -> "NPR"
    "NPR": "NPR",
    "NPR Technology": "NPR",
    "NPR Politics": "NPR",
    "NPR Science": "NPR",
    "NPR Business": "NPR",
    # Direct feeds that map to themselves
    "Al Jazeera": "Al Jazeera",
    "TechCrunch": "TechCrunch",
    "Ars Technica": "Ars Technica",
    "The Hacker News": "The Hacker News",
    "Wired": "Wired",
    "The Verge": "The Verge",
    "MIT Technology Review": "MIT Technology Review",
    "CNBC Tech": "CNBC Tech",
    "Sky News": "Sky News",
    "DW": "DW",
    "France 24": "France 24",
    "ABC Australia": "ABC Australia",
    "BleepingComputer": "BleepingComputer",
    "Engadget": "Engadget",
    "The Independent": "The Independent",
}

NON_NEWS_DOMAINS = {
    "github.com",
    "gitlab.com",
    "medium.com",
    "substack.com",
    "wordpress.com",
    "blogspot.com",
    "stackoverflow.com",
    "quora.com",
    "arxiv.org",
    "news.ycombinator.com",
    "docs.google.com",
    "notion.site",
    "prnewswire.com",
    "businesswire.com",
    "globenewswire.com",
}

NON_NEWS_PATH_PATTERNS = (
    r"/case-stud(?:y|ies)(?:/|$)",
    r"/customers?(?:/|$)",
    r"/whitepapers?(?:/|$)",
    r"/webinars?(?:/|$)",
    r"/pricing(?:/|$)",
    r"/careers?(?:/|$)",
    r"/solutions?(?:/|$)",
    r"/products?(?:/|$)",
)

FAILURE_CATEGORIES = (
    "robots.txt disallowed",
    "HTTP 403/blocked",
    "HTTP other / timeout",
    "google redirect not resolved",
    "headline-only",
    "social domain",
    "non-news domain",
)

PUBLISHER_BY_DOMAIN = {
    "cbsnews.com": "CBS News",
    "bbc.co.uk": "BBC News",
    "bbc.com": "BBC News",
    "theguardian.com": "The Guardian",
    "theverge.com": "The Verge",
    "techcrunch.com": "TechCrunch",
    "arstechnica.com": "Ars Technica",
    "wired.com": "Wired",
    "npr.org": "NPR",
    "aljazeera.com": "Al Jazeera",
    "bleepingcomputer.com": "BleepingComputer",
    "cnbc.com": "CNBC",
    "independent.co.uk": "The Independent",
    "techradar.com": "TechRadar",
    "nextgov.com": "Nextgov",
    "nytimes.com": "The New York Times",
    "reuters.com": "Reuters",
    "theregister.com": "The Register",
    "zdnet.com": "ZDNET",
    "securityweek.com": "SecurityWeek",
    "darkreading.com": "Dark Reading",
    "therecord.media": "The Record",
}


# --------------------------------------------------------------------------- HTTP
def make_session():
    """Create an HTTP session with a user agent and retry policy."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
    retry = Retry(total=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


_robots_cache = {}


def allowed_by_robots(session, url):
    """Respect robots.txt. If robots.txt is unreachable, allow (standard behaviour)."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = session.get(base + "/robots.txt", timeout=8)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots_cache[base] = rp
    return _robots_cache[base].can_fetch(USER_AGENT, url)


# --------------------------------------------------------------------------- Utilities
def tokens(text):
    """Return normalized content tokens with common stopwords removed."""
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS]


def topic_tokens(text):
    """Topic-matching tokens: include short relevant terms such as ai, eu, uk, and ev."""
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 2 and w not in STOPWORDS]


def expand_topic_query(topic):
    """Expand common abbreviations in a search query."""
    parts = re.findall(r"[a-z0-9]+(?:['-][a-z0-9]+)*", topic.lower())
    expanded = []
    i = 0
    while i < len(parts):
        token = parts[i]
        if token in ALIASES:
            phrase = ALIASES[token]
            expanded.append(f'"{phrase}"' if " " in phrase else phrase)
        elif len(token) >= 2:
            expanded.append(token)
        i += 1
    return " ".join(expanded)


def word_boundary_contains(text, token):
    """Check that a token occurs as a whole word, allowing long terms to split."""
    if len(token) >= 8:
        token_pattern = r"[-\s]?".join(re.escape(character) for character in token)
        pattern = r"(?<![a-z0-9])" + token_pattern + r"(?![a-z0-9])"
        return re.search(pattern, text.lower()) is not None
    if len(token) == 2:
        return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", text.lower()) is not None
    return token in tokens(text)


def entry_date(entry):
    """Return a feed entry's publication or update timestamp in UTC."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def _preserve_text_lines(text):
    """Collapse whitespace within lines while preserving paragraph boundaries."""
    return "\n".join(
        re.sub(r"[^\S\n]+", " ", line).strip()
        for line in (text or "").splitlines()
    ).strip()


def clean_html(s):
    """Convert HTML to plain text without flattening line breaks."""
    return _preserve_text_lines(BeautifulSoup(s or "", "html.parser").get_text("\n", strip=True))


def useful_snippet(title, snippet):
    """Remove headline and publisher-only text from an RSS snippet."""
    text = clean_html(snippet)
    title_text = clean_html(title)
    if not text:
        return ""

    if title_text:
        text = re.sub(re.escape(title_text), "", text, flags=re.IGNORECASE)

    known_publishers = set(PUBLISHER_FEEDS)
    known_publishers.update(SEARCH_SOURCES)
    known_publishers.update({"BBC", "Reuters", "Associated Press", "AP", "CNBC", "CNN"})
    for publisher in sorted(known_publishers, key=len, reverse=True):
        text = re.sub(r"(?i)(?:^|[\s\-|:]+)" + re.escape(publisher) + r"(?:$|[\s\-|:]+)", " ", text)

    text = re.sub(r"\s*[-|:]\s*$", "", text)
    text = re.sub(r"^[-|:\s]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n-|:")
    if title_text and len(text.split()) <= 3 and len(text) < 40:
        return ""
    return text


def host_from_url(url):
    """Return a lowercase hostname without the common www prefix."""
    if isinstance(url, bytes):
        url = url.decode("utf-8", errors="ignore")
    host = urlparse(url).hostname or ""
    return host.lower().removeprefix("www.")


def is_social_url(url):
    """Return whether a URL belongs to a known social network."""
    host = host_from_url(url)
    return any(host == domain or host.endswith("." + domain) for domain in SOCIAL_DOMAINS)


def normalize_url_value(url):
    """Normalize URL values to stripped text, preserving missing values."""
    if isinstance(url, bytes):
        url = url.decode("utf-8", errors="ignore")
    if url is None:
        return None
    if not isinstance(url, str):
        return str(url).strip()
    return url.strip()


def candidate_url(candidate):
    """Get a candidate URL from its normalized or feed-entry fields."""
    url = normalize_url_value(candidate.get("url"))
    if url:
        return url
    entry = candidate.get("_entry") or {}
    return normalize_url_value(entry.get("link") or entry.get("url"))


def validate_url(url):
    """Return a validated HTTP(S) URL or a concise validation reason."""
    url = normalize_url_value(url)
    if not url:
        return None, "missing url"
    if not url.startswith("http://") and not url.startswith("https://"):
        return None, "invalid URL"
    return url, None


def _safe_error_message(error):
    """Redact configured credentials and API-key query parameters from errors."""
    message = str(error)
    for variable in ("ANTHROPIC_API_KEY", "GUARDIAN_API_KEY"):
        secret = os.environ.get(variable)
        if secret:
            message = message.replace(secret, "[redacted]")
    return re.sub(r"(?i)(api[-_]?key=)[^&\s]+", r"\1[redacted]", message)


def normalize_publisher(name):
    """Normalize known publishers and turn unknown domain names into readable labels."""
    if not isinstance(name, str):
        return name
    name = name.strip()
    domain = name.lower().rstrip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    if not re.fullmatch(r"[a-z0-9.-]+", domain) or "." not in domain:
        return name

    labels = domain.split(".")
    country_domain_prefixes = {"co", "com", "org", "net", "gov", "edu", "ac", "mil"}
    if (len(labels) >= 3 and len(labels[-1]) == 2
            and labels[-2] in country_domain_prefixes):
        main_domain = ".".join(labels[-3:])
        publisher_label = labels[-3]
    else:
        main_domain = ".".join(labels[-2:])
        publisher_label = labels[-2]

    return PUBLISHER_BY_DOMAIN.get(main_domain, publisher_label.capitalize())


def norm_url(url):
    """Normalize URL host and path for deduplication."""
    if isinstance(url, bytes):
        url = url.decode("utf-8", errors="ignore")
    if not url:
        return ""
    p = urlparse(url)
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


def norm_title(t):
    """Normalize a title into its deduplication token sequence."""
    return " ".join(tokens(t))


def is_duplicate(title, url, seen_urls, seen_titles):
    """Return whether a candidate duplicates a previously seen URL or title."""
    if norm_url(url) in seen_urls:
        return True
    nt = norm_title(title)
    return any(SequenceMatcher(None, nt, o).ratio() > 0.85 for o in seen_titles)


def _expand_topic_phrase(topic):
    """Expand a topic phrase using ALIASES, returning the alias or the original."""
    parts = re.findall(r"[a-z0-9]+(?:['-][a-z0-9]+)*", topic.lower())
    expanded = []
    for token in parts:
        expanded.append(ALIASES.get(token, token))
    return " ".join(expanded)


def _singularize_token(token):
    """Normalize common English plural endings for publisher relevance checks."""
    if token.endswith("ies") and len(token) > 3:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith(("ss", "us", "is", "news")):
        return token[:-1]
    return token


def _phrase_tokens(text):
    """Return phrase tokens normalized for simple singular/plural variants."""
    return [_singularize_token(token) for token in re.findall(r"[a-z0-9]+", text.lower())]


def _contains_token_phrase(text_tokens, phrase_tokens):
    """Return whether a normalized token phrase occurs contiguously in a token list."""
    width = len(phrase_tokens)
    return bool(width) and any(
        text_tokens[index:index + width] == phrase_tokens
        for index in range(len(text_tokens) - width + 1)
    )


def is_relevant(topic, title, body):
    """Check whether an article is relevant to the topic.

    Accept if (a) all topic tokens are in the title, OR
              (b) the exact topic phrase or its alias-expanded phrase occurs in the title or body, OR
              (c) all tokens occur in the body as whole words and their total occurrences are >= 3.
    Matches simple plural/singular variants (vehicle/vehicles, regulation/regulations).
    """
    tt = topic_tokens(topic)
    if not tt:
        return True

    topic_words = [_singularize_token(token) for token in tt]
    title_words = _phrase_tokens(title)
    body_words = _phrase_tokens(body)

    # (a) all topic tokens are in the title
    if all(word_boundary_contains(title, token) for token in tt):
        return True

    # (b) the exact topic phrase or its alias-expanded phrase occurs in the title or body
    phrases = {topic.strip().lower(), _expand_topic_phrase(topic).lower()}
    for phrase in phrases:
        phrase_words = _phrase_tokens(phrase)
        if (_contains_token_phrase(title_words, phrase_words)
                or _contains_token_phrase(body_words, phrase_words)
                or (
                    len(topic_words) == 1
                    and word_boundary_contains(title + " " + body, topic_words[0])
                )):
            return True

    # (c) all tokens occur in the body as whole words and their total occurrences are >= 3
    occurrences = Counter(body_words)
    token_counts = [occurrences[token] for token in topic_words]
    return all(count > 0 for count in token_counts) and sum(token_counts) >= 3


def excerpt_text(body, limit=1200):
    """Cut text at the last sentence end before the limit, append '...' when truncated."""
    if not body:
        return ""
    if len(body) <= limit:
        return body
    truncated = body[:limit]
    last_end = -1
    for match in re.finditer(r'[.!?]["\']?\s', truncated):
        last_end = match.end()
    if last_end > 0:
        result = truncated[:last_end].strip()
    else:
        result = truncated.rstrip()
    return result + "…"


# --------------------------------------------------------------------------- Feed helpers
def fetch_feed(session, name, url):
    """Fetch and parse one RSS or Atom feed."""
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return feedparser.parse(r.content)


def feed_entry_content(entry):
    """Extract plain-text content blocks attached to a feed entry."""
    pieces = []
    for item in entry.get("content", []) or []:
        value = item.get("value", "") if isinstance(item, dict) else getattr(item, "value", "")
        if value:
            pieces.append(clean_html(value))
    if not pieces:
        pieces.append(clean_html(entry.get("content_encoded", "")))
    return "\n".join(piece for piece in pieces if piece).strip()


# --------------------------------------------------------------------------- Guardian API
def parse_guardian_payload(payload):
    """Convert a Guardian API response into normalized article candidates."""
    candidates = []
    for item in payload.get("response", {}).get("results", []):
        fields = item.get("fields") or {}
        body = clean_html(fields.get("bodyText") or "")
        if not item.get("webUrl") or not item.get("webTitle"):
            continue
        published = item.get("webPublicationDate")
        try:
            published_at = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
            if published_at and published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published_at = None
        candidates.append({
            "title": clean_html(item["webTitle"]),
            "source": "The Guardian",
            "feed": "Guardian Open Platform",  # avoid collision with the "The Guardian" RSS feed
            "url": item["webUrl"],
            "_entry": {},
            "published_at": published_at,
            "snippet": clean_html(fields.get("trailText") or ""),
            "_full_text": body,
        })
    return candidates


def discover_guardian(session, topic, days):
    """Search the Guardian API and return candidates or a source error."""
    api_key = os.environ.get("GUARDIAN_API_KEY")
    if not api_key:
        return [], "GUARDIAN_API_KEY not set"

    from_date = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    try:
        response = session.get(
            "https://content.guardianapis.com/search",
            params={
                "q": topic,
                "from-date": from_date,
                "order-by": "newest",
                "page-size": 30,
                "show-fields": "bodyText,trailText",
                "api-key": api_key,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        return parse_guardian_payload(payload), None
    except (requests.RequestException, ValueError) as exc:
        return [], _safe_error_message(exc)[:150]


# --------------------------------------------------------------------------- GDELT
def parse_gdelt_payload(payload):
    """Convert a GDELT JSON response into normalized article candidates."""
    candidates = []
    if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
        raise ValueError("GDELT returned invalid JSON")
    for item in payload["articles"]:
        url = item.get("url", "")
        title = clean_html(item.get("title", ""))
        if not url or not title:
            continue
        seendate = item.get("seendate", "")
        try:
            published_at = datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published_at = None
        candidates.append({
            "title": title,
            "source": normalize_publisher(item.get("domain") or host_from_url(url) or "GDELT"),
            "feed": "GDELT",
            "url": url,
            "_entry": {},
            "published_at": published_at,
            "snippet": "",
        })
    return candidates


def discover_gdelt(session, topic, days):
    """Search GDELT with a bounded retry for transient failures."""
    params = {
        "query": f"{expand_topic_query(topic)} sourcelang:english",
        "mode": "artlist",
        "format": "json",
        "maxrecords": 50,
        "timespan": f"{days}d",
    }
    last_status = None
    last_body = ""
    for attempt in range(2):
        try:
            response = session.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params=params,
                timeout=30,
            )
            last_status = getattr(response, "status_code", None)
            last_body = getattr(response, "text", "")[:120]
            response.raise_for_status()
            payload = response.json()
            return parse_gdelt_payload(payload), None
        except (requests.RequestException, ValueError) as exc:
            if attempt == 0:
                time.sleep(6)
                continue
            status_text = f"HTTP {last_status}" if last_status is not None else "HTTP unknown"
            body_text = re.sub(r"\s+", " ", last_body).strip()
            if body_text:
                return [], f"{status_text}: {body_text[:120]}"
            return [], f"{status_text}: {_safe_error_message(exc)[:120]}"


# --------------------------------------------------------------------------- Hacker News
def discover_hacker_news(session, topic, days):
    """Search Hacker News Algolia for recent story candidates."""
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    try:
        response = session.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "query": topic,
                "tags": "story",
                "numericFilters": f"created_at_i>{cutoff}",
                "hitsPerPage": 30,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        candidates = []
        for hit in payload.get("hits", []):
            url = hit.get("url", "")
            title = clean_html(hit.get("title", ""))
            if not url or not title:
                continue
            created = hit.get("created_at_i")
            try:
                published_at = datetime.fromtimestamp(created, tz=timezone.utc) if created else None
            except (TypeError, ValueError, OverflowError):
                published_at = None
            candidates.append({
                "title": title,
                "source": normalize_publisher(host_from_url(url) or "Hacker News"),
                "feed": "Hacker News",
                "url": url,
                "_entry": {},
                "published_at": published_at,
                "snippet": clean_html(hit.get("story_text") or ""),
            })
        return candidates, None
    except (requests.RequestException, ValueError) as exc:
        return [], _safe_error_message(exc)[:150]


# --------------------------------------------------------------------------- Link resolution
def resolve_link(source_name, entry):
    """Resolve a known feed entry's canonical link when available."""
    link = entry.get("link", "")
    if not link:
        link = entry.get("url", "")
    if source_name == "Bing News":
        real = parse_qs(urlparse(link).query).get("url")
        if real:
            return real[0]
        real = parse_qs(urlparse(link).query).get("u")
        if real:
            return real[0]
    return link


def decode_google_url(link, state, decoder=None):
    """Decode one Google link lazily, with retries and a decode budget."""
    decoder = decoder or gnewsdecoder
    if state["stopped"] or state["budget"] <= 0 or not decoder:
        state["stopped"] = True
        return None

    if state["last_decode"] is not None:
        elapsed = time.time() - state["last_decode"]
        if elapsed < 1.2:
            time.sleep(1.2 - elapsed)

    state["budget"] -= 1
    state["last_decode"] = time.time()

    def attempt():
        try:
            result = decoder(link, interval=0.5)
            return result.get("decoded_url") if result and result.get("status") else None
        except Exception as exc:  # noqa: BLE001
            log.debug("google decode failed: %s", exc)
            return None

    decoded_url = attempt()
    if decoded_url:
        state["consecutive_failures"] = 0
        return decoded_url

    time.sleep(3)
    decoded_url = attempt()
    if decoded_url:
        state["consecutive_failures"] = 0
        return decoded_url

    state["consecutive_failures"] += 1
    if state["consecutive_failures"] >= 6:
        time.sleep(15)
        decoded_url = attempt()
        if decoded_url:
            state["consecutive_failures"] = 0
            return decoded_url
        state["stopped"] = True
    return None


def resolve_bing_link(session, link):
    """Resolve a Bing redirect URL without fetching the final publisher page."""
    if isinstance(link, bytes):
        link = link.decode("utf-8", errors="ignore")
    extracted = parse_qs(urlparse(link).query).get("url") or parse_qs(urlparse(link).query).get("u")
    if extracted:
        return extracted[0] if extracted[0] else link, bool(extracted[0])
    if host_from_url(link) != "bing.com":
        return link, True
    if not allowed_by_robots(session, link):
        return link, False
    try:
        response = session.get(link, timeout=TIMEOUT, allow_redirects=True, stream=True)
        try:
            response.raise_for_status()
            final_url = getattr(response, "url", None) or link
            return final_url, host_from_url(final_url) != "bing.com"
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
    except requests.RequestException:
        return link, False


def _discover_source_safely(name, discoverer, *args):
    """Keep an unexpected failure in one discovery API isolated to that source."""
    try:
        return discoverer(*args)
    except Exception as exc:  # noqa: BLE001
        message = _safe_error_message(exc)[:150]
        log.warning("Source '%s' unavailable: %s", name, message)
        return [], message


# --------------------------------------------------------------------------- Discovery
def discover(session, topic, days, status):
    """Return candidate article dicts from all sources. Per-source failures are recorded, not fatal."""
    cands = []
    q = quote_plus(expand_topic_query(topic))
    required_topic_tokens = topic_tokens(topic)

    guardian_cands, guardian_error = _discover_source_safely(
        "Guardian Open Platform", discover_guardian, session, topic, days
    )
    if guardian_error:
        status["Guardian Open Platform"] = {"ok": False, "candidates": 0, "kept": 0, "error": guardian_error}
        if guardian_error != "GUARDIAN_API_KEY not set":
            log.warning("Source 'Guardian Open Platform' unavailable: %s", guardian_error)
    else:
        status["Guardian Open Platform"] = {"ok": True, "candidates": len(guardian_cands), "kept": len(guardian_cands)}
        cands.extend(guardian_cands)

    hn_cands, hn_error = _discover_source_safely(
        "Hacker News", discover_hacker_news, session, topic, days
    )
    if hn_error:
        status["Hacker News"] = {"ok": False, "candidates": 0, "kept": 0, "error": hn_error}
        log.warning("Source 'Hacker News' unavailable: %s", hn_error)
    else:
        status["Hacker News"] = {"ok": True, "candidates": len(hn_cands), "kept": len(hn_cands)}
        cands.extend(hn_cands)

    gdelt_cands, gdelt_error = _discover_source_safely(
        "GDELT", discover_gdelt, session, topic, days
    )
    if gdelt_error:
        status["GDELT"] = {"ok": False, "candidates": 0, "kept": 0, "error": gdelt_error}
        log.warning("Source 'GDELT' unavailable: %s", gdelt_error)
    else:
        status["GDELT"] = {"ok": True, "candidates": len(gdelt_cands), "kept": len(gdelt_cands)}
        cands.extend(gdelt_cands)

    for name, tmpl in SEARCH_SOURCES.items():
        try:
            feed = fetch_feed(session, name, tmpl.format(q=q, days=days))
            n = 0
            for e in feed.entries:
                title = clean_html(e.get("title", ""))
                publisher = (e.get("source", {}) or {}).get("title") or name
                if name == "Google News" and " - " in title:  # strip " - Publisher" suffix
                    title = title.rsplit(" - ", 1)[0]
                url = e.get("link", "")
                feed_text = feed_entry_content(e)
                if not url:
                    continue
                if is_social_url(url):
                    log.info("%s: skipped social candidate", host_from_url(url) or "unknown")
                    continue
                normalized_source = normalize_publisher(publisher)
                candidate = {"title": title, "source": normalized_source, "feed": name,
                              "url": url, "_entry": e,
                              "published_at": entry_date(e),
                              "snippet": clean_html(e.get("summary", ""))}
                if len(feed_text) >= 500:
                    candidate["_full_text"] = feed_text
                cands.append(candidate)
                n += 1
            status[name] = {"ok": True, "candidates": len(feed.entries), "kept": n}
        except Exception as ex:  # noqa: BLE001
            error = _safe_error_message(ex)[:150]
            status[name] = {"ok": False, "candidates": 0, "kept": 0, "error": error}
            log.warning("Source '%s' unavailable: %s", name, error)

    for name, url in PUBLISHER_FEEDS.items():
        publisher = FEED_PUBLISHER.get(name, name)
        try:
            feed = fetch_feed(session, name, url)
            if not getattr(feed, "entries", None):
                status[name] = {"ok": False, "candidates": 0, "kept": 0, "error": "no parseable RSS entries"}
                log.warning("Source '%s' unavailable: %s (%s)", name, "no parseable RSS entries", url)
                continue
            n = 0
            for e in feed.entries:
                title = clean_html(e.get("title", ""))
                snippet = clean_html(e.get("summary", ""))
                feed_text = feed_entry_content(e)
                combined_text = title + " " + snippet + " " + feed_text
                if is_relevant(topic, title, combined_text) or all(
                    word_boundary_contains(combined_text, t) for t in required_topic_tokens
                ):
                    link = e.get("link", "")
                    if not link:
                        continue
                    if is_social_url(link):
                        log.info("%s: skipped social candidate", host_from_url(link) or "unknown")
                        continue
                    candidate = {"title": title, "source": publisher, "feed": name,
                                  "url": link, "_entry": e,
                                  "published_at": entry_date(e), "snippet": snippet}
                    if len(feed_text) >= 500:
                        candidate["_full_text"] = feed_text
                    cands.append(candidate)
                    n += 1
            status[name] = {"ok": True, "candidates": len(feed.entries), "kept": n}
        except Exception as ex:  # noqa: BLE001
            error = _safe_error_message(ex)[:150]
            status[name] = {"ok": False, "candidates": 0, "kept": 0, "error": error}
            log.warning("Source '%s' unavailable: %s (%s)", name, error, url)

    # Filter social URLs and non-news domains, tracking non-news drops.
    filtered = []
    for candidate in cands:
        if is_social_url(candidate.get("url", "")):
            log.info("%s: skipped social candidate",
                     host_from_url(candidate.get("url", "")) or "unknown")
            continue
        host = host_from_url(candidate.get("url", ""))
        exclusion_reason = non_news_reason(candidate.get("url", ""))
        if exclusion_reason:
            log.info("%s: skipped %s", host or "unknown", exclusion_reason)
            source_name = candidate.get("feed") or "unknown"
            source_status = status.setdefault(source_name, {})
            source_status["non_news_dropped"] = source_status.get("non_news_dropped", 0) + 1
            source_status.setdefault("non_news_failures", []).append({
                "source": candidate.get("source") or host,
                "url": candidate.get("url", ""),
                "error": exclusion_reason,
            })
            continue
        filtered.append(candidate)
    return filtered


def is_non_news_url(url):
    """Return whether a URL targets a known non-news domain or promotional page."""
    return non_news_reason(url) is not None


def non_news_reason(url):
    """Return the matching non-news exclusion reason, if any."""
    host = host_from_url(url)
    if any(host == domain or host.endswith("." + domain) for domain in NON_NEWS_DOMAINS):
        return "non-news domain"
    path = urlparse(url or "").path.lower()
    if any(re.search(pattern, path) for pattern in NON_NEWS_PATH_PATTERNS):
        return "non-news page"
    return None


# --------------------------------------------------------------------------- Ordering
def round_robin(cands):
    """Newest-first within each publisher, then interleave publishers for source diversity."""
    groups = {}
    for c in sorted(cands, key=lambda c: c["published_at"] or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True):
        groups.setdefault(c["source"], []).append(c)

    def source_priority(source):
        feeds = {c["feed"] for c in cands if c["source"] == source}
        if any(c.get("_full_text") for c in cands if c["source"] == source):
            return 0
        if any(feed in PUBLISHER_FEEDS for feed in feeds):
            return 1
        if "Bing News" in feeds:
            return 2
        if "Hacker News" in feeds:
            return 3
        if "GDELT" in feeds:
            return 4
        if "Google News" in feeds:
            return 5
        return 6

    ordered_groups = sorted(groups.items(), key=lambda item: (source_priority(item[0]), item[0]))
    out, lists = [], [values for _, values in ordered_groups]
    while any(lists):
        for g in lists:
            if g:
                out.append(g.pop(0))
    return out


# --------------------------------------------------------------------------- Extraction
def extract_text(html):
    """Extract article prose with trafilatura and a BeautifulSoup fallback."""
    if trafilatura:
        txt = trafilatura.extract(html, include_comments=False, include_tables=False)
        if txt:
            return _preserve_text_lines(txt)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    paras = [p.get_text("\n", strip=True) for p in soup.find_all("p")]
    return _preserve_text_lines("\n".join(p for p in paras if len(p) > 60))


def fetch_article(session, url):
    """Returns (text, final_url, error)."""
    if isinstance(url, bytes):
        url = url.decode("utf-8", errors="ignore")
    if not url:
        return None, "", "invalid url"
    if not url.startswith("http"):
        return None, url, "invalid url"
    if not allowed_by_robots(session, url):
        return None, url, "disallowed by robots.txt"
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code in (401, 402, 403, 451):
            return None, r.url, f"blocked or paywalled (HTTP {r.status_code})"
        r.raise_for_status()
        return extract_text(r.text), r.url, None
    except Exception as e:  # noqa: BLE001
        return None, url, f"request failed: {_safe_error_message(e)[:100]}"


# --------------------------------------------------------------------------- Summarization
def _sentence_fragments(text):
    """Split summary text into sentence-like fragments without filtering."""
    parts = []
    boundary = re.compile(r'(?:(?<=[.!?]["”’])|(?<=[.!?]))\s+(?=[A-Z0-9"“‘])')
    for line in _preserve_text_lines(text).splitlines():
        parts.extend(boundary.split(re.sub(r"\s+", " ", line).strip()))
    return [sentence.strip() for sentence in parts if sentence.strip()]


def _strip_repeated_title(text, title):
    """Remove a leading title occurrence regardless of its punctuation."""
    title_tokens = re.findall(r"[a-z0-9]+", (title or "").lower())
    if not title_tokens:
        return text.strip()
    title_pattern = r"[\W_]*".join(re.escape(token) for token in title_tokens)
    pattern = r"^\s*" + title_pattern + r"(?=$|[\W_])"
    return re.sub(pattern, "", text.strip(), count=1, flags=re.IGNORECASE).lstrip(" \t:—-")


def _strip_boilerplate_prefix(text):
    """Remove a recognized leading navigation or promotion label."""
    for prefix in BOILERPLATE_PREFIXES:
        pattern = r"^\s*" + re.escape(prefix) + r"(?:\s*[:—-]\s*|\s+|$)"
        text = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)
    return text.strip()


def sentences(text, title=None):
    """Return usable article sentences for extractive summarization."""
    usable = []
    for line in _preserve_text_lines(text).splitlines():
        line = _strip_repeated_title(_strip_boilerplate_prefix(line), title)
        if not line:
            continue
        if len(line) < 80 and line[-1] not in '.!?"”’':
            continue
        if any(phrase in line.lower() for phrase in BOILERPLATE):
            continue
        for sentence in _sentence_fragments(line):
            sentence = _strip_repeated_title(_strip_boilerplate_prefix(sentence), title)
            if (40 <= len(sentence) <= 400
                    and not any(phrase in sentence.lower() for phrase in BOILERPLATE)):
                usable.append(sentence)
    return usable


def _quotes_balanced(text):
    """Return whether curly double quotes are paired."""
    return text.count("“") == text.count("”")


def extractive_summary(title, text, n=3):
    """Pick the n most informative sentences, verbatim from the article (no invented facts)."""
    sents = sentences(text, title)
    if len(sents) < 2:
        return None
    freq = Counter(tokens(" ".join(sents)))
    title_tokens = set(tokens(title))
    scored = []
    for i, s in enumerate(sents):
        toks = tokens(s)
        if not toks:
            continue
        score = sum(freq[t] for t in toks) / math.sqrt(len(toks))
        score += 2 * len(title_tokens & set(toks))
        score *= 1.3 if i < 3 else 1.0  # lead sentences usually carry the key facts
        scored.append((score, i, s))
    ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
    selected = ranked[:n]

    def summary_for(items):
        return " ".join(item[2] for item in sorted(items, key=lambda item: item[1]))

    summary = summary_for(selected)
    if not _quotes_balanced(summary):
        balanced_ranked = [
            item for item in ranked if _quotes_balanced(item[2])
        ]
        balanced_selection = balanced_ranked[:n]
        if len(balanced_selection) >= 2:
            balanced_summary = summary_for(balanced_selection)
            if _quotes_balanced(balanced_summary):
                return balanced_summary

        replacements = ranked[n:]
        replaced = False
        for index in sorted(range(len(selected)), key=lambda i: selected[i][0]):
            for candidate in replacements:
                trial = selected[:index] + [candidate] + selected[index + 1:]
                if _quotes_balanced(summary_for(trial)):
                    selected = trial
                    replaced = True
                    break
            if replaced:
                break
        if not replaced:
            for item in sorted(selected, key=lambda item: item[0]):
                selected.remove(item)
                if _quotes_balanced(summary_for(selected)):
                    break

    summary = summary_for(selected)
    return summary if len(selected) >= 2 and _quotes_balanced(summary) else None


def llm_summary(title, text):
    """Optional: summarize ONLY the collected text with Claude. Falls back to extractive on any error."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = ("Summarize the news article below in 2-4 sentences. Use ONLY facts stated in the text. "
              "Do not add outside knowledge.\n\nTitle: " + title + "\n\nText:\n" + text[:8000])
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=40,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5"),
                                "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception as e:  # noqa: BLE001
        log.warning("LLM summary failed, using extractive: %s", e)
        return None


def _validate_llm_summary(summary):
    """Accept only grounded LLM output containing two to four sentences."""
    if not isinstance(summary, str):
        return None
    fragments = _sentence_fragments(summary or "")
    if not 2 <= len(fragments) <= 4 or not _quotes_balanced(" ".join(fragments)):
        return None
    return " ".join(fragments)


# --------------------------------------------------------------------------- Failure classification
def classify_failure(error):
    """Map a fetch error to one of the user-facing failure categories."""
    error_lower = (error or "").lower()
    if "robots.txt" in error_lower or "disallowed" in error_lower:
        return "robots.txt disallowed"
    if "google redirect" in error_lower:
        return "google redirect not resolved"
    if "only headline" in error_lower:
        return "headline-only"
    if "social domain" in error_lower:
        return "social domain"
    if "non-news domain" in error_lower or "non-news page" in error_lower:
        return "non-news domain"
    if re.search(r"http\s*(401|402|403|451)", error_lower) or "blocked or paywalled" in error_lower:
        return "HTTP 403/blocked"
    return "HTTP other / timeout"


def build_failure_summary(fetch_failures, google_unresolved_count=0):
    """Aggregate failed article fetches by category and affected domain."""
    grouped = {category: [] for category in FAILURE_CATEGORIES}
    for failure in fetch_failures:
        category = classify_failure(failure.get("error", ""))
        grouped[category].append(failure)

    summary = {}
    for category in FAILURE_CATEGORIES:
        failures = grouped[category]
        domains = Counter(
            host_from_url(failure.get("url", "")) or "unknown"
            for failure in failures
        )
        count = len(failures)
        if category == "google redirect not resolved":
            count += google_unresolved_count
            if google_unresolved_count:
                domains["news.google.com"] += google_unresolved_count
        summary[category] = {
            "count": count,
            "top_domains": [
                {"domain": domain, "count": count}
                for domain, count in domains.most_common(5)
            ],
        }
    return summary


def unusable_page_count(fetch_failures):
    """Count failed candidate pages once per normalized URL."""
    return len({
        norm_url(failure.get("url", ""))
        for failure in fetch_failures
        if failure.get("url") and norm_url(failure.get("url", ""))
    })


def has_feed_excerpt(articles):
    """Return whether any accepted article used a feed excerpt instead of page text."""
    return any(
        any("excerpt only" in flag.lower() for flag in article.get("flags", []))
        for article in articles
    )


def unusable_page_message(fetch_failures, articles):
    """Format the shared CLI and digest candidate-page diagnostic."""
    count = unusable_page_count(fetch_failures)
    message = (
        f"{count} candidate pages could not be used "
        "(blocked, robots.txt-disallowed, paywalled or headline-only)."
    )
    if has_feed_excerpt(articles):
        message += " Feed excerpts were used."
    return message


# --------------------------------------------------------------------------- Pipeline
FUNNEL_STAGES = (
    "discovered",
    "after_date_filter",
    "after_dedupe",
    "after_keyword_relevance_filter",
    "url_valid",
    "decode_resolve_ok",
    "fetch_ok",
    "text_200",
    "relevant_after_extraction",
    "kept",
)


def funnel_source(candidate):
    """Choose the source name used for candidate funnel accounting."""
    return candidate.get("feed") or candidate.get("source") or "unknown"


def add_funnel_record(funnel, source):
    """Initialize and return a source's funnel record."""
    if source not in funnel:
        funnel[source] = {
            "counts": {stage: 0 for stage in FUNNEL_STAGES},
            "drop_reasons": {},
            "examples": {},
        }
    return funnel[source]


def add_funnel_drop(funnel, source, stage, reason, title="", url=""):
    """Record a candidate drop reason and retain a bounded diagnostic example."""
    record = add_funnel_record(funnel, source)
    record["drop_reasons"].setdefault(stage, Counter())
    record["drop_reasons"][stage][reason] += 1
    example_key = f"{stage}: {reason}"
    examples = record["examples"].setdefault(example_key, [])
    if len(examples) < 5:
        normalized_url = normalize_url_value(url)
        examples.append({
            "title": title,
            "url": normalized_url,
            "url_type": type(url).__name__,
            "url_repr": repr(url),
            "reason": reason,
        })


def serialize_funnel(funnel):
    """Convert internal funnel counters to JSON-serializable values."""
    return {
        source: {
            "counts": record["counts"],
            "drop_reasons": {
                stage: dict(reasons)
                for stage, reasons in record["drop_reasons"].items()
            },
            "examples": record["examples"],
        }
        for source, record in sorted(funnel.items())
    }


def run(topic, limit, days, use_llm, progress=None):
    """Discover, retrieve, clean, summarize, and serialize a topic's news."""
    session = make_session()
    status = {}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    future_limit = now + timedelta(days=1)

    log.info("Discovering '%s' across %d sources...", topic, len(SEARCH_SOURCES) + len(PUBLISHER_FEEDS))
    cands = discover(session, topic, days, status)
    discovery_failures = [
        failure
        for details in status.values()
        for failure in details.get("non_news_failures", [])
    ]

    funnel = {}
    for name, details in status.items():
        add_funnel_record(funnel, name)
        candidate_count = details.get("candidates", 0)
        kept_count = details.get("kept", candidate_count)
        funnel[name]["counts"]["discovered"] = candidate_count
        funnel[name]["counts"]["after_keyword_relevance_filter"] = kept_count
        if candidate_count > kept_count:
            funnel[name]["drop_reasons"].setdefault(
                "after_keyword_relevance_filter", Counter()
            )["keyword filter"] += candidate_count - kept_count
        non_news = details.get("non_news_dropped", 0)
        if non_news:
            funnel[name]["drop_reasons"].setdefault("after_date_filter", Counter())
            funnel[name]["drop_reasons"]["after_date_filter"]["non-news domain"] += non_news

    # date filter (undated items are kept but flagged) + dedupe
    seen_urls, seen_titles, fresh = set(), [], []
    for c in cands:
        source = funnel_source(c)
        add_funnel_record(funnel, source)
        if c.get("published_at") and (c["published_at"] < cutoff or c["published_at"] > future_limit):
            add_funnel_drop(funnel, source, "after_date_filter", "outside date window",
                            c["title"], c["url"])
            continue
        if not c.get("title") or not c.get("url") or not c.get("source"):
            add_funnel_drop(funnel, source, "after_date_filter",
                            "missing title, source, or URL",
                            c.get("title", ""), c.get("url", "") or "")
            continue
        if is_duplicate(c["title"], c["url"], set(), seen_titles) or norm_url(c["url"]) in seen_urls:
            add_funnel_drop(funnel, source, "after_dedupe", "duplicate",
                            c["title"], c["url"])
            continue
        seen_urls.add(norm_url(c["url"]))
        seen_titles.append(norm_title(c["title"]))
        fresh.append(c)
        funnel[source]["counts"]["after_date_filter"] += 1
        funnel[source]["counts"]["after_dedupe"] += 1
    log.info("%d candidates after date filter and de-duplication", len(fresh))

    articles, backups, failures = [], [], discovery_failures
    kept_urls, kept_titles = set(), []
    max_attempts = max(limit * 10, 40)
    attempts = 0
    bing_resolved_count = 0
    bing_unresolved_count = 0
    google_unresolved = 0
    google_decode_state = {
        "budget": 60,
        "consecutive_failures": 0,
        "last_decode": None,
        "stopped": False,
    }

    def report(msg, stage="retrieve"):
        log.info(msg)
        if progress:
            progress(min(len(articles) + len(backups), limit), limit, msg, stage)

    if progress:
        progress(0, limit, f"Discovered {len(fresh)} candidates", "discover")

    for c in round_robin(fresh):
        try:
            if len(articles) >= limit or attempts >= max_attempts:
                break
            source = funnel_source(c)
            add_funnel_record(funnel, source)
            if c["feed"] == "Google News":
                url = decode_google_url(candidate_url(c), google_decode_state)
                if not url:
                    google_unresolved += 1
                    add_funnel_drop(funnel, source, "decode_resolve_ok",
                                    "google redirect not resolved", c["title"], c["url"])
                    continue
            else:
                url = candidate_url(c)
                if c["feed"] == "Bing News":
                    url = resolve_link(c["feed"], c["_entry"]) or url

            if c["feed"] == "Bing News":
                url, bing_resolved = resolve_bing_link(session, url)
                if bing_resolved:
                    bing_resolved_count += 1
                else:
                    bing_unresolved_count += 1
                    add_funnel_drop(funnel, source, "decode_resolve_ok",
                                    "bing URL unresolved", c["title"], c["url"])
                    continue
            attempts += 1
            funnel[source]["counts"]["decode_resolve_ok"] += 1

            flags = []
            if c.get("_full_text"):
                if c["feed"] == "Guardian Open Platform":
                    flags.append("full text from API")
                else:
                    flags.append("full text from feed")

            url, url_error = validate_url(url)
            if url_error:
                add_funnel_drop(funnel, source, "url_valid", url_error,
                                c["title"], c["url"])
                continue
            funnel[source]["counts"]["url_valid"] += 1
            candidate_host = host_from_url(url)
            if is_social_url(url):
                reason = "social domain"
                log.info("%s: skipped %s", candidate_host or "unknown", reason)
                failures.append({"source": c["source"], "url": url, "error": reason})
                add_funnel_drop(funnel, source, "url_valid", reason,
                                c["title"], c["url"])
                continue
            exclusion_reason = non_news_reason(url)
            if exclusion_reason:
                reason = exclusion_reason
                log.info("%s: skipped %s", candidate_host or "unknown", reason)
                failures.append({"source": c["source"], "url": url, "error": reason})
                add_funnel_drop(funnel, source, "url_valid", reason,
                                c["title"], c["url"])
                continue
            if candidate_host == "news.google.com":
                reason = "google redirect not resolved"
                log.info("%s: skipped %s", candidate_host, reason)
                failures.append({"source": c["source"], "url": url, "error": reason})
                add_funnel_drop(funnel, source, "decode_resolve_ok", reason,
                                c["title"], c["url"])
                continue

            if progress:
                progress(
                    min(len(articles) + len(backups), limit),
                    limit,
                    f"Retrieving {c['title'][:60]}",
                    "retrieve",
                )
            full_text = c.get("_full_text")
            if full_text:
                text = full_text
                final_url = url
                err = None
                funnel[source]["counts"]["fetch_ok"] += 1
            else:
                time.sleep(REQUEST_DELAY)
                try:
                    text, final_url, err = fetch_article(session, url)
                except Exception as exc:  # noqa: BLE001
                    text, final_url = None, url
                    err = f"article processing failed: {str(exc)[:100]}"
                if err:
                    flags.append(f"article fetch failed: {err}")
                    failures.append({"source": c["source"], "url": final_url, "error": err})
                    log.info("%s: fetch failed: %s", host_from_url(final_url) or "unknown", err)
                    add_funnel_drop(funnel, source, "fetch_ok", err,
                                    c["title"], final_url)
                else:
                    funnel[source]["counts"]["fetch_ok"] += 1
            if progress:
                progress(
                    min(len(articles) + len(backups), limit),
                    limit,
                    f"Cleaning {c['title'][:60]}",
                    "clean",
                )
            if is_duplicate(c["title"], final_url, kept_urls, kept_titles):
                add_funnel_drop(funnel, source, "after_dedupe", "duplicate within kept articles",
                                c["title"], final_url)
                continue
            excerpt_only = False
            if c.get("_full_text"):
                body = clean_html(text) if text else None
            else:
                body = clean_html(text) if text and len(clean_html(text)) >= 200 else None
                if not body:
                    if text:
                        flags.append("extracted text very short")
                    body = useful_snippet(c["title"], c["snippet"])
                    if len(body) < 80:
                        reason = "only headline available"
                        log.info("%s: skipped %s", candidate_host or "unknown", reason)
                        failures.append({"source": c["source"], "url": final_url or url, "error": reason})
                        continue
                    flags.append("using feed excerpt only (full text unavailable)")
                    excerpt_only = True
            if body and len(body) >= 200:
                funnel[source]["counts"]["text_200"] += 1
            elif text:
                add_funnel_drop(funnel, source, "text_200", "text shorter than 200 chars",
                                c["title"], final_url)
            if not body or len(body) < 40:
                report(f"skip (no usable text): {c['title'][:70]}", "clean")
                log.info("%s: skipped no usable text", candidate_host or "unknown")
                add_funnel_drop(funnel, source, "text_200", "no usable text",
                                c["title"], final_url)
                continue
            if not is_relevant(topic, c["title"], body + " " + c["snippet"]):
                report(f"skip (off-topic): {c['title'][:70]}", "clean")
                add_funnel_drop(funnel, source, "relevant_after_extraction", "off-topic",
                                c["title"], final_url)
                continue
            funnel[source]["counts"]["relevant_after_extraction"] += 1
            if not c["published_at"]:
                flags.append("published_at missing")

            if progress:
                progress(
                    min(len(articles) + len(backups), limit),
                    limit,
                    f"Summarizing {c['title'][:60]}",
                    "summarize",
                )
            try:
                extractive = extractive_summary(c["title"], body)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not summarize article from %s: %s", c["source"], exc)
                extractive = None
            llm = _validate_llm_summary(llm_summary(c["title"], body)) if use_llm else None
            summary, method = (llm, "llm") if llm else (extractive, "extractive")
            if not summary:
                flags.append("summary could not be generated")
            else:
                normalized_title = re.sub(r"\W+", " ", c["title"]).strip().lower()
                normalized_summary = re.sub(r"\W+", " ", summary).strip().lower()
                remainder = normalized_summary[len(normalized_title):].strip() if normalized_summary.startswith(normalized_title) else ""
                if normalized_summary == normalized_title or (normalized_summary.startswith(normalized_title) and len(remainder) < 40):
                    summary = extractive
                    method = "extractive"
                    if not summary:
                        flags.append("summary could not be generated")

            # Summaries are computed from the full body; only the stored excerpt is trimmed.
            excerpt = excerpt_text(body, 1200) if body else ""
            kept_urls.add(norm_url(final_url))
            kept_titles.append(norm_title(c["title"]))
            record = {
                "title": c["title"], "source": normalize_publisher(c["source"]), "url": final_url,
                "published_at": c["published_at"].isoformat() if c["published_at"] else None,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "excerpt": excerpt, "summary": summary, "summary_method": method,
                "discovered_via": c["feed"], "flags": flags,
            }
            # Prefer full-text articles; excerpt-only ones are kept as backups to fill any gap.
            (backups if excerpt_only else articles).append(record)
            report(
                f"[{min(len(articles) + len(backups), limit)}/{limit}] "
                f"{c['source']} - {c['title'][:60]}",
                "summarize",
            )

        except Exception as exc:  # noqa: BLE001
            error = _safe_error_message(exc)[:150]
            source_name = c.get("feed") or c.get("source") or "unknown"
            source_status = status.setdefault(source_name, {
                "ok": False, "candidates": 0, "kept": 0
            })
            source_status["article_errors"] = source_status.get("article_errors", 0) + 1
            source_status["last_article_error"] = error
            failures.append({
                "source": c.get("source") or source_name,
                "url": normalize_url_value(candidate_url(c)) or "",
                "error": f"article processing failed: {error}",
            })
            add_funnel_drop(
                funnel, source_name, "article_processing", "article processing failed",
                c.get("title", ""), normalize_url_value(candidate_url(c)) or "",
            )
            log.warning("Article from '%s' was skipped: %s", source_name, error)

    for b in backups:
        if len(articles) >= limit:
            break
        articles.append(b)

    for article in articles:
        source = article["discovered_via"] or article["source"]
        add_funnel_record(funnel, source)
        funnel[source]["counts"]["kept"] += 1

    if bing_resolved_count or bing_unresolved_count:
        log.info("Bing News: resolved %d candidates, unresolved %d candidates",
                 bing_resolved_count, bing_unresolved_count)
    if google_unresolved:
        log.info("Google News: %d undecoded candidates; decoding stopped or remaining links dropped",
                 google_unresolved)

    sources_used = sorted({normalize_publisher(a["source"]) for a in articles})
    within_window_count = sum(1 for article in articles if article["published_at"])
    undated_count = len(articles) - within_window_count
    failure_summary = build_failure_summary(failures, google_unresolved)
    failure_reasons = [
        f"{category}: {details['count']}"
        for category, details in failure_summary.items()
        if details["count"]
    ]
    unavailable_sources = [
        f"{name}: {details.get('error', 'unavailable')}"
        for name, details in status.items()
        if not details.get("ok") and details.get("error") != "GUARDIAN_API_KEY not set"
    ]
    failure_reasons.extend(unavailable_sources[:4])
    if len(unavailable_sources) > 4:
        failure_reasons.append(f"{len(unavailable_sources) - 4} more sources unavailable")
    reasons = "; ".join(failure_reasons) or (
        "date-window, duplicate, relevance, or source-availability filters reduced the candidate pool"
    )
    notes = []
    if len(articles) < 5:
        notes.append(
            f"Shortfall: {len(articles)} qualifying articles were found in the past {days} days "
            f"(five-article goal; requested maximum: {limit}). Reasons: {reasons}. "
            f"{unusable_page_message(failures, articles)}"
        )
    if len(sources_used) < 2:
        notes.append("Fewer than two distinct publishers contributed results.")
    if use_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        notes.append(
            "LLM summaries were requested (--llm) but ANTHROPIC_API_KEY is not set; "
            "extractive summaries were used instead."
        )
    serialized_funnel = serialize_funnel(funnel)
    for source, record in serialized_funnel.items():
        for example_key, examples in record["examples"].items():
            if examples:
                for example in examples:
                    log.info("Funnel skip [%s / %s] title=%s url=%s",
                             source, example_key, example["title"], example["url"])
    close = getattr(session, "close", None)
    if close:
        close()
    return {"topic": topic, "generated_at": now.isoformat(), "window_days": days,
            "requested_limit": limit, "article_count": len(articles),
            "within_window_count": within_window_count, "undated_count": undated_count,
            "publishers": sources_used, "notes": notes, "source_status": status, "fetch_failures": failures,
            "failure_summary": failure_summary, "funnel": serialized_funnel,
            "articles": articles}


# --------------------------------------------------------------------------- Export
def top_themes(articles, topic, n=8):
    """Return recurring non-topic tokens shared by article titles and summaries."""
    skip = set(tokens(topic))
    c = Counter(t for a in articles for t in set(tokens((a["summary"] or "") + " " + a["title"]))
                if t not in skip and len(t) > 3)
    return [w for w, k in c.most_common(n) if k >= 2]


def write_outputs(result, outdir):
    """Write JSON, CSV, and Markdown files for a completed run."""
    os.makedirs(outdir, exist_ok=True)
    articles = [
        {**article, "source": normalize_publisher(article.get("source", ""))}
        for article in result["articles"]
    ]
    publishers = sorted({
        normalize_publisher(publisher)
        for publisher in result.get("publishers", [])
    })
    export_result = {**result, "articles": articles, "publishers": publishers}
    slug = re.sub(r"[^a-z0-9]+", "_", result["topic"].lower()).strip("_")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(outdir, f"{slug}_{stamp}")

    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(export_result, f, indent=2, ensure_ascii=False)

    cols = ["title", "source", "url", "published_at", "retrieved_at", "summary", "summary_method", "flags"]
    with open(base + ".csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for a in articles:
            w.writerow({**a, "flags": "; ".join(a["flags"])})

    lines = [f"# News Digest: {result['topic']}", "",
             f"*Generated {result['generated_at']} UTC · {result['article_count']} articles · "
             f"within window: {result.get('within_window_count', result['article_count'])} · "
             f"window: last {result['window_days']} days · "
             f"publishers: {', '.join(publishers) or 'none'}*", ""]
    for n in result["notes"]:
        lines += [f"> **Note:** {n}", ""]
    themes = top_themes(articles, result["topic"])
    if themes:
        lines += ["**Recurring themes:** " + ", ".join(themes), ""]
    for i, a in enumerate(articles, 1):
        lines += [f"## {i}. {a['title']}",
                  f"**Source:** {a['source']} · **Published:** {a['published_at'] or 'unknown'}  ",
                  f"**Link:** {a['url']}", "",
                  a["summary"] or "_Summary unavailable._", ""]
        if a["flags"]:
            lines += ["_Data flags: " + "; ".join(a["flags"]) + "_", ""]
    blocked = result.get("fetch_failures", [])
    page_message = unusable_page_message(blocked, articles)
    if unusable_page_count(blocked):
        lines += ["---", f"**{page_message}**"]
    failure_summary = result.get("failure_summary", {})
    if failure_summary:
        lines += ["---", "## Fetch failure summary"]
        for category in FAILURE_CATEGORIES:
            details = failure_summary.get(category, {"count": 0, "top_domains": []})
            domain_text = ", ".join(
                f"{item['domain']} ({item['count']})"
                for item in details["top_domains"]
            )
            lines.append(f"- **{category}:** {details['count']}"
                         + (f"; top domains: {domain_text}" if domain_text else ""))
    failed = [f"{k} ({v['error']})" for k, v in result["source_status"].items() if not v["ok"]]
    if failed:
        lines += ["---", "**Sources unavailable this run:** " + "; ".join(failed)]
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return base


def main():
    """Parse CLI arguments, run the pipeline, and print the final report."""
    ap = argparse.ArgumentParser(description="Discover and summarize recent news on a topic.")
    ap.add_argument("--topic", help="topic or keywords, e.g. 'AI regulation'")
    ap.add_argument("--limit", type=int, default=5, help="max number of articles (default 5)")
    ap.add_argument("--days", type=int, default=7, help="only articles from the last N days (default 7)")
    ap.add_argument("--out", default="output", help="output directory")
    ap.add_argument("--llm", action="store_true",
                    help="optional: summarize with Claude (needs ANTHROPIC_API_KEY); default is non-LLM")
    ap.add_argument("--funnel-json", action="store_true",
                    help="write the candidate funnel to funnel.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    for noisy in ("httpx", "httpcore", "urllib3", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    topic = args.topic or input("Topic: ").strip()
    if not topic:
        sys.exit("A topic is required.")
    if args.limit < 1:
        sys.exit("--limit must be >= 1")

    if args.llm and not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: --llm requested but ANTHROPIC_API_KEY is not set; "
              "using extractive summaries.", file=sys.stderr)

    result = run(topic, args.limit, args.days, args.llm)
    if args.funnel_json:
        with open("funnel.json", "w", encoding="utf-8") as funnel_file:
            json.dump(result["funnel"], funnel_file, indent=2, ensure_ascii=False)
    base = write_outputs(result, args.out)
    print(f"\nDone: {result['article_count']} articles from {len(result['publishers'])} publishers.")
    for n in result["notes"]:
        print("NOTE:", n)
    if unusable_page_count(result.get("fetch_failures", [])):
        print(unusable_page_message(result["fetch_failures"], result["articles"]))
    print("Failure summary:")
    for category in FAILURE_CATEGORIES:
        details = result["failure_summary"].get(category, {"count": 0, "top_domains": []})
        domain_text = ", ".join(
            f"{item['domain']} ({item['count']})"
            for item in details["top_domains"]
        )
        print(f"- {category}: {details['count']}"
              + (f"; top domains: {domain_text}" if domain_text else ""))
    articles_used = Counter(article["discovered_via"] for article in result["articles"])
    excerpt_only = sum("using feed excerpt only" in "; ".join(article["flags"]) for article in result["articles"])
    full_text = result["article_count"] - excerpt_only
    distinct_publishers = len(result["publishers"])
    print(f"\nCounts: articles={result['article_count']}, "
          f"within-window={result.get('within_window_count', result['article_count'])}, "
          f"undated={result.get('undated_count', 0)}, full-text={full_text}, "
          f"excerpt-only={excerpt_only}, distinct-publishers={distinct_publishers}")
    print("\nSource status:")
    print(f"{'Source':<28} {'Status':<8} {'Candidates':<10} {'Kept':<8} {'Articles used':<13} Error")
    print("-" * 105)
    for source, details in result["source_status"].items():
        state = "ok" if details["ok"] else "failed"
        candidates = details.get("candidates", 0)
        kept = details.get("kept", candidates)
        used = articles_used.get(source, 0)
        error = details.get("error", "")
        print(f"{source[:28]:<28} {state:<8} {candidates:<10} {kept:<8} {used:<13} {error}")
    print("\nCandidate funnel:")
    print(f"{'Source':<28} {'Disc':>5} {'Date':>5} {'Dedup':>6} {'Keyword':>8} "
          f"{'URL':>5} {'Resolve':>8} {'Fetch':>6} {'Text200':>8} {'Relevant':>9} {'Kept':>5}")
    print("-" * 105)
    for source, record in result["funnel"].items():
        counts = record["counts"]
        print(
            f"{source[:28]:<28} "
            f"{counts['discovered']:>5} "
            f"{counts['after_date_filter']:>5} "
            f"{counts['after_dedupe']:>6} "
            f"{counts['after_keyword_relevance_filter']:>8} "
            f"{counts['url_valid']:>5} "
            f"{counts['decode_resolve_ok']:>8} "
            f"{counts['fetch_ok']:>6} "
            f"{counts['text_200']:>8} "
            f"{counts['relevant_after_extraction']:>9} "
            f"{counts['kept']:>5}"
        )
        drop_reasons = []
        for stage, reasons in record["drop_reasons"].items():
            for reason, count in reasons.items():
                drop_reasons.append(f"{stage}: {reason} ({count})")
        if drop_reasons:
            print(f"  drops: {'; '.join(drop_reasons)}")
    print("\nURL drop diagnostics (first 5 per source/reason):")
    for source, record in result["funnel"].items():
        for example_key, examples in record["examples"].items():
            if not example_key.startswith("url_valid:"):
                continue
            for example in examples:
                print(
                    f"{source}: type(url)={example['url_type']} "
                    f"repr(url)={example['url_repr']} reason={example['reason']}"
                )
    print(f"Files: {base}.json | .csv | .md")


if __name__ == "__main__":
    main()
