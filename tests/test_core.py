import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import requests

import main
from main import (
    norm_url, is_duplicate, is_relevant, extractive_summary, sentences,
    discover_guardian, parse_gdelt_payload, topic_tokens, decode_google_url,
    expand_topic_query, discover, validate_url, normalize_url_value, candidate_url,
)


def test_norm_url_ignores_query_www_and_trailing_slash():
    assert norm_url("https://www.example.com/a/b/?utm=1#x") == norm_url("https://example.com/a/b")


def test_duplicate_by_url():
    assert is_duplicate("Anything", "https://example.com/a?x=1", {norm_url("https://example.com/a")}, [])


def test_duplicate_by_similar_title():
    from main import norm_title
    seen = [norm_title("Hackers breach major hospital network in Europe")]
    assert is_duplicate("Hackers breach major hospital network in Europe!", "https://other.com/x", set(), seen)


def test_relevance_title_match():
    assert is_relevant("cybersecurity", "New cybersecurity rules announced", "short body")


def test_relevance_rejects_offtopic():
    assert not is_relevant("cybersecurity", "Local team wins cup final", "The match ended 2-1 after extra time.")


def test_relevance_requires_topic_coverage_and_matches_simple_plurals():
    assert not is_relevant(
        "AI regulation",
        "New policy announced",
        "The regulation was discussed once during a short briefing.",
    )
    assert is_relevant(
        "electric vehicles",
        "Electric vehicle sales grow",
        "The report covers the market.",
    )
    assert is_relevant(
        "electric vehicles",
        "Transport report",
        "Electric vehicle sales rose. Electric vehicles also changed fleet plans.",
    )


def test_relevance_accepts_alias_phrase_and_requires_three_body_mentions():
    assert is_relevant(
        "AI regulation",
        "New rules announced",
        "Artificial intelligence regulation was approved.",
    )
    assert not is_relevant(
        "AI regulation",
        "New rules announced",
        "Regulation appeared once, but artificial intelligence did not.",
    )
    assert is_relevant(
        "AI regulation",
        "New rules announced",
        "AI systems discuss regulation. AI policy includes regulation. "
        "More AI guidance addresses regulation.",
    )


def test_summary_is_subset_of_source_text():
    text = ("Researchers disclosed a critical flaw in a widely used router on Monday. "
            "The vendor said a patch would ship within two weeks for all affected models. "
            "Attackers could exploit the bug remotely without any credentials at all. "
            "Customers are urged to disable remote administration until the update arrives. "
            "The company did not say whether the flaw had been exploited in the wild.")
    summary = extractive_summary("Router flaw disclosed", text)
    assert summary
    for s in sentences(summary):
        assert s in text  # grounded: every summary sentence comes verbatim from the article


def test_paragraph_aware_sentences_drop_headings_and_repeated_titles():
    title = "Heading Here"
    text = (
        "Heading Here\n"
        "First real sentence with enough words in it to count as one. "
        "Second real sentence follows here with more words."
    )

    summary = extractive_summary(title, text)

    assert summary
    assert "Heading Here" not in summary
    assert "First real sentence" in summary
    assert "Second real sentence" in summary
    assert all("\n" not in sentence for sentence in sentences(text, title))

    repeated = (
        "Heading Here: First real sentence contains enough words to remain useful "
        "and informative. Second real sentence follows with additional detail."
    )
    assert all("Heading Here" not in sentence for sentence in sentences(repeated, title))
    punctuation_variant = (
        "Large scale attacks in Italy: First sentence explains the event in enough "
        "detail for a concise news summary. Second sentence provides additional facts."
    )
    assert all(
        not sentence.lower().startswith("large scale attacks in italy")
        for sentence in sentences(punctuation_variant, "Large-scale attacks in Italy")
    )


def test_clean_html_and_extraction_preserve_paragraph_line_breaks(monkeypatch):
    plain_text = "First paragraph stays on its line.\nSecond paragraph stays separate."
    assert main.clean_html(plain_text) == plain_text
    assert "\n" in main.clean_html("<p>First paragraph.</p><p>Second paragraph.</p>")

    monkeypatch.setattr(main, "trafilatura", Mock(extract=lambda *_args, **_kwargs: plain_text))
    assert main.extract_text("<html>unused</html>") == plain_text

    monkeypatch.setattr(main, "trafilatura", Mock(extract=lambda *_args, **_kwargs: None))
    html_text = (
        "<p>" + ("First paragraph has enough words to be included by the fallback extractor. " * 2)
        + "</p><p>" + ("Second paragraph also has enough words to be included in the result. " * 2)
        + "</p>"
    )
    extracted = main.extract_text(html_text)
    assert "\n" in extracted
    assert "First paragraph" in extracted and "Second paragraph" in extracted


def test_quote_safe_sentence_splitting_and_summary_validation():
    fragments = main._sentence_fragments(
        'It said it was “a cybersecurity incident.” The bureau agreed.'
    )
    assert fragments[0] == 'It said it was “a cybersecurity incident.”'
    assert fragments[0].endswith("”")

    unbalanced_source = (
        "Cybersecurity officials described the incident as “a major service interruption "
        "that affected several public systems. "
        "The response team restored access after reviewing network activity. "
        "Officials said the investigation remains active and updates will follow."
    )
    summary = extractive_summary("Cybersecurity incident response", unbalanced_source)
    assert summary
    assert summary.count("“") == summary.count("”")
    assert "“a major service interruption" not in summary
    assert main._validate_llm_summary(
        'First sentence has enough details to explain the issue “without closing. '
        "Second sentence adds another grounded detail from the article."
    ) is None


def test_boilerplate_lines_are_removed_and_leading_prefixes_are_stripped():
    substantive = (
        "Log in First real sentence explains the cybersecurity incident with enough "
        "specific detail for a reader to understand the response. "
        "The agency confirmed that its investigation continues today."
    )
    result = sentences(substantive)
    assert len(result) == 2
    assert all("Log in" not in sentence for sentence in result)

    boilerplate_line = (
        "This page offers access to your privacy choices before readers continue "
        "to the article content and review the reporting on this subject."
    )
    assert sentences(boilerplate_line) == []
    assert all(
        phrase in main.BOILERPLATE
        for phrase in (
            "log in", "sign in", "share this", "related:", "read also",
            "listen to this article", "your privacy choices",
            "skip to main content", "advertisement",
        )
    )


def test_guardian_parser_uses_mocked_api_response(monkeypatch):
    monkeypatch.setenv("GUARDIAN_API_KEY", "test-key")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "response": {
            "results": [{
                "webTitle": "Regulators approve AI safeguards",
                "webUrl": "https://www.theguardian.com/example",
                "webPublicationDate": "2026-11-23T10:00:00Z",
                "fields": {
                    "bodyText": "The regulation establishes two complete sentences about AI safeguards.",
                    "trailText": "A short trail.",
                },
            }]
        }
    }
    session = Mock()
    session.get.return_value = response

    candidates, error = discover_guardian(session, "AI regulation", 7)

    assert error is None
    assert candidates[0]["source"] == "The Guardian"
    assert candidates[0]["feed"] == "Guardian Open Platform"
    assert "two complete sentences" in candidates[0]["_full_text"]


def test_gdelt_parser_uses_mocked_response():
    payload = {
        "articles": [{
            "title": "AI rules announced",
            "url": "https://example.com/ai-rules",
            "domain": "www.cbsnews.com",
            "seendate": "20261123T100000Z",
        }]
    }

    candidates = parse_gdelt_payload(payload)

    assert len(candidates) == 1
    assert candidates[0]["title"] == "AI rules announced"
    assert candidates[0]["source"] == "CBS News"


def test_publisher_feed_mapping_and_domain_normalization():
    assert len(main.PUBLISHER_FEEDS) == 32
    assert "The Guardian World" not in main.PUBLISHER_FEEDS
    assert "VentureBeat" not in main.PUBLISHER_FEEDS
    assert set(main.FEED_PUBLISHER) == set(main.PUBLISHER_FEEDS)
    assert main.FEED_PUBLISHER["BBC Technology"] == "BBC News"
    assert main.FEED_PUBLISHER["The Guardian Science"] == "The Guardian"
    assert main.FEED_PUBLISHER["NPR Business"] == "NPR"
    assert main.FEED_PUBLISHER["CNBC Tech"] == "CNBC Tech"

    mappings = {
        "cbsnews.com": "CBS News",
        "www.bbc.co.uk": "BBC News",
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
    for domain, publisher in mappings.items():
        assert main.normalize_publisher("  " + domain + "  ") == publisher
    assert main.normalize_publisher("  Example News  ") == "Example News"
    assert main.normalize_publisher("cbsnews.example") == "Cbsnews"
    assert main.normalize_publisher("stackbuilders.com") == "Stackbuilders"
    assert main.normalize_publisher("www.example.co.uk") == "Example"
    assert main.normalize_publisher("reports.example.com.au") == "Example"
    assert main.normalize_publisher("www.news.ycombinator.com") == "Ycombinator"


def test_discovery_uses_normalized_publishers_for_rss_candidates(monkeypatch):
    from types import SimpleNamespace

    entries = {
        "Google News": [{
            "title": "AI regulation update - CBS News",
            "source": {"title": "www.cbsnews.com"},
            "link": "https://news.example/google",
            "summary": "",
        }],
        "Bing News": [{
            "title": "AI regulation report",
            "source": {"title": "bbc.co.uk"},
            "link": "https://news.example/bing",
            "summary": "",
        }],
        "BBC Technology": [{
            "title": "AI regulation research",
            "link": "https://news.example/bbc",
            "summary": "",
        }],
    }
    monkeypatch.setattr(
        main,
        "SEARCH_SOURCES",
        {"Google News": "", "Bing News": ""},
    )
    monkeypatch.setattr(main, "PUBLISHER_FEEDS", {"BBC Technology": ""})
    monkeypatch.setattr(main, "discover_guardian", lambda *_args: ([], None))
    monkeypatch.setattr(main, "discover_hacker_news", lambda *_args: ([], None))
    monkeypatch.setattr(main, "discover_gdelt", lambda *_args: ([], None))
    monkeypatch.setattr(
        main,
        "fetch_feed",
        lambda _session, name, _url: SimpleNamespace(entries=entries[name]),
    )

    candidates = discover(Mock(), "AI regulation", 7, {})
    by_feed = {candidate["feed"]: candidate["source"] for candidate in candidates}

    assert by_feed == {
        "Google News": "CBS News",
        "Bing News": "BBC News",
        "BBC Technology": "BBC News",
    }


def test_hacker_news_publisher_domain_is_normalized():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "hits": [{
            "url": "https://www.bbc.co.uk/news/example",
            "title": "AI regulation update",
            "created_at_i": int(datetime.now(timezone.utc).timestamp()),
            "story_text": "",
        }]
    }
    candidates, error = main.discover_hacker_news(Mock(get=Mock(return_value=response)), "AI regulation", 7)
    assert error is None
    assert candidates[0]["source"] == "BBC News"
    assert candidates[0]["feed"] == "Hacker News"


def test_topic_tokens_keeps_ai():
    assert "ai" in topic_tokens("AI regulation")


def test_word_boundary_rejects_said_for_ai():
    assert not is_relevant("AI", "Software policy", "The proposal was said to be against regulation.")
    assert not main.word_boundary_contains("said", "AI")


def test_long_keyword_matching_accepts_hyphens_and_spaces_in_direct_feeds(monkeypatch):
    assert main.word_boundary_contains("Cyber-security chief", "cybersecurity")
    assert main.word_boundary_contains("Cyber Security Update", "cybersecurity")
    assert not main.word_boundary_contains("said", "cybersecurity")

    from types import SimpleNamespace

    monkeypatch.setattr(main, "SEARCH_SOURCES", {})
    monkeypatch.setattr(main, "PUBLISHER_FEEDS", {"Test Feed": "https://example.com/feed"})
    monkeypatch.setattr(main, "discover_guardian", lambda *_args: ([], None))
    monkeypatch.setattr(main, "discover_hacker_news", lambda *_args: ([], None))
    monkeypatch.setattr(main, "discover_gdelt", lambda *_args: ([], None))
    monkeypatch.setattr(
        main,
        "fetch_feed",
        lambda *_args: SimpleNamespace(entries=[{
            "title": "Local policy update",
            "summary": "Cyber Security Update from the agency.",
            "link": "https://example.com/article",
        }]),
    )

    candidates = discover(Mock(), "cybersecurity", 7, {})
    assert len(candidates) == 1


def test_alias_expansion_for_ai():
    assert expand_topic_query("AI regulation") == '"artificial intelligence" regulation'


def test_feed_content_creates_full_text_candidate(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(main, "SEARCH_SOURCES", {})
    monkeypatch.setattr(main, "PUBLISHER_FEEDS", {"Test Feed": "https://example.com/feed"})
    monkeypatch.setattr(main, "discover_guardian", lambda *_args: ([], "GUARDIAN_API_KEY not set"))
    monkeypatch.setattr(main, "discover_hacker_news", lambda *_args: ([], None))
    monkeypatch.setattr(main, "discover_gdelt", lambda *_args: ([], None))
    monkeypatch.setattr(main, "fetch_feed", lambda *_args: SimpleNamespace(entries=[{
        "title": "AI regulation update",
        "summary": "AI regulation summary",
        "link": "https://example.com/article",
        "content": [{"value": "<p>" + ("Artificial intelligence policy details. " * 30) + "</p>"}],
    }]))

    status = {}
    candidates = discover(Mock(), "AI regulation", 7, status)

    assert len(candidates) == 1
    assert len(candidates[0]["_full_text"]) >= 500


def test_excerpt_text_truncates_at_sentence_and_marks_truncation():
    sentence = "AI regulation sets clear reporting rules for public agencies."
    excerpt = main.excerpt_text(sentence + " " + ("x" * 1300), limit=80)
    assert excerpt == sentence + "…"
    assert main.excerpt_text("x" * 1301, limit=1200) == ("x" * 1200) + "…"
    assert main.excerpt_text("Short source text.", limit=1200) == "Short source text."


def test_run_summarizes_full_text_but_stores_bounded_excerpt(monkeypatch, tmp_path):
    sentence = (
        "AI regulation researchers described a detailed reporting process for agencies "
        "and technology companies."
    )
    body = " ".join(sentence for _ in range(20))
    candidate = {
        "title": "AI regulation reporting process described",
        "source": "  stackbuilders.com  ",
        "feed": "BBC Technology",
        "url": "https://example.com/ai-regulation",
        "published_at": datetime.now(timezone.utc),
        "snippet": "",
        "_full_text": body,
    }
    summary_inputs = []
    extractive = main.extractive_summary

    def capture_summary_input(title, text):
        summary_inputs.append(text)
        return extractive(title, text)

    monkeypatch.setattr(main, "make_session", Mock(return_value=Mock()))
    monkeypatch.setattr(main, "discover", lambda *_args: [candidate])
    monkeypatch.setattr(main, "extractive_summary", capture_summary_input)

    result = main.run("AI regulation", 1, 7, False)

    assert summary_inputs == [body]
    excerpt = result["articles"][0]["excerpt"]
    assert len(excerpt) <= 1201
    assert excerpt.endswith("…")
    assert result["articles"][0]["summary"]
    assert result["publishers"] == ["Stackbuilders"]
    assert result["articles"][0]["source"] == "Stackbuilders"

    output_base = main.write_outputs(result, str(tmp_path))
    csv_text = Path(output_base + ".csv").read_text(encoding="utf-8")
    digest_text = Path(output_base + ".md").read_text(encoding="utf-8")
    assert "Stackbuilders" in csv_text and "stackbuilders.com" not in csv_text
    assert "Stackbuilders" in digest_text and "stackbuilders.com" not in digest_text


def test_output_files_normalize_domain_sources_without_mutating_input(tmp_path):
    article = {
        "title": "Example article",
        "source": "stackbuilders.com",
        "url": "https://example.com/article",
        "published_at": None,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "excerpt": "A collected source excerpt.",
        "summary": "First grounded sentence with enough detail. Another sentence explains more.",
        "summary_method": "extractive",
        "flags": [],
    }
    result = {
        "topic": "example",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": 7,
        "article_count": 1,
        "within_window_count": 0,
        "publishers": ["stackbuilders.com"],
        "notes": [],
        "articles": [article],
        "fetch_failures": [],
        "failure_summary": {},
        "source_status": {},
    }

    base = main.write_outputs(result, str(tmp_path))

    serialized = json.loads(Path(base + ".json").read_text(encoding="utf-8"))
    csv_text = Path(base + ".csv").read_text(encoding="utf-8")
    digest_text = Path(base + ".md").read_text(encoding="utf-8")
    assert serialized["publishers"] == ["Stackbuilders"]
    assert serialized["articles"][0]["source"] == "Stackbuilders"
    assert "Stackbuilders" in csv_text and "stackbuilders.com" not in csv_text
    assert "Stackbuilders" in digest_text and "stackbuilders.com" not in digest_text
    assert result["publishers"] == ["stackbuilders.com"]
    assert article["source"] == "stackbuilders.com"


def test_google_circuit_breaker_sleeps_retries_then_stops(monkeypatch):
    sleeps = []
    monkeypatch.setattr(main.time, "sleep", sleeps.append)
    monkeypatch.setattr(main.time, "time", lambda: 100.0)

    def failing_decoder(_url, interval=0.5):
        return {"status": False}

    state = {"budget": 60, "consecutive_failures": 0, "last_decode": None, "stopped": False}
    decoded = None
    for _ in range(6):
        decoded = decode_google_url("https://news.google.com/article", state, failing_decoder)

    assert decoded is None
    assert state["stopped"] is True
    assert 3 in sleeps


def test_https_candidate_passes_url_stage():
    url, error = validate_url("https://example.com/a")
    assert url == "https://example.com/a"
    assert error is None


def test_bytes_url_decodes_and_passes():
    url = normalize_url_value(b"https://example.com/a")
    validated, error = validate_url(url)
    assert validated == "https://example.com/a"
    assert error is None


def test_empty_or_none_urls_have_missing_url_reason():
    assert validate_url("") == (None, "missing url")
    assert validate_url(None) == (None, "missing url")


def test_feed_entry_link_only_produces_candidate(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(main, "SEARCH_SOURCES", {})
    monkeypatch.setattr(main, "PUBLISHER_FEEDS", {"Test Feed": "https://example.com/feed"})
    monkeypatch.setattr(main, "discover_guardian", lambda *_args: ([], "GUARDIAN_API_KEY not set"))
    monkeypatch.setattr(main, "discover_hacker_news", lambda *_args: ([], None))
    monkeypatch.setattr(main, "discover_gdelt", lambda *_args: ([], None))
    monkeypatch.setattr(main, "fetch_feed", lambda *_args: SimpleNamespace(entries=[{
        "title": "AI regulation update",
        "summary": "AI regulation summary",
        "link": "https://example.com/article",
    }]))

    candidates = discover(Mock(), "AI regulation", 7, {})
    assert candidate_url(candidates[0]) == "https://example.com/article"


def test_google_bing_gdelt_hn_shapes_pass_url_stage():
    shapes = [
        {"url": "https://news.google.com/rss/articles/1", "_entry": {}},
        {"url": "https://www.bing.com/ck/a?u=https://example.com/a", "_entry": {}},
        {"url": "https://example.com/gdelt", "_entry": {}},
        {"url": "https://example.com/hacker-news", "_entry": {}},
        {"_entry": {"link": "https://example.com/feed-link"}},
    ]
    for candidate in shapes:
        url = candidate_url(candidate)
        validated, error = validate_url(url)
        assert validated
        assert error is None


def test_llm_summary_must_have_two_to_four_sentences():
    assert main._validate_llm_summary("One complete sentence can be too short.") is None
    valid = (
        "First sentence contains enough words to be useful and complete. "
        "Second sentence adds another grounded detail from the source."
    )
    assert main._validate_llm_summary(valid) == valid
    five_sentences = " ".join(
        f"Sentence {number} contains enough words to be complete and meaningful."
        for number in range(1, 6)
    )
    assert main._validate_llm_summary(five_sentences) is None


def test_run_progress_reports_pipeline_stages(monkeypatch):
    candidate = {
        "title": "Cybersecurity teams disclose a critical vulnerability",
        "url": "https://example.com/security-update",
        "source": "Example Publisher",
        "feed": "Example Feed",
        "published_at": datetime.now(timezone.utc),
        "_full_text": (
            "Researchers disclosed a critical cybersecurity flaw on Monday. "
            "The vendor released a patch for all affected systems. "
            "Customers should install the update as soon as possible."
        ),
        "snippet": "",
    }
    monkeypatch.setattr(main, "make_session", lambda: object())
    monkeypatch.setattr(main, "discover", lambda *_args: [candidate])
    stages = []

    result = main.run(
        "cybersecurity",
        1,
        7,
        False,
        progress=lambda _done, _total, _message, stage="retrieve": stages.append(stage),
    )

    assert result["article_count"] == 1
    assert stages == ["discover", "retrieve", "clean", "summarize", "summarize"]


def test_run_enforces_date_window_and_flags_undated_articles(monkeypatch):
    now = datetime.now(timezone.utc)
    body = (
        "Cybersecurity teams disclosed a new vulnerability affecting network routers today. "
        "The vendor said a software update will be available to customers next week."
    )
    candidates = [
        {
            "title": f"Cybersecurity report for {source}",
            "source": source,
            "feed": source,
            "url": f"https://example{index}.com/article",
            "published_at": published,
            "snippet": "",
            "_full_text": body,
            "_entry": {},
        }
        for index, (source, published) in enumerate(
            (
                ("Atlas newsroom", now - timedelta(days=2)),
                ("Birch journal", now - timedelta(days=12)),
                ("Cobalt media", None),
                ("Delta press", now + timedelta(days=1)),
                ("Elm press", now + timedelta(days=2)),
            ),
            start=1,
        )
    ]

    def fake_discover(_session, _topic, _days, status):
        for candidate in candidates:
            status[candidate["feed"]] = {"ok": True, "candidates": 1, "kept": 1}
        return candidates

    monkeypatch.setattr(main, "make_session", Mock(return_value=Mock()))
    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(
        main,
        "llm_summary",
        lambda *_args: "This one-sentence answer should be rejected by the validator.",
    )
    result = main.run("cybersecurity", 5, 7, True)

    assert result["article_count"] == 3
    assert result["within_window_count"] == 2
    assert result["undated_count"] == 1
    assert result["articles"][1]["published_at"] is None
    assert "published_at missing" in result["articles"][1]["flags"]
    assert all(article["summary_method"] == "extractive" for article in result["articles"])
    assert all(
        {"title", "source", "url", "published_at", "retrieved_at", "excerpt", "summary"}
        <= article.keys()
        for article in result["articles"]
    )
    for article in result["articles"]:
        retrieved = datetime.fromisoformat(article["retrieved_at"])
        assert retrieved.tzinfo == timezone.utc
        assert 2 <= len(sentences(article["summary"])) <= 4
    assert any("Shortfall:" in note for note in result["notes"])


def test_cli_counts_section_feeds_as_one_normalized_publisher(
    monkeypatch, tmp_path, capsys
):
    now = datetime.now(timezone.utc)
    body = (
        "AI regulation experts described a new reporting framework for public agencies. "
        "The proposal includes annual reviews and clear compliance timelines."
    )
    candidates = [
        {
            "title": title,
            "source": "  bbc.co.uk  ",
            "feed": feed,
            "url": f"https://example.com/{feed.replace(' ', '-').lower()}",
            "published_at": now,
            "snippet": "",
            "_full_text": body,
        }
        for title, feed in (
            ("AI regulation framework announced", "BBC Technology"),
            ("AI regulation compliance timeline released", "BBC Business"),
        )
    ]

    def fake_discover(_session, _topic, _days, status):
        for candidate in candidates:
            status[candidate["feed"]] = {"ok": True, "candidates": 1, "kept": 1}
        return candidates

    monkeypatch.setattr(main, "make_session", Mock(return_value=Mock()))
    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main.py",
            "--topic",
            "AI regulation",
            "--limit",
            "2",
            "--out",
            str(tmp_path),
        ],
    )

    main.main()

    output = capsys.readouterr().out
    assert "Done: 2 articles from 1 publishers." in output
    assert "distinct-publishers=1" in output
    assert "BBC Technology" in output and "BBC Business" in output
    result = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert result["publishers"] == ["BBC News"]
    assert {article["source"] for article in result["articles"]} == {"BBC News"}


def test_guardian_api_has_distinct_cli_and_funnel_source(monkeypatch, tmp_path, capsys):
    candidate = {
        "title": "AI regulation safeguards take effect",
        "source": "The Guardian",
        "feed": "Guardian Open Platform",
        "url": "https://www.theguardian.com/politics/example",
        "published_at": datetime.now(timezone.utc),
        "snippet": "",
        "_full_text": (
            "The AI regulation safeguards establish a reporting process for agencies. "
            "Officials said the framework begins this month and includes annual reviews."
        ),
    }

    def fake_discover(_session, _topic, _days, status):
        status["Guardian Open Platform"] = {"ok": True, "candidates": 1, "kept": 1}
        status["The Guardian"] = {"ok": True, "candidates": 0, "kept": 0}
        return [candidate]

    monkeypatch.setattr(main, "make_session", Mock(return_value=Mock()))
    monkeypatch.setattr(main, "discover", fake_discover)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main.py",
            "--topic",
            "AI regulation",
            "--limit",
            "1",
            "--out",
            str(tmp_path),
        ],
    )

    main.main()

    output = capsys.readouterr().out
    status_line = next(
        line for line in output.splitlines()
        if line.startswith("Guardian Open Platform")
    )
    assert status_line.split()[3:7] == ["ok", "1", "1", "1"]
    assert "Guardian Open Platform" in output.split("Candidate funnel:")[1]
    result = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    article = result["articles"][0]
    assert article["source"] == "The Guardian"
    assert article["discovered_via"] == "Guardian Open Platform"
    assert "full text from API" in article["flags"]
    assert "Guardian Open Platform" in result["funnel"]


def test_non_news_pages_reach_cli_digest_and_summary(
    monkeypatch, tmp_path, capsys, caplog
):
    from types import SimpleNamespace

    entry = {
        "title": "AI regulation coverage",
        "source": {"title": "Stackbuilders"},
        "link": (
            "https://www.stackbuilders.com/case-studies/"
            "engineering-ai-attack-defense-for-main-street/"
        ),
        "summary": "",
    }
    monkeypatch.setattr(main, "SEARCH_SOURCES", {"Google News": ""})
    monkeypatch.setattr(main, "PUBLISHER_FEEDS", {})
    monkeypatch.setattr(main, "discover_guardian", lambda *_args: ([], None))
    monkeypatch.setattr(main, "discover_hacker_news", lambda *_args: ([], None))
    monkeypatch.setattr(main, "discover_gdelt", lambda *_args: ([], None))
    monkeypatch.setattr(
        main,
        "fetch_feed",
        lambda *_args: SimpleNamespace(entries=[entry]),
    )
    caplog.set_level("INFO")
    discovery_status = {}
    candidates = discover(Mock(), "AI regulation", 7, discovery_status)

    assert candidates == []
    assert discovery_status["Google News"]["non_news_dropped"] == 1
    assert discovery_status["Google News"]["non_news_failures"][0]["error"] == "non-news page"
    assert caplog.text.count("skipped non-news page") == 1
    assert main.is_non_news_url("https://sub.github.com/project")
    assert not main.is_non_news_url("https://github.com.example.org/project")
    assert main.non_news_reason(entry["link"]) == "non-news page"
    assert main.classify_failure("non-news page") == "non-news domain"
    for domain in ("prnewswire.com", "businesswire.com", "globenewswire.com"):
        assert main.non_news_reason("https://www." + domain + "/news") == "non-news domain"
    for path in (
        "/case-study", "/case-studies/x", "/customers/", "/whitepaper/x",
        "/webinar", "/pricing", "/careers/jobs", "/solutions/ai", "/products/tool",
    ):
        assert main.non_news_reason("https://example.com" + path) == "non-news page"

    def replay_discovery(_session, _topic, _days, status):
        status.update(discovery_status)
        return []

    monkeypatch.setattr(main, "make_session", Mock(return_value=Mock()))
    monkeypatch.setattr(main, "discover", replay_discovery)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main.py",
            "--topic",
            "AI regulation",
            "--limit",
            "5",
            "--out",
            str(tmp_path),
        ],
    )
    main.main()

    output = capsys.readouterr().out
    assert "- non-news domain: 1; top domains: stackbuilders.com (1)" in output
    result = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert result["failure_summary"]["non-news domain"]["count"] == 1
    digest = next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
    assert "- **non-news domain:** 1; top domains: stackbuilders.com (1)" in digest


def test_llm_without_key_warns_and_records_fallback_note(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(main, "make_session", Mock(return_value=Mock()))
    monkeypatch.setattr(main, "discover", lambda *_args: [])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main.py",
            "--topic",
            "AI regulation",
            "--limit",
            "1",
            "--llm",
            "--out",
            str(tmp_path),
        ],
    )

    main.main()

    captured = capsys.readouterr()
    assert "--llm requested but ANTHROPIC_API_KEY is not set" in captured.err
    result = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    note = "LLM summaries were requested (--llm) but ANTHROPIC_API_KEY is not set"
    assert any(note in item for item in result["notes"])
    digest = next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
    assert note in digest


def test_app_disables_llm_toggle_without_api_key(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = AppTest.from_file(str(Path(main.__file__).with_name("app.py"))).run()

    assert not app.exception
    toggle = next(item for item in app.toggle if item.label == "Summarize with Claude")
    assert toggle.disabled


def test_app_normalizes_domain_publishers_in_digest_and_data_tab(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = AppTest.from_file(str(Path(main.__file__).with_name("app.py"))).run()
    article = {
        "title": "A publisher report",
        "source": "stackbuilders.com",
        "url": "https://example.com/report",
        "published_at": None,
        "excerpt": "A short source excerpt.",
        "summary": "First grounded sentence with sufficient detail. Second sentence adds facts.",
        "summary_method": "extractive",
        "flags": [],
        "discovered_via": "Example Feed",
    }
    app.session_state["result"] = {
        "topic": "report",
        "article_count": 1,
        "articles": [article],
        "publishers": ["stackbuilders.com"],
        "funnel": {},
        "failure_summary": {},
        "source_status": {"Example Feed": {"ok": True, "candidates": 1, "kept": 1}},
        "notes": [],
    }
    app.session_state["base"] = "mock"
    app.session_state["downloads"] = {"md": "", "json": "{}", "csv": ""}
    app.run(timeout=10)

    assert not app.exception
    dataframe = app.dataframe[0].value
    assert dataframe["source"][0] == "Stackbuilders"
    markdown = "\n".join(element.value for element in app.markdown)
    assert "Stackbuilders" in markdown
    assert "stackbuilders.com" not in markdown


def test_cli_and_digest_count_failed_urls_once_and_condition_excerpt_note(
    monkeypatch, tmp_path, capsys
):
    failures = [
        {"source": "Example", "url": "https://example.com/story", "error": "request failed"},
        {
            "source": "Example",
            "url": "https://example.com/story?source=feed",
            "error": "only headline available",
        },
    ]
    article = {
        "title": "Example report",
        "source": "Example",
        "url": "https://example.com/story",
        "published_at": None,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "excerpt": "A feed excerpt.",
        "summary": "First grounded sentence with sufficient detail. Second sentence adds facts.",
        "summary_method": "extractive",
        "flags": ["using feed excerpt only (full text unavailable)"],
        "discovered_via": "Example Feed",
    }
    result = {
        "topic": "example",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": 7,
        "article_count": 1,
        "within_window_count": 0,
        "requested_limit": 5,
        "publishers": ["Example"],
        "notes": [],
        "source_status": {"Example Feed": {"ok": True, "candidates": 1, "kept": 1}},
        "fetch_failures": failures,
        "failure_summary": main.build_failure_summary(failures),
        "funnel": {},
        "articles": [article],
    }
    monkeypatch.setattr(main, "run", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "--topic", "example", "--out", str(tmp_path)],
    )

    main.main()

    output = capsys.readouterr().out
    expected = (
        "1 candidate pages could not be used "
        "(blocked, robots.txt-disallowed, paywalled or headline-only)."
    )
    assert expected in output
    assert "Feed excerpts were used." in output
    digest = next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
    assert f"**{expected} Feed excerpts were used.**" in digest

    assert main.unusable_page_count(failures) == 1
    assert "Feed excerpts were used" not in main.unusable_page_message(failures, [])


def test_cli_completes_and_writes_shortfall_after_all_sources_fail(
    monkeypatch, tmp_path, capsys
):
    class FailedSession:
        headers = {}

        def mount(self, *_args, **_kwargs):
            return None

        def get(self, *_args, **_kwargs):
            raise requests.Timeout("simulated timeout for api-key=test-guardian-key")

    monkeypatch.setattr(main.requests, "Session", FailedSession)
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("GUARDIAN_API_KEY", "test-guardian-key")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main.py",
            "--topic",
            "cybersecurity",
            "--limit",
            "5",
            "--out",
            str(tmp_path),
        ],
    )

    main.main()

    output = capsys.readouterr().out
    assert "Done: 0 articles" in output
    assert "Shortfall: 0 qualifying articles" in output
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert len(list(tmp_path.glob("*.csv"))) == 1
    assert len(list(tmp_path.glob("*.md"))) == 1
    written_json = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert written_json["articles"] == []
    assert any("Shortfall:" in note for note in written_json["notes"])
    assert all(not details["ok"] for details in written_json["source_status"].values())
    assert "test-guardian-key" not in json.dumps(written_json)
    assert any("[redacted]" in details.get("error", "") for details in written_json["source_status"].values())
