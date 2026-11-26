"""Polished Streamlit news desk for the news digest pipeline.

Run with: streamlit run app.py
"""
import hashlib
import html
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

import main as pipeline


logging.getLogger("httpx").setLevel(logging.WARNING)

st.set_page_config(page_title="Briefly", layout="wide")

SUGGESTIONS = (
    "AI regulation",
    "Cybersecurity",
    "Electric vehicles",
    "Climate policy",
)
PIPELINE_STEPS = ("Discover", "Retrieve", "Clean", "Summarize", "Export")
AVATAR_COLORS = ("#8B6EE8", "#168A83", "#C45B4A", "#5279C7", "#A66B29", "#B24D88")


def _queue_suggestion():
    selected = st.session_state.get("suggestion")
    if isinstance(selected, list):
        selected = selected[0] if selected else None
    if selected in SUGGESTIONS:
        st.session_state["topic_input"] = selected
        st.session_state["fetch_from_suggestion"] = True


def _relative_time(value):
    if not value:
        return "Date unknown"
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_seconds = max(0, (datetime.now(timezone.utc) - published).total_seconds())
    except (TypeError, ValueError):
        return "Date unknown"
    if age_seconds < 60:
        return "Just now"
    if age_seconds < 3600:
        return f"{int(age_seconds // 60)}m ago"
    if age_seconds < 86400:
        return f"{int(age_seconds // 3600)}h ago"
    age_days = int(age_seconds // 86400)
    if age_days == 1:
        return "Yesterday"
    return f"{age_days}d ago"


def _newest_age(articles):
    published = []
    for article in articles:
        value = article.get("published_at")
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            published.append(parsed)
        except (TypeError, ValueError):
            continue
    return _relative_time(max(published).isoformat()) if published else "Unknown"


def _avatar_color(publisher):
    digest = hashlib.sha256((publisher or "Unknown").encode("utf-8")).digest()
    return AVATAR_COLORS[digest[0] % len(AVATAR_COLORS)]


def _safe_source_url(value):
    parsed = urlparse(value or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return value


def _render_article_card(article, index, featured=False):
    publisher = article.get("source") or "Unknown publisher"
    title = article.get("title") or "Untitled article"
    summary = article.get("summary") or "Summary unavailable."
    published = _relative_time(article.get("published_at"))
    excerpt = article.get("excerpt") or ""
    full_text = not any("excerpt only" in flag.lower() for flag in article.get("flags", []))
    badge = "Full text" if full_text else "Excerpt only"
    badge_class = "full-text" if full_text else "excerpt-only"
    words = len(excerpt.split())
    reading_minutes = max(1, round(words / 200))
    source_url = _safe_source_url(article.get("url"))
    safe_url = html.escape(source_url, quote=True)
    source_link = (
        f'<a class="source-link" href="{safe_url}" target="_blank" rel="noopener noreferrer">'
        "Read source ↗</a>"
        if source_url else '<span class="source-link unavailable">Source link unavailable</span>'
    )
    color = _avatar_color(publisher)
    title_class = "article-title featured-title" if featured else "article-title"
    featured_label = '<div class="featured-label">FEATURED STORY</div>' if featured else ""
    safe_publisher = html.escape(publisher)
    safe_title = html.escape(title)
    safe_summary = html.escape(summary)
    safe_published = html.escape(published)
    safe_initial = html.escape((publisher.strip() or "?")[0].upper())
    card_html = f"""
    <article class="article-card">
      {featured_label}
      <div class="article-meta">
        <span class="publisher-avatar" style="background:{color}">{safe_initial}</span>
        <span class="publisher-name">{safe_publisher}</span>
        <span class="meta-separator">·</span>
        <span class="published-time">{safe_published}</span>
        <span class="text-badge {badge_class}">{badge}</span>
      </div>
      <h2 class="{title_class}">{safe_title}</h2>
      <p class="article-summary">{safe_summary}</p>
      <div class="article-footer">
        <span class="reading-time">{reading_minutes} min read</span>
        {source_link}
      </div>
    </article>
    """
    st.markdown(card_html, unsafe_allow_html=True)
    with st.expander(f"Source text · {index}", expanded=False):
        st.text(excerpt or "No extracted source text available.")


def _pipeline_markup(active_index, done, total):
    items = []
    for index, name in enumerate(PIPELINE_STEPS):
        if index < active_index:
            state, marker = "complete", "✓"
        elif index == active_index:
            state, marker = "active", "•"
        else:
            state, marker = "pending", str(index + 1)
        label = f"{name} <span class='step-count'>{done}/{total}</span>" if name in (
            "Retrieve", "Clean", "Summarize"
        ) and index == active_index else name
        items.append(
            f"<div class='pipeline-step {state}'><span class='step-marker'>{marker}</span>"
            f"<span>{label}</span></div>"
        )
    return "<div class='pipeline-steps'>" + "".join(items) + "</div>"


def _failure_reasons(result):
    failure_summary = result.get("failure_summary", {})
    return [
        (category, int(details.get("count", 0)))
        for category, details in failure_summary.items()
        if int(details.get("count", 0)) > 0
    ]


def _render_source_tab(result):
    st.markdown("#### Source health")
    status = result.get("source_status", {})
    used_by_feed = Counter(
        article.get("discovered_via") or article.get("source") or "Unknown"
        for article in result.get("articles", [])
    )
    if status:
        for name, details in status.items():
            ok = bool(details.get("ok"))
            dot_class = "source-dot online" if ok else "source-dot offline"
            state_label = "Online" if ok else "Unavailable"
            candidates = int(details.get("candidates", 0) or 0)
            articles_used = used_by_feed.get(name, 0)
            status_note = f" · {html.escape(str(details.get('error', '')))}" if not ok else ""
            row = (
                "<div class='source-row'>"
                f"<span class='{dot_class}' aria-label='{state_label}'></span>"
                f"<span class='source-name'>{html.escape(str(name))}</span>"
                f"<span class='source-counts'>{candidates} candidates · {articles_used} used"
                f"{status_note}</span></div>"
            )
            st.markdown(row, unsafe_allow_html=True)
    else:
        st.info("No source status was reported for this run.")

    st.markdown("#### Articles by publisher")
    publisher_counts = Counter(article.get("source") or "Unknown" for article in result["articles"])
    if publisher_counts:
        publisher_data = pd.DataFrame(
            sorted(publisher_counts.items(), key=lambda item: (-item[1], item[0])),
            columns=["Publisher", "Articles"],
        )
        st.bar_chart(
            publisher_data,
            x="Publisher",
            y="Articles",
            horizontal=True,
            alt="Articles returned per publisher",
        )
    else:
        st.caption("Publisher counts appear when qualifying articles are found.")

    st.markdown("#### Skipped candidates")
    reasons = _failure_reasons(result)
    if reasons:
        maximum = max(count for _, count in reasons)
        for reason, count in reasons:
            st.progress(count / maximum, text=f"{reason} · {count}")
    else:
        st.caption("No retrieval failure reasons were recorded.")


def _render_data_tab(result, downloads):
    records = result.get("articles", [])
    columns = ["title", "source", "published_at", "url", "summary", "summary_method", "flags"]
    table = pd.DataFrame(
        [
            {
                "title": article.get("title", ""),
                "source": article.get("source", ""),
                "published_at": article.get("published_at", ""),
                "url": _safe_source_url(article.get("url")),
                "summary": article.get("summary") or "",
                "summary_method": article.get("summary_method", ""),
                "flags": "; ".join(article.get("flags", [])),
            }
            for article in records
        ],
        columns=columns,
    )
    st.dataframe(
        table,
        column_config={
            "url": st.column_config.LinkColumn("URL", display_text="Open source"),
        },
        hide_index=True,
        alt="News articles with source links and summaries",
    )

    st.markdown("#### Download this digest")
    download_columns = st.columns(3)
    downloads_config = (
        ("md", "Digest · Markdown", "text/markdown"),
        ("json", "Data · JSON", "application/json"),
        ("csv", "Data · CSV", "text/csv"),
    )
    for column, (extension, label, mime) in zip(download_columns, downloads_config):
        filename = os.path.basename(f"{st.session_state['base']}.{extension}")
        column.download_button(
            label,
            data=downloads[extension],
            file_name=filename,
            mime=mime,
            width="stretch",
            key=f"download_{extension}",
        )
    with st.expander("JSON preview"):
        st.json(result)


def _render_empty_state():
    st.markdown(
        """
        <section class="empty-state">
          <div class="empty-kicker">A CLEARER READ ON WHAT'S HAPPENING</div>
          <h2>From the open web to a sharper brief.</h2>
          <p>Pick a topic to build a source-linked digest from current reporting.</p>
        </section>
        """,
        unsafe_allow_html=True,
    )
    steps = st.columns(3)
    how_it_works = (
        ("01", "Discover", "Find stories via RSS feeds and news APIs."),
        ("02", "Retrieve", "Fetch readable article text with Python."),
        ("03", "Summarize", "Build grounded summaries from the source text."),
    )
    for column, (number, title, description) in zip(steps, how_it_works):
        with column:
            st.markdown(
                f"""
                <div class="how-card">
                  <span class="how-number">{number}</span>
                  <h3>{title}</h3>
                  <p>{description}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
    st.caption("No LLM is used to find news. Claude is optional and only summarizes retrieved text.")


def _render_result(result):
    articles = result.get("articles", [])
    publisher_count = len(set(article.get("source") for article in articles if article.get("source")))
    funnel = result.get("funnel", {})
    discovered = sum(
        int(source_data.get("counts", {}).get("discovered", 0) or 0)
        for source_data in funnel.values()
    )
    skipped = max(0, discovered - len(articles))
    metrics = st.columns(4)
    values = (
        ("Articles", len(articles)),
        ("Publishers", publisher_count),
        ("Newest article", _newest_age(articles)),
        ("Skipped candidates", skipped),
    )
    for column, (label, value) in zip(metrics, values):
        with column:
            st.metric(label, value, border=True)

    themes = pipeline.top_themes(articles, result.get("topic", ""))
    if themes:
        theme_badges = " ".join(
            f"<span class='theme-chip'>{html.escape(str(theme))}</span>" for theme in themes
        )
        st.markdown(
            f"<div class='themes-row'><span class='themes-label'>RECURRING THEMES</span>"
            f"{theme_badges}</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div class='themes-row'><span class='themes-label'>RECURRING THEMES</span>"
            "<span class='themes-empty'>No recurring themes surfaced in this batch.</span></div>",
            unsafe_allow_html=True,
        )

    if len(articles) < 5:
        reasons = _failure_reasons(result)
        if reasons:
            reason_text = "; ".join(f"{name} ({count})" for name, count in reasons)
        else:
            reason_text = "No specific fetch failure was recorded; available sources may not have returned enough recent, relevant stories."
        st.markdown(
            f"""
            <div class="shortfall-banner">
              <strong>Shortfall — {len(articles)} qualifying {'article' if len(articles) == 1 else 'articles'} found.</strong>
              <span>{html.escape(reason_text)}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div class='success-line'>A complete brief: {len(articles)} qualifying articles from "
            f"{publisher_count} publishers.</div>",
            unsafe_allow_html=True,
        )

    digest_tab, sources_tab, data_tab = st.tabs(("Digest", "Sources", "Data"))
    with digest_tab:
        if articles:
            _render_article_card(articles[0], 1, featured=True)
            for start in range(1, len(articles), 2):
                left, right = st.columns(2)
                with left:
                    _render_article_card(articles[start], start + 1)
                if start + 1 < len(articles):
                    with right:
                        _render_article_card(articles[start + 1], start + 2)
        else:
            st.info("No qualifying articles were returned. Review source health and skipped reasons in Sources.")
    with sources_tab:
        _render_source_tab(result)
    with data_tab:
        _render_data_tab(result, st.session_state["downloads"])


css_path = Path(__file__).with_name("styles.css")
st.markdown(
    f"<style>{css_path.read_text(encoding='utf-8')}</style>",
    unsafe_allow_html=True,
)

st.markdown(
    """
    <header class="briefly-hero">
      <div class="wordmark">Briefly<span class="wordmark-period">.</span></div>
      <p>Real news, fetched by code, summarized from the source.</p>
    </header>
    """,
    unsafe_allow_html=True,
)

topic_column, fetch_column, options_column = st.columns([6, 1.5, 1.2], vertical_alignment="bottom")
with topic_column:
    st.text_input(
        "Topic",
        placeholder="What are you following?",
        key="topic_input",
        label_visibility="collapsed",
    )
with fetch_column:
    fetch_clicked = st.button(
        "Fetch news",
        type="primary",
        width="stretch",
        key="fetch_news",
    )
with options_column:
    options_container = st.popover("Options", width="stretch") if hasattr(st, "popover") else st.expander("Options")
    with options_container:
        article_limit = st.slider("Article limit", 1, 15, 5, key="article_limit")
        recency_days = st.slider("Recency window (days)", 1, 14, 7, key="recency_days")
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        use_claude = st.toggle(
            "Summarize with Claude",
            value=False,
            disabled=not has_key,
            help="Optional. Requires ANTHROPIC_API_KEY and only summarizes retrieved source text.",
            key="use_claude",
        )

if hasattr(st, "pills"):
    st.pills(
        "Suggested topics",
        SUGGESTIONS,
        selection_mode="single",
        key="suggestion",
        on_change=_queue_suggestion,
        label_visibility="collapsed",
        width="stretch",
        wrap=True,
    )
else:
    st.radio(
        "Suggested topics",
        ("Choose a suggested topic",) + SUGGESTIONS,
        horizontal=True,
        key="suggestion",
        on_change=_queue_suggestion,
        label_visibility="collapsed",
    )

fetch_from_suggestion = st.session_state.pop("fetch_from_suggestion", False)
if fetch_clicked or fetch_from_suggestion:
    topic = st.session_state.get("topic_input", "").strip()
    if not topic:
        st.warning("Enter a topic before fetching the news.")
    else:
        started = time.perf_counter()
        status = st.status("Discovering sources", expanded=True)
        with status:
            pipeline_view = st.empty()
            progress_state = {"active_index": 0}
            pipeline_view.markdown(
                _pipeline_markup(progress_state["active_index"], 0, article_limit),
                unsafe_allow_html=True,
            )

            def on_progress(done, total, message):
                nonlocal_active = 1
                message_lower = (message or "").lower()
                if "skip" in message_lower or "off-topic" in message_lower:
                    nonlocal_active = 2
                elif (message or "").lstrip().startswith("["):
                    nonlocal_active = 3
                progress_state["active_index"] = nonlocal_active
                active_name = PIPELINE_STEPS[progress_state["active_index"]]
                status.update(label=f"{active_name} · {done}/{total}", state="running", expanded=True)
                pipeline_view.markdown(
                    _pipeline_markup(progress_state["active_index"], done, total),
                    unsafe_allow_html=True,
                )

            try:
                result = pipeline.run(
                    topic,
                    article_limit,
                    recency_days,
                    use_claude,
                    progress=on_progress,
                )
                progress_state["active_index"] = 4
                pipeline_view.markdown(
                    _pipeline_markup(progress_state["active_index"], result.get("article_count", 0), article_limit),
                    unsafe_allow_html=True,
                )
                status.update(label="Export · writing digest files", state="running", expanded=True)
                base = pipeline.write_outputs(result, "output")
                downloads = {
                    extension: Path(f"{base}.{extension}").read_bytes()
                    for extension in ("md", "json", "csv")
                }
                pipeline_view.markdown(
                    _pipeline_markup(len(PIPELINE_STEPS), result.get("article_count", 0), article_limit),
                    unsafe_allow_html=True,
                )
                elapsed = time.perf_counter() - started
                status.update(label=f"Done in {elapsed:.1f}s", state="complete", expanded=False)
                st.session_state["result"] = result
                st.session_state["base"] = base
                st.session_state["downloads"] = downloads
                st.session_state.pop("run_error", None)
            except Exception as exc:
                status.update(label="News fetch failed", state="error", expanded=True)
                logging.getLogger("newsdigest-ui").error("News run failed: %s", exc)
                st.session_state["run_error"] = str(exc)

if st.session_state.get("run_error"):
    st.error(f"Could not complete the news fetch. {st.session_state['run_error']}")

result = st.session_state.get("result")
if result:
    _render_result(result)
else:
    _render_empty_state()
