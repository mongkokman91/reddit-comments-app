import re
import queue
import threading
from urllib.parse import urlparse, parse_qs

import streamlit as st
from yt_dlp import YoutubeDL

st.set_page_config(page_title="YouTube Comments", page_icon="💬", layout="wide")
st.title("YouTube Comments")
st.caption("Read channel and search-result comments here. Results stay in this browser session; no comment files or videos are downloaded.")


class QuietLog:
    def __init__(self):
        self.warnings = []

    def debug(self, message):
        pass

    def warning(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def error(self, message):
        self.warning(message)


def options(log):
    return dict(quiet=True, no_warnings=True, logger=log, skip_download=True,
                cachedir=False, socket_timeout=25, retries=2, extractor_retries=2,
                ignore_no_formats_error=True)


def sources(value, mode, shorts):
    value = value.strip()
    if not value:
        raise ValueError("Enter a channel, search phrase, or YouTube URL.")
    if mode == "Search" and not value.startswith(("https://", "http://")):
        return ["ytsearch:" + value]
    if value.startswith("@"):
        value = "https://www.youtube.com/" + value
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        raise ValueError("Use an HTTPS YouTube URL, a channel @handle, or choose Search for a phrase.")
    if mode == "Channel":
        base = value.split("?")[0].rstrip("/")
        base = re.sub(r"/(videos|shorts|streams|featured)$", "", base)
        if not re.search(r"youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)$", base):
            raise ValueError("Enter a channel URL or @handle.")
        return [base + "/videos"] + ([base + "/shorts"] if shorts else [])
    return [value]


def discover(value, mode, shorts, limit, log):
    videos = {}
    for source in sources(value, mode, shorts):
        if source.startswith("ytsearch:"):
            source = f"ytsearch{limit}:" + source.split(":", 1)[1]
        opts = options(log)
        opts.update(extract_flat=True, playlistend=limit, lazy_playlist=True)
        with YoutubeDL(opts) as ydl:
            result = ydl.extract_info(source, download=False)
        for item in ((result or {}).get("entries") or [result]):
            if not item:
                continue
            video_id = item.get("id", "")
            if re.fullmatch(r"[\w-]{11}", video_id):
                videos.setdefault(video_id, {"id": video_id, "title": item.get("title") or video_id})
    return list(videos.values())


def fetch_comments(video, replies, parents, order, log):
    opts = options(log)
    opts.update(getcomments=True, noplaylist=True,
                extractor_args={"youtube": {"comment_sort": [order],
                    "max_comments": [f"all,{parents or 'all'},all,{replies},2"],
                    "skip": ["hls", "dash"]}})
    with YoutubeDL(opts) as ydl:
        result = ydl.extract_info("https://www.youtube.com/watch?v=" + video["id"], download=False)
    if not result:
        raise RuntimeError("YouTube returned no video information.")
    return result.get("comments") or []


def fetch_with_timeout(video, replies, parents, order, log):
    result_queue = queue.Queue(maxsize=1)
    def worker():
        try:
            result_queue.put((True, fetch_comments(video, replies, parents, order, log)))
        except Exception as exc:
            result_queue.put((False, exc))
    threading.Thread(target=worker, daemon=True).start()
    try:
        ok, result = result_queue.get(timeout=90)
    except queue.Empty:
        raise RuntimeError("YouTube did not return comments within 90 seconds. The hosted server may be blocked. Try again later or try a different video.")
    if not ok:
        raise result
    return result


for key, default in {"yt_videos": [], "yt_comments": {}, "yt_errors": {}, "yt_done": [], "yt_job": None}.items():
    st.session_state.setdefault(key, default)

with st.form("youtube_job"):
    mode = st.radio("Source", ["Channel", "Search", "Video / search URL"], horizontal=True)
    value = st.text_input("Channel @handle, YouTube URL, or search phrase", placeholder="@mattpalmer")
    a, b, c = st.columns(3)
    limit = a.number_input("Videos per listing", 1, 200, 20)
    replies = b.number_input("Replies per comment", 0, 100, 10)
    parents = c.number_input("Parent comments per video (0 = all accessible)", 0, 100000, 0)
    shorts = st.checkbox("Include channel Shorts", value=True)
    order_label = st.selectbox("Comment order", ["Top comments", "Newest first"])
    st.caption("Channel capture checks Videos and optionally Shorts. Search uses the first N results; paste a search URL to retain its filters. This is a bounded listing, not a snapshot of another open YouTube tab.")
    start = st.form_submit_button("Load comments", type="primary")

if start:
    log = QuietLog()
    try:
        with st.spinner("Finding videos…"):
            videos = discover(value, mode, shorts, int(limit), log)
        if not videos:
            st.error("No videos found. Try a video URL or another search.")
        else:
            st.session_state.update(yt_videos=videos, yt_comments={}, yt_errors={}, yt_done=[],
                                    yt_job=(int(replies), int(parents), "top" if order_label == "Top comments" else "new"))
            st.session_state["yt_run"] = True
    except Exception as exc:
        st.error(str(exc))

if st.session_state.yt_errors and st.button("Retry failed videos"):
    st.session_state.yt_done = [key for key in st.session_state.yt_done if key not in st.session_state.yt_errors]
    st.session_state["yt_run"] = True

if st.session_state.pop("yt_run", False):
    progress = st.progress(0)
    status = st.empty()
    videos = st.session_state.yt_videos
    for index, video in enumerate(videos):
        if video["id"] not in st.session_state.yt_done:
            status.info(f"Reading {index + 1}/{len(videos)}: {video['title']}")
            log = QuietLog()
            try:
                comments = fetch_with_timeout(video, *st.session_state.yt_job, log)
                seen = set()
                st.session_state.yt_comments[video["id"]] = [item for item in comments if item.get("id") and not (item["id"] in seen or seen.add(item["id"]))]
                st.session_state.yt_done.append(video["id"])
                st.session_state.yt_errors.pop(video["id"], None)
                if log.warnings:
                    st.session_state.yt_errors[video["id"]] = "Extraction warning: " + " | ".join(log.warnings)
            except Exception as exc:
                st.session_state.yt_errors[video["id"]] = str(exc)
        progress.progress((index + 1) / len(videos))
    status.success("Finished reading available comments.")

videos = st.session_state.yt_videos
if videos:
    total = sum(len(items) for items in st.session_state.yt_comments.values())
    st.write(f"{total:,} comments and replies across {len(st.session_state.yt_done)} processed videos.")
    if st.button("Clear session results"):
        for key in list(st.session_state):
            if key.startswith("yt_"):
                del st.session_state[key]
        st.rerun()
    labels = {v["id"]: v["title"] for v in videos}
    selected = st.selectbox("Video", list(labels), format_func=lambda key: labels[key])
    st.link_button("Open video on YouTube", "https://www.youtube.com/watch?v=" + selected)
    if selected in st.session_state.yt_errors:
        st.warning(st.session_state.yt_errors[selected])
    query = st.text_input("Filter comment text or author")
    comments = st.session_state.yt_comments.get(selected, [])
    parents_by_id = {c["id"]: c for c in comments if c.get("parent") in (None, "root")}
    reply_map = {}
    for comment in comments:
        if comment.get("parent") not in (None, "root"):
            reply_map.setdefault(comment["parent"], []).append(comment)
    threads = list(parents_by_id.values())
    if query:
        def matches(comment):
            return query.casefold() in (str(comment.get("text", "")) + " " + str(comment.get("author", ""))).casefold()
        threads = [c for c in threads if matches(c) or any(matches(r) for r in reply_map.get(c["id"], []))]
    pages = max(1, (len(threads) + 24) // 25)
    page = st.number_input("Page", 1, pages, 1, key=f"yt_page_{selected}_{query}")
    st.caption(f"{len(threads):,} matching threads · 25 per page")
    for comment in threads[(page - 1) * 25:page * 25]:
        with st.container(border=True):
            st.caption(f"{comment.get('author') or 'Unknown author'} · {comment.get('like_count') or 0:,} likes")
            st.text(comment.get("text") or "")
            thread_replies = reply_map.get(comment["id"], [])
            if thread_replies:
                with st.expander(f"{len(thread_replies)} replies"):
                    for reply in thread_replies:
                        st.caption(f"{reply.get('author') or 'Unknown author'} · {reply.get('like_count') or 0:,} likes")
                        st.text(reply.get("text") or "")
    if not comments:
        st.info("No comments were returned for this video. Comments may be disabled, unavailable, or blocked by YouTube.")
