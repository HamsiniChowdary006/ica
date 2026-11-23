"""Lightweight web UI for the news digest pipeline.

Run with:  streamlit run app.py
"""
import logging
import os

import streamlit as st

import main as pipeline

logging.getLogger("httpx").setLevel(logging.WARNING)

st.set_page_config(page_title="News Digest", page_icon="📰", layout="wide")
st.title("📰 Automated News Digest")
st.caption("Discovers recent news via RSS feeds, fetches the articles with Python, and writes "
           "grounded, source-linked summaries. No LLM is used for discovery or retrieval.")

with st.sidebar:
    st.header("Settings")
    topic = st.text_input("Topic", placeholder="e.g. cybersecurity, electric vehicles")
    limit = st.slider("Max articles", 1, 15, 5)
    days = st.slider("Recency window (days)", 1, 14, 7)
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    use_llm = st.checkbox("Summarize with Claude (optional)", value=False, disabled=not has_key,
                          help="Requires ANTHROPIC_API_KEY in your environment or .env. "
                               "Default summaries are extractive and need no key.")
    run_clicked = st.button("Fetch news", type="primary", use_container_width=True)

if run_clicked:
    if not topic.strip():
        st.warning("Please enter a topic.")
    else:
        bar = st.progress(0.0, text="Discovering sources...")

        def on_progress(done, total, msg):
            bar.progress(min(done / total, 1.0), text=msg[:90])

        with st.spinner("Fetching and summarizing..."):
            result = pipeline.run(topic.strip(), limit, days, use_llm, progress=on_progress)
            base = pipeline.write_outputs(result, "output")
        bar.empty()
        st.session_state["result"] = result
        st.session_state["base"] = base

result = st.session_state.get("result")
if result:
    c1, c2, c3 = st.columns(3)
    c1.metric("Articles", result["article_count"])
    c2.metric("Publishers", len(result["publishers"]))
    c3.metric("Blocked / failed pages", len(result.get("fetch_failures", [])))

    for note in result["notes"]:
        st.warning(note)

    base = st.session_state["base"]
    d1, d2, d3 = st.columns(3)
    for col, ext, mime, label in ((d1, "md", "text/markdown", "Digest (.md)"),
                                  (d2, "json", "application/json", "Data (.json)"),
                                  (d3, "csv", "text/csv", "Data (.csv)")):
        with open(f"{base}.{ext}", "rb") as f:
            col.download_button(label, f.read(), file_name=os.path.basename(f"{base}.{ext}"),
                                mime=mime, use_container_width=True)

    st.divider()
    for i, a in enumerate(result["articles"], 1):
        with st.container(border=True):
            st.subheader(f"{i}. {a['title']}")
            st.caption(f"**{a['source']}** · {a['published_at'] or 'date unknown'} · "
                       f"summary: {a['summary_method']}")
            st.write(a["summary"] or "_Summary unavailable._")
            st.markdown(f"[Read original article ↗]({a['url']})")
            if a["flags"]:
                st.caption("⚠️ " + "; ".join(a["flags"]))
            with st.expander("Extracted text"):
                st.write(a["excerpt"])

    with st.expander("Source status"):
        for name, s in result["source_status"].items():
            st.write(f"{'✅' if s['ok'] else '❌'} **{name}** — "
                     f"{s.get('candidates', s.get('error', ''))}")
else:
    st.info("Enter a topic in the sidebar and click **Fetch news**.")
