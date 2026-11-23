import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from main import (
    norm_url, is_duplicate, is_relevant, extractive_summary, sentences,
    discover_guardian, parse_gdelt_payload, topic_tokens, decode_google_url,
    expand_topic_query, discover,
)
from unittest.mock import Mock
import main


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
    assert "two complete sentences" in candidates[0]["_full_text"]


def test_gdelt_parser_uses_mocked_response():
    payload = {
        "articles": [{
            "title": "AI rules announced",
            "url": "https://example.com/ai-rules",
            "domain": "example.com",
            "seendate": "20261123T100000Z",
        }]
    }

    candidates = parse_gdelt_payload(payload)

    assert len(candidates) == 1
    assert candidates[0]["title"] == "AI rules announced"
    assert candidates[0]["source"] == "example.com"


def test_topic_tokens_keeps_ai():
    assert "ai" in topic_tokens("AI regulation")


def test_word_boundary_rejects_said_for_ai():
    assert not is_relevant("AI", "Software policy", "The proposal was said to be against regulation.")


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
