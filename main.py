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
    "The Guardian World": "https://www.theguardian.com/world/rss",
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
    "VentureBeat": "https://venturebeat.com/feed/",
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
               "follow us", "read more", "advertisement", "privacy policy", "terms of use")

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


# --------------------------------------------------------------------------- HTTP
def make_session():
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


# --------------------------------------------------------------------------- Discovery
def tokens(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS]


def topic_tokens(text):
    """Topic-matching tokens: include short relevant terms such as ai, eu, uk, and ev."""
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 2 and w not in STOPWORDS]


def expand_topic_query(topic):
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
    if len(token) == 2:
        return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", text.lower()) is not None
    return token in tokens(text)


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def clean_html(s):
    return BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)


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
    if isinstance(url, bytes):
        url = url.decode("utf-8", errors="ignore")
    host = urlparse(url).hostname or ""
    return host.lower().removeprefix("www.")


def is_social_url(url):
    host = host_from_url(url)
    return any(host == domain or host.endswith("." + domain) for domain in SOCIAL_DOMAINS)


FAILURE_CATEGORIES = (
    "robots.txt disallowed",
    "HTTP 403/blocked",
    "HTTP other / timeout",
    "google redirect not resolved",
    "headline-only",
    "social domain",
)


def classify_failure(error):
    error_lower = (error or "").lower()
    if "robots.txt" in error_lower or "disallowed" in error_lower:
        return "robots.txt disallowed"
    if "google redirect" in error_lower:
        return "google redirect not resolved"
    if "only headline" in error_lower:
        return "headline-only"
    if "social domain" in error_lower:
        return "social domain"
    if re.search(r"http\s*(401|402|403|451)", error_lower) or "blocked or paywalled" in error_lower:
        return "HTTP 403/blocked"
    return "HTTP other / timeout"


def build_failure_summary(fetch_failures, google_unresolved_count=0):
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


def resolve_link(source_name, entry):
    link = entry.get("link", "")
    if source_name == "Bing News":
        real = parse_qs(urlparse(link).query).get("url")
        if real:
            return real[0]
        real = parse_qs(urlparse(link).query).get("u")
        if real:
            return real[0]
    if source_name == "Google News" and gnewsdecoder:
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


def decode_google_candidates(candidates, decoder=None):
    """Compatibility helper for tests; lazily applies the production decode policy."""
    state = {"budget": 60, "consecutive_failures": 0, "last_decode": None, "stopped": False}
    resolved, unresolved = [], 0
    for candidate in candidates:
        if state["stopped"]:
            unresolved += len(candidates) - len(resolved) - unresolved
            break
        decoded = decode_google_url(candidate["url"], state, decoder)
        if decoded:
            candidate["_resolved_url"] = decoded
            resolved.append(candidate)
        else:
            unresolved += 1
    return resolved, unresolved


def resolve_bing_link(session, link):
    if isinstance(link, bytes):
        link = link.decode("utf-8", errors="ignore")
    extracted = parse_qs(urlparse(link).query).get("url") or parse_qs(urlparse(link).query).get("u")
    if extracted:
        return extracted[0] if extracted[0] else link, bool(extracted[0])
    if host_from_url(link) != "bing.com":
        return link, True
    try:
        response = session.get(link, timeout=TIMEOUT, allow_redirects=True, stream=True)
        response.raise_for_status()
        final_url = getattr(response, "url", None) or link
        return final_url, host_from_url(final_url) != "bing.com"
    except requests.RequestException:
        return link, False


def fetch_feed(session, name, url):
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return feedparser.parse(r.content)


def feed_entry_content(entry):
    pieces = []
    for item in entry.get("content", []) or []:
        value = item.get("value", "") if isinstance(item, dict) else getattr(item, "value", "")
        if value:
            pieces.append(clean_html(value))
    if not pieces:
        pieces.append(clean_html(entry.get("content_encoded", "")))
    return "\n".join(piece for piece in pieces if piece).strip()


def parse_guardian_payload(payload):
    candidates = []
    for item in payload.get("response", {}).get("results", []):
        fields = item.get("fields") or {}
        body = clean_html(fields.get("bodyText") or "")
        if not item.get("webUrl") or not item.get("webTitle"):
            continue
        published = item.get("webPublicationDate")
        try:
            published_at = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
        except (TypeError, ValueError):
            published_at = None
        candidates.append({
            "title": clean_html(item["webTitle"]),
            "source": "The Guardian",
            "feed": "The Guardian",
            "url": item["webUrl"],
            "_entry": {},
            "published_at": published_at,
            "snippet": clean_html(fields.get("trailText") or ""),
            "_full_text": body,
        })
    return candidates


def discover_guardian(session, topic, days):
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
        return [], str(exc)[:150]


def parse_gdelt_payload(payload):
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
            "source": item.get("domain") or host_from_url(url) or "GDELT",
            "feed": "GDELT",
            "url": url,
            "_entry": {},
            "published_at": published_at,
            "snippet": "",
        })
    return candidates


_gdelt_last_request = 0.0


def discover_gdelt(session, topic, days):
    global _gdelt_last_request
    # GDELT is intentionally limited to one request attempt cycle per run.
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
            return [], f"{status_text}: {str(exc)[:120]}"


def discover_hacker_news(session, topic, days):
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
                "source": host_from_url(url) or "Hacker News",
                "feed": "Hacker News",
                "url": url,
                "_entry": {},
                "published_at": published_at,
                "snippet": clean_html(hit.get("story_text") or ""),
            })
        return candidates, None
    except (requests.RequestException, ValueError) as exc:
        return [], str(exc)[:150]


def discover(session, topic, days, status):
    """Return candidate article dicts from all sources. Per-source failures are recorded, not fatal."""
    cands = []
    q = quote_plus(expand_topic_query(topic))
    required_topic_tokens = topic_tokens(topic)

    guardian_cands, guardian_error = discover_guardian(session, topic, days)
    if guardian_error:
        status["Guardian Open Platform"] = {"ok": False, "candidates": 0, "kept": 0, "error": guardian_error}
        if guardian_error != "GUARDIAN_API_KEY not set":
            log.warning("Source 'Guardian Open Platform' unavailable: %s", guardian_error)
    else:
        status["Guardian Open Platform"] = {"ok": True, "candidates": len(guardian_cands), "kept": len(guardian_cands)}
        cands.extend(guardian_cands)

    hn_cands, hn_error = discover_hacker_news(session, topic, days)
    if hn_error:
        status["Hacker News"] = {"ok": False, "candidates": 0, "kept": 0, "error": hn_error}
        log.warning("Source 'Hacker News' unavailable: %s", hn_error)
    else:
        status["Hacker News"] = {"ok": True, "candidates": len(hn_cands), "kept": len(hn_cands)}
        cands.extend(hn_cands)

    gdelt_cands, gdelt_error = discover_gdelt(session, topic, days)
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
                candidate = {"title": title, "source": publisher, "feed": name,
                              "url": url, "_entry": e,
                              "published_at": entry_date(e),
                              "snippet": clean_html(e.get("summary", ""))}
                if len(feed_text) >= 500:
                    candidate["_full_text"] = feed_text
                cands.append(candidate)
                n += 1
            status[name] = {"ok": True, "candidates": n, "kept": n}
        except Exception as ex:  # noqa: BLE001
            status[name] = {"ok": False, "candidates": 0, "kept": 0, "error": str(ex)[:150]}
            log.warning("Source '%s' unavailable: %s", name, ex)

    for name, url in PUBLISHER_FEEDS.items():
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
                full_phrase_match = bool(topic.strip()) and re.search(
                    r"(?<![a-z0-9])" + re.escape(topic.strip().lower()) + r"(?![a-z0-9])",
                    combined_text.lower(),
                )
                if (full_phrase_match or all(word_boundary_contains(combined_text, t) for t in required_topic_tokens)):
                    candidate_url = e.get("link", "")
                    if not candidate_url:
                        continue
                    if is_social_url(candidate_url):
                        log.info("%s: skipped social candidate", host_from_url(candidate_url) or "unknown")
                        continue
                    candidate = {"title": title, "source": name, "feed": name,
                                  "url": candidate_url, "_entry": e,
                                  "published_at": entry_date(e), "snippet": snippet}
                    if len(feed_text) >= 500:
                        candidate["_full_text"] = feed_text
                    cands.append(candidate)
                    n += 1
            status[name] = {"ok": True, "candidates": len(feed.entries), "kept": n}
        except Exception as ex:  # noqa: BLE001
            status[name] = {"ok": False, "candidates": 0, "kept": 0, "error": str(ex)[:150]}
            log.warning("Source '%s' unavailable: %s (%s)", name, ex, url)
    filtered = []
    for candidate in cands:
        if is_social_url(candidate.get("url", "")):
            log.info("%s: skipped social candidate",
                     host_from_url(candidate.get("url", "")) or "unknown")
            continue
        filtered.append(candidate)
    return filtered


# --------------------------------------------------------------------------- Cleaning
def norm_url(url):
    if isinstance(url, bytes):
        url = url.decode("utf-8", errors="ignore")
    if not url:
        return ""
    p = urlparse(url)
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}"


def norm_title(t):
    return " ".join(tokens(t))


def is_duplicate(title, url, seen_urls, seen_titles):
    if norm_url(url) in seen_urls:
        return True
    nt = norm_title(title)
    return any(SequenceMatcher(None, nt, o).ratio() > 0.85 for o in seen_titles)


def is_relevant(topic, title, body):
    tt = topic_tokens(topic)
    if not tt:
        return True
    title_tokens = set(topic_tokens(title))
    if all(t in title_tokens for t in tt):
        return True
    title_match = all(word_boundary_contains(title, t) for t in tt)
    if title_match:
        return True
    return all(word_boundary_contains(body, t) for t in tt)


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
    if trafilatura:
        txt = trafilatura.extract(html, include_comments=False, include_tables=False)
        if txt:
            return txt
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    return "\n".join(p for p in paras if len(p) > 60)


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
    except requests.RequestException as e:
        return None, url, f"request failed: {str(e)[:100]}"


# --------------------------------------------------------------------------- Summarization
def sentences(text):
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r'(?<=[.!?])["”]?\s+(?=[A-Z0-9"“])', text)
    return [s.strip() for s in parts if 40 <= len(s.strip()) <= 400
            and not any(b in s.lower() for b in BOILERPLATE)]


def extractive_summary(title, text, n=3):
    """Pick the n most informative sentences, verbatim from the article (no invented facts)."""
    sents = sentences(text)
    if len(sents) < 2:
        return None
    if len(sents) <= n:
        return " ".join(sents)
    freq = Counter(tokens(text))
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
    top = sorted(sorted(scored, reverse=True)[:n], key=lambda x: x[1])  # restore original order
    return " ".join(s for _, _, s in top)


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


# --------------------------------------------------------------------------- Pipeline
def run(topic, limit, days, use_llm, progress=None):
    session = make_session()
    status = {}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    log.info("Discovering '%s' across %d sources...", topic, len(SEARCH_SOURCES) + len(PUBLISHER_FEEDS))
    cands = discover(session, topic, days, status)

    # date filter (undated items are kept but flagged) + dedupe
    seen_urls, seen_titles, fresh = set(), [], []
    for c in cands:
        if c["published_at"] and c["published_at"] < cutoff:
            continue
        if not c["title"] or not c["url"]:
            continue
        if is_duplicate(c["title"], c["url"], set(), seen_titles) or norm_url(c["url"]) in seen_urls:
            continue
        seen_urls.add(norm_url(c["url"]))
        seen_titles.append(norm_title(c["title"]))
        fresh.append(c)
    log.info("%d candidates after date filter and de-duplication", len(fresh))

    articles, backups, failures = [], [], []
    skipped_headline_or_blocked = 0
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

    def report(msg):
        log.info(msg)
        if progress:
            progress(min(len(articles) + len(backups), limit), limit, msg)

    for c in round_robin(fresh):
        if len(articles) >= limit or attempts >= max_attempts:
            break
        if c["feed"] == "Google News":
            url = decode_google_url(c["url"], google_decode_state)
            if not url:
                google_unresolved += 1
                continue
        else:
            url = resolve_link(c["feed"], c["_entry"])

        if c["feed"] == "Bing News":
            url, bing_resolved = resolve_bing_link(session, url)
            if bing_resolved:
                bing_resolved_count += 1
            else:
                bing_unresolved_count += 1
        attempts += 1

        flags = []
        if c.get("_full_text"):
            if c["feed"] == "The Guardian":
                flags.append("full text from API")
            else:
                flags.append("full text from feed")

        candidate_host = host_from_url(url)
        if is_social_url(url):
            reason = "social domain"
            log.info("%s: skipped %s", candidate_host or "unknown", reason)
            failures.append({"source": c["source"], "url": url, "error": reason})
            skipped_headline_or_blocked += 1
            continue
        if candidate_host == "news.google.com":
            reason = "google redirect not resolved"
            log.info("%s: skipped %s", candidate_host, reason)
            failures.append({"source": c["source"], "url": url, "error": reason})
            skipped_headline_or_blocked += 1
            continue

        full_text = c.get("_full_text")
        if full_text:
            text = full_text
            final_url = url
            err = None
        else:
            time.sleep(REQUEST_DELAY)
            text, final_url, err = fetch_article(session, url)
            if err:
                flags.append(f"article fetch failed: {err}")
                failures.append({"source": c["source"], "url": final_url, "error": err})
                skipped_headline_or_blocked += 1
                log.info("%s: fetch failed: %s", host_from_url(final_url) or "unknown", err)
        if is_duplicate(c["title"], final_url, kept_urls, kept_titles):
            continue
        if c.get("_full_text"):
            body = clean_html(text) if text else None
        else:
            body = clean_html(text) if text and len(clean_html(text)) >= 200 else None
        excerpt_only = False
        if not body:
            if text:
                flags.append("extracted text very short")
            body = useful_snippet(c["title"], c["snippet"])
            if len(body) < 80:
                reason = "only headline available"
                log.info("%s: skipped %s", candidate_host or "unknown", reason)
                failures.append({"source": c["source"], "url": final_url or url, "error": reason})
                skipped_headline_or_blocked += 1
                continue
            flags.append("using feed excerpt only (full text unavailable)")
            excerpt_only = True
        if not body or len(body) < 40:
            report(f"skip (no usable text): {c['title'][:70]}")
            log.info("%s: skipped no usable text", candidate_host or "unknown")
            continue
        if not is_relevant(topic, c["title"], body + " " + c["snippet"]):
            report(f"skip (off-topic): {c['title'][:70]}")
            continue
        if not c["published_at"]:
            flags.append("published_at missing")

        extractive = extractive_summary(c["title"], body)
        llm = llm_summary(c["title"], body) if use_llm else None
        summary, method = (llm, "llm") if llm else (extractive, "extractive")
        if not summary:
            flags.append("summary could not be generated")
        else:
            normalized_title = re.sub(r"\W+", " ", c["title"]).strip().lower()
            normalized_summary = re.sub(r"\W+", " ", summary).strip().lower()
            remainder = normalized_summary[len(normalized_title):].strip() if normalized_summary.startswith(normalized_title) else ""
            if normalized_summary == normalized_title or (normalized_summary.startswith(normalized_title) and len(remainder) < 40):
                summary = None
                method = "extractive"
                flags.append("summary could not be generated")

        kept_urls.add(norm_url(final_url))
        kept_titles.append(norm_title(c["title"]))
        record = {
            "title": c["title"], "source": c["source"], "url": final_url,
            "published_at": c["published_at"].isoformat() if c["published_at"] else None,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "excerpt": body[:3000], "summary": summary, "summary_method": method,
            "discovered_via": c["feed"], "flags": flags,
        }
        # Prefer full-text articles; excerpt-only ones are kept as backups to fill any gap.
        (backups if excerpt_only else articles).append(record)
        report(f"[{min(len(articles) + len(backups), limit)}/{limit}] {c['source']} - {c['title'][:60]}")

    for b in backups:
        if len(articles) >= limit:
            break
        articles.append(b)

    if bing_resolved_count or bing_unresolved_count:
        log.info("Bing News: resolved %d candidates, unresolved %d candidates",
                 bing_resolved_count, bing_unresolved_count)
    if google_unresolved:
        log.info("Google News: %d undecoded candidates; decoding stopped or remaining links dropped",
                 google_unresolved)

    sources_used = sorted({a["source"] for a in articles})
    notes = []
    if len(articles) < limit:
        notes.append(f"Only {len(articles)} qualifying articles were found in the past {days} days "
                     f"(target: {limit}). Sources may be sparse for this topic, blocked, or failed; "
                     f"see source_status. Skipped {skipped_headline_or_blocked} articles "
                     f"for headline-only or blocked content.")
    if len(sources_used) < 2:
        notes.append("Fewer than two distinct publishers contributed results.")
    failure_summary = build_failure_summary(failures, google_unresolved)
    return {"topic": topic, "generated_at": now.isoformat(), "window_days": days,
            "requested_limit": limit, "article_count": len(articles),
            "publishers": sources_used, "notes": notes, "source_status": status, "fetch_failures": failures,
            "failure_summary": failure_summary,
            "articles": articles}


# --------------------------------------------------------------------------- Export
def top_themes(articles, topic, n=8):
    skip = set(tokens(topic))
    c = Counter(t for a in articles for t in set(tokens((a["summary"] or "") + " " + a["title"]))
                if t not in skip and len(t) > 3)
    return [w for w, k in c.most_common(n) if k >= 2]


def write_outputs(result, outdir):
    os.makedirs(outdir, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", result["topic"].lower()).strip("_")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(outdir, f"{slug}_{stamp}")

    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    cols = ["title", "source", "url", "published_at", "retrieved_at", "summary", "summary_method", "flags"]
    with open(base + ".csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for a in result["articles"]:
            w.writerow({**a, "flags": "; ".join(a["flags"])})

    lines = [f"# News Digest: {result['topic']}", "",
             f"*Generated {result['generated_at']} UTC · {result['article_count']} articles · "
             f"window: last {result['window_days']} days · publishers: {', '.join(result['publishers']) or 'none'}*", ""]
    for n in result["notes"]:
        lines += [f"> **Note:** {n}", ""]
    themes = top_themes(result["articles"], result["topic"])
    if themes:
        lines += ["**Recurring themes:** " + ", ".join(themes), ""]
    for i, a in enumerate(result["articles"], 1):
        lines += [f"## {i}. {a['title']}",
                  f"**Source:** {a['source']} · **Published:** {a['published_at'] or 'unknown'}  ",
                  f"**Link:** {a['url']}", "",
                  a["summary"] or "_Summary unavailable._", ""]
        if a["flags"]:
            lines += ["_Data flags: " + "; ".join(a["flags"]) + "_", ""]
    blocked = result.get("fetch_failures", [])
    if blocked:
        lines += ["---", f"**{len(blocked)} article pages could not be fetched** "
                  "(blocked, paywalled or unreachable); feed excerpts were used where available."]
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
    ap = argparse.ArgumentParser(description="Discover and summarize recent news on a topic.")
    ap.add_argument("--topic", help="topic or keywords, e.g. 'AI regulation'")
    ap.add_argument("--limit", type=int, default=5, help="max number of articles (default 5)")
    ap.add_argument("--days", type=int, default=7, help="only articles from the last N days (default 7)")
    ap.add_argument("--out", default="output", help="output directory")
    ap.add_argument("--llm", action="store_true",
                    help="optional: summarize with Claude (needs ANTHROPIC_API_KEY); default is non-LLM")
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

    result = run(topic, args.limit, args.days, args.llm)
    base = write_outputs(result, args.out)
    print(f"\nDone: {result['article_count']} articles from {len(result['publishers'])} publishers.")
    for n in result["notes"]:
        print("NOTE:", n)
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
    print(f"\nCounts: articles={result['article_count']}, full-text={full_text}, "
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
    print(f"Files: {base}.json | .csv | .md")


if __name__ == "__main__":
    main()
