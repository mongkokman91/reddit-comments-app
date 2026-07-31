import json
import re
import time
from datetime import date, datetime, time as dt_time, timezone

import gspread
import pandas as pd
import requests
import streamlit as st
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe

st.set_page_config(page_title="Reddit Research Extractor", page_icon="🔎", layout="centered")
st.title("Reddit Research Extractor")
st.caption("Extract subreddit comments or search Reddit by keywords and save the results to one Google Sheet.")

COMMENTS_URL = "https://arctic-shift.photon-reddit.com/api/comments/search"
POSTS_SEARCH_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
POSTS_IDS_URL = "https://arctic-shift.photon-reddit.com/api/posts/ids"


def parse_list(value):
    seen, result = set(), []
    for raw in re.split(r"[\n,]+", value or ""):
        item = raw.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    return result


def parse_subreddits(value):
    return [x.removeprefix("r/") for x in parse_list(value)]


def extract_records(payload, keys):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
        for value in payload.values():
            if isinstance(value, dict):
                for key in keys:
                    if isinstance(value.get(key), list):
                        return value[key]
    raise ValueError("Unrecognized API response: " + json.dumps(payload, ensure_ascii=False)[:500])


def api_get(session, url, params, attempts=8):
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=90)
            if response.status_code == 429:
                time.sleep(min(60, 5 * attempt))
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            if attempt == attempts:
                raise
            time.sleep(min(60, 2 ** attempt))
    raise RuntimeError("Request failed after retries.")


def clean_id(value):
    if value is None:
        return None
    return str(value).removeprefix("t1_").removeprefix("t3_")


def to_unix_start(day):
    return int(datetime.combine(day, dt_time.min, tzinfo=timezone.utc).timestamp())


def to_unix_end(day):
    return int(datetime.combine(day, dt_time.max, tzinfo=timezone.utc).timestamp())


def wildcard_regex(pattern):
    parts = []
    for char in pattern:
        if char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts), re.IGNORECASE | re.DOTALL)


def make_matcher(terms, match_mode, wildcard_logic):
    terms = [x for x in terms if x]
    if not terms:
        raise ValueError("Enter at least one keyword or pattern.")

    if match_mode == "Wildcard patterns":
        compiled = [(term, wildcard_regex(term)) for term in terms]

        def match(text):
            text = text or ""
            hits = [term for term, rx in compiled if rx.search(text)]
            ok = bool(hits) if wildcard_logic == "Any pattern" else len(hits) == len(compiled)
            return ok, hits

        return match

    lowered = [(term, term.lower()) for term in terms]

    def match(text):
        haystack = (text or "").lower()
        hits = [original for original, low in lowered if low in haystack]
        if match_mode == "Any keyword":
            ok = bool(hits)
        elif match_mode == "All keywords":
            ok = len(hits) == len(lowered)
        else:  # Exact phrase: every line is treated as its own exact phrase, using Any logic.
            ok = bool(hits)
        return ok, hits

    return match


def scan_archive(session, url, record_type, subreddits, start_ts, end_ts, scan_limit,
                 result_limit, direction, delay, matcher, status, progress):
    results, seen = [], set()
    cursor = None
    scanned = 0
    previous_boundary = None
    page = 0

    while scanned < scan_limit and len(results) < result_limit:
        params = {
            "sort": "desc" if direction == "Newest" else "asc",
            "limit": "auto",
            "after": start_ts,
            "before": end_ts,
        }
        if subreddits:
            params["subreddit"] = ",".join(subreddits)
        if cursor is not None:
            params["before" if direction == "Newest" else "after"] = cursor

        payload = api_get(session, url, params)
        records = extract_records(payload, ("data", "comments", "posts", "results", "items"))
        valid = [r for r in records if isinstance(r, dict) and r.get("id") and r.get("created_utc") is not None]
        if not valid:
            break

        valid.sort(key=lambda x: int(x["created_utc"]), reverse=(direction == "Newest"))
        page += 1

        for item in valid:
            if scanned >= scan_limit or len(results) >= result_limit:
                break
            rid = clean_id(item.get("id"))
            key = (record_type, rid)
            if key in seen:
                continue
            seen.add(key)
            scanned += 1

            if record_type == "comment":
                searchable = item.get("body") or ""
                title = None
                body = item.get("body")
                post_id = clean_id(item.get("link_id"))
                comment_id = rid
                permalink = f"https://www.reddit.com/comments/{post_id}/_/{comment_id}/" if post_id else None
            else:
                title = item.get("title")
                body = item.get("selftext")
                searchable = f"{title or ''}\n{body or ''}"
                post_id = rid
                comment_id = None
                permalink = f"https://www.reddit.com/comments/{post_id}/"

            ok, hits = matcher(searchable)
            if ok:
                created = int(item["created_utc"])
                results.append({
                    "result_type": record_type,
                    "matched_terms": ", ".join(hits),
                    "subreddit": item.get("subreddit"),
                    "post_id": post_id,
                    "comment_id": comment_id,
                    "title": title,
                    "body": body,
                    "author": item.get("author"),
                    "created_utc": created,
                    "created_iso_utc": datetime.fromtimestamp(created, tz=timezone.utc).isoformat(),
                    "score": item.get("score"),
                    "permalink": permalink,
                })

        timestamps = [int(x["created_utc"]) for x in valid]
        boundary = min(timestamps) if direction == "Newest" else max(timestamps)
        status.info(f"Searching {record_type}s: page {page} — scanned {scanned:,}, found {len(results):,}")
        progress.progress(min(scanned / scan_limit, 1.0))

        if previous_boundary is not None:
            stuck = boundary >= previous_boundary if direction == "Newest" else boundary <= previous_boundary
            if stuck:
                break
        previous_boundary = boundary
        cursor = boundary
        time.sleep(delay)

    return results, scanned


def download_comments(session, subreddit, limit, direction, delay, status, progress):
    by_id, cursor, previous_boundary = {}, None, None
    page = 0
    while len(by_id) < limit:
        params = {
            "subreddit": subreddit,
            "sort": "desc" if direction == "newest" else "asc",
            "limit": "auto",
            "fields": "id,author,author_flair_text,body,created_utc,distinguished,link_id,parent_id,retrieved_on,score,subreddit",
        }
        if cursor is not None:
            params["before" if direction == "newest" else "after"] = cursor
        payload = api_get(session, COMMENTS_URL, params)
        records = extract_records(payload, ("data", "comments", "results", "items"))
        valid = [x for x in records if isinstance(x, dict) and x.get("id") and x.get("created_utc") is not None]
        if not valid:
            break
        page += 1
        valid.sort(key=lambda x: int(x["created_utc"]), reverse=(direction == "newest"))
        for item in valid:
            if len(by_id) >= limit:
                break
            cid = clean_id(item["id"])
            item["id"] = cid
            by_id.setdefault(cid, item)
        timestamps = [int(x["created_utc"]) for x in valid]
        boundary = min(timestamps) if direction == "newest" else max(timestamps)
        status.info(f"r/{subreddit}: page {page} — {len(by_id):,}/{limit:,} comments")
        progress.progress(min(len(by_id) / limit, 1.0))
        if previous_boundary is not None:
            stuck = boundary >= previous_boundary if direction == "newest" else boundary <= previous_boundary
            if stuck:
                break
        previous_boundary, cursor = boundary, boundary
        time.sleep(delay)
    return sorted(by_id.values(), key=lambda x: int(x["created_utc"]), reverse=(direction == "newest"))[:limit]


def comments_dataframe(comments, sort_choice):
    rows = []
    for c in comments:
        cid = clean_id(c.get("id"))
        post_id = clean_id(c.get("link_id"))
        parent = c.get("parent_id")
        created = int(c["created_utc"])
        rows.append({
            "comment_id": cid, "subreddit": c.get("subreddit"), "post_id": post_id,
            "parent_id": clean_id(parent),
            "parent_type": "post" if parent and str(parent).startswith("t3_") else "comment" if parent else None,
            "created_utc": created,
            "created_iso_utc": datetime.fromtimestamp(created, tz=timezone.utc).isoformat(),
            "author": c.get("author"), "body": c.get("body"), "score": c.get("score"),
            "permalink": f"https://www.reddit.com/comments/{post_id}/_/{cid}/" if post_id else None,
        })
    df = pd.DataFrame(rows).drop_duplicates(subset=["comment_id"])
    if df.empty:
        return df
    if sort_choice == "Random":
        return df.sample(frac=1, random_state=42).reset_index(drop=True)
    sort_map = {
        "Newest": (["created_utc"], [False]), "Oldest": (["created_utc"], [True]),
        "Highest score": (["score", "created_utc"], [False, False]),
        "Lowest score": (["score", "created_utc"], [True, False]),
        "Author A–Z": (["author", "created_utc"], [True, False]),
        "Author Z–A": (["author", "created_utc"], [False, False]),
        "Longest comments": (["body", "created_utc"], [False, False]),
        "Shortest comments": (["body", "created_utc"], [True, False]),
        "Subreddit A–Z": (["subreddit", "created_utc"], [True, False]),
    }
    cols, asc = sort_map.get(sort_choice, (["created_utc"], [False]))
    if sort_choice in ("Longest comments", "Shortest comments"):
        df["_length"] = df["body"].fillna("").astype(str).str.len()
        df = df.sort_values(["_length", "created_utc"], ascending=asc, na_position="last").drop(columns="_length")
    else:
        df = df.sort_values(cols, ascending=asc, na_position="last")
    return df.reset_index(drop=True)


def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def posts_dataframe(session, post_ids, delay):
    found = {}
    for batch in chunks(sorted(post_ids), 500):
        payload = api_get(session, POSTS_IDS_URL, {"ids": ",".join(batch), "fields": "id,subreddit,title,selftext,author,created_utc,score,num_comments,url,link_flair_text"})
        for post in extract_records(payload, ("data", "posts", "results", "items")):
            if isinstance(post, dict) and post.get("id"):
                found[clean_id(post["id"])] = post
        time.sleep(delay)
    rows = []
    for post_id in sorted(post_ids):
        post = found.get(post_id, {})
        created = post.get("created_utc")
        rows.append({
            "post_id": post_id, "subreddit": post.get("subreddit"), "title": post.get("title"),
            "post_body": post.get("selftext"), "post_author": post.get("author"),
            "post_created_utc": created,
            "post_created_iso_utc": datetime.fromtimestamp(int(created), tz=timezone.utc).isoformat() if created is not None else None,
            "post_score": post.get("score"), "num_comments": post.get("num_comments"),
            "reddit_permalink": f"https://www.reddit.com/comments/{post_id}/", "archive_post_found": bool(post),
        })
    return pd.DataFrame(rows)


def sheets_client():
    info = dict(st.secrets["gcp_service_account"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    return gspread.authorize(Credentials.from_service_account_info(info, scopes=scopes))


def clean_for_sheets(df):
    return df.replace([float("inf"), float("-inf")], "").where(pd.notna(df), "")


def write_tab(spreadsheet, name, df, append=False):
    clean = clean_for_sheets(df)
    try:
        ws = spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=max(len(clean) + 1, 2), cols=max(len(clean.columns), 1))

    if append and ws.row_count > 1 and ws.get_all_values():
        existing = pd.DataFrame(ws.get_all_records())
        clean = pd.concat([existing, clean], ignore_index=True).drop_duplicates()

    ws.clear()
    ws.resize(rows=max(len(clean) + 1, 2), cols=max(len(clean.columns), 1))
    set_with_dataframe(ws, clean, include_index=False, include_column_header=True, resize=False)
    ws.freeze(rows=1)


mode = st.selectbox("Mode", ["Subreddit extraction", "Keyword search"])

with st.form("research_form"):
    sheet_name = st.text_input("Google Sheet name", "Reddit Comments Research")
    delay = st.number_input("Delay between requests (seconds)", min_value=0.0, max_value=10.0, value=1.0, step=0.5)

    if mode == "Subreddit extraction":
        subreddits_text = st.text_input("Subreddit(s)", "i130suffering")
        comment_limit = st.number_input("Comments per subreddit", 1, 100000, 5000, 100)
        fetch_direction = st.selectbox("Which comments to collect", ["Newest available", "Oldest available"])
        output_sort = st.selectbox("Sort Comments tab by", ["Newest", "Oldest", "Highest score", "Lowest score", "Author A–Z", "Author Z–A", "Longest comments", "Shortest comments", "Subreddit A–Z", "Random"])
    else:
        keywords_text = st.text_area("Keywords or wildcard patterns", help="Enter one per line or separate them with commas.")
        match_mode = st.selectbox("Match mode", ["Any keyword", "All keywords", "Exact phrase", "Wildcard patterns"])
        wildcard_logic = st.selectbox("Wildcard pattern logic", ["Any pattern", "All patterns"], disabled=(match_mode != "Wildcard patterns"))
        search_in = st.selectbox("Search in", ["Posts and comments", "Posts only", "Comments only"])
        search_subreddits_text = st.text_input("Subreddits (optional)", help="Leave blank to search across all indexed Reddit data.")
        col1, col2 = st.columns(2)
        with col1:
            date_from = st.date_input("Date from", value=date(2020, 1, 1))
        with col2:
            date_to = st.date_input("Date to", value=date.today())
        result_limit = st.number_input("Maximum matching results", 1, 100000, 1000, 100)
        scan_limit = st.number_input("Maximum records to scan", 100, 1000000, 50000, 1000, help="All-Reddit searches are filtered locally; a larger scan limit increases coverage.")
        search_direction = st.selectbox("Search order", ["Newest", "Oldest"])
        save_mode = st.selectbox("Save mode", ["Replace previous search results", "Append to existing search results"])

    submitted = st.form_submit_button("Start", type="primary", use_container_width=True)


if submitted:
    try:
        client = sheets_client()
        spreadsheet = client.open(sheet_name)
    except gspread.SpreadsheetNotFound:
        st.error("Create the Google Sheet first and share it with the service-account email as Editor, then use that exact sheet name.")
        st.stop()
    except Exception as exc:
        st.error("Google Sheets credentials or access could not be verified.")
        st.exception(exc)
        st.stop()

    session = requests.Session()
    session.headers.update({"User-Agent": "Private Streamlit Reddit research extractor/2.0"})
    status, progress = st.empty(), st.progress(0.0)

    try:
        if mode == "Subreddit extraction":
            subreddits = parse_subreddits(subreddits_text)
            if not subreddits:
                raise ValueError("Enter at least one subreddit.")
            all_comments = []
            for subreddit in subreddits:
                all_comments.extend(download_comments(session, subreddit, int(comment_limit), "newest" if fetch_direction == "Newest available" else "oldest", float(delay), status, progress))
            unique = {clean_id(x["id"]): x for x in all_comments}
            comments_df = comments_dataframe(list(unique.values()), output_sort)
            post_ids = {str(x) for x in comments_df.get("post_id", pd.Series(dtype=str)).dropna().unique() if str(x).strip()}
            status.info("Retrieving related posts...")
            posts_df = posts_dataframe(session, post_ids, float(delay))
            write_tab(spreadsheet, "Comments", comments_df)
            write_tab(spreadsheet, "Posts", posts_df)
            progress.progress(1.0)
            status.success(f"Finished: {len(comments_df):,} comments and {len(posts_df):,} posts.")
        else:
            if date_from > date_to:
                raise ValueError("Date from cannot be later than Date to.")
            terms = parse_list(keywords_text)
            matcher = make_matcher(terms, match_mode, wildcard_logic)
            subreddits = parse_subreddits(search_subreddits_text)
            start_ts, end_ts = to_unix_start(date_from), to_unix_end(date_to)
            remaining, scanned_total, all_results = int(result_limit), 0, []
            targets = []
            if search_in in ("Posts and comments", "Posts only"):
                targets.append((POSTS_SEARCH_URL, "post"))
            if search_in in ("Posts and comments", "Comments only"):
                targets.append((COMMENTS_URL, "comment"))

            for url, record_type in targets:
                if remaining <= 0:
                    break
                results, scanned = scan_archive(session, url, record_type, subreddits, start_ts, end_ts, max(1, int(scan_limit) - scanned_total), remaining, search_direction, float(delay), matcher, status, progress)
                all_results.extend(results)
                scanned_total += scanned
                remaining = int(result_limit) - len(all_results)

            results_df = pd.DataFrame(all_results)
            if not results_df.empty:
                results_df = results_df.sort_values("created_utc", ascending=(search_direction == "Oldest")).reset_index(drop=True)
            log_df = pd.DataFrame([{
                "run_time_utc": datetime.now(timezone.utc).isoformat(),
                "keywords_or_patterns": " | ".join(terms), "match_mode": match_mode,
                "wildcard_logic": wildcard_logic if match_mode == "Wildcard patterns" else "",
                "search_in": search_in, "subreddits": ", ".join(subreddits) if subreddits else "ALL REDDIT",
                "date_from": str(date_from), "date_to": str(date_to), "scan_limit": int(scan_limit),
                "records_scanned": scanned_total, "results_found": len(results_df), "save_mode": save_mode,
            }])
            append = save_mode == "Append to existing search results"
            write_tab(spreadsheet, "Search Results", results_df, append=append)
            write_tab(spreadsheet, "Search Log", log_df, append=True)
            progress.progress(1.0)
            status.success(f"Finished: scanned {scanned_total:,} records and found {len(results_df):,} matches.")

        st.link_button("Open Google Sheet", spreadsheet.url, use_container_width=True)

    except Exception as exc:
        status.error("The extraction stopped because of an error.")
        st.exception(exc)
