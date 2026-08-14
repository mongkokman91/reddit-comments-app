import re
import time
from datetime import date, datetime, time as dt_time, timezone

import gspread
import pandas as pd
import requests
import streamlit as st
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe
from streamlit_searchbox import st_searchbox


st.set_page_config(
    page_title="Reddit Research Extractor",
    page_icon="🔎",
)

st.title("Reddit Research Extractor")
st.caption(
    "Extract subreddit comments or search Reddit by keywords "
    "and save results to one Google Sheet."
)

ARCTIC_COMMENTS_URL = (
    "https://arctic-shift.photon-reddit.com/api/comments/search"
)

ARCTIC_POSTS_BY_ID_URL = (
    "https://arctic-shift.photon-reddit.com/api/posts/ids"
)

PULLPUSH_COMMENTS_URL = (
    "https://api.pullpush.io/reddit/search/comment/"
)

PULLPUSH_POSTS_URL = (
    "https://api.pullpush.io/reddit/search/submission/"
)

REDDIT_SUBREDDIT_AUTOCOMPLETE_URL = (
    "https://www.reddit.com/api/subreddit_autocomplete_v2.json"
)

REDDIT_SUBREDDIT_SEARCH_URL = (
    "https://www.reddit.com/subreddits/search.json"
)

REDDIT_SUBREDDIT_NAMES_URL = (
    "https://www.reddit.com/api/search_reddit_names.json"
)


def _subreddit_suggestion_rows(children):
    suggestions = []
    seen = set()

    for child in children:
        data = child.get("data", {}) if isinstance(child, dict) else {}
        name = str(data.get("display_name") or "").strip()

        if not name or name.lower() in seen:
            continue

        seen.add(name.lower())
        subscribers = data.get("subscribers")
        title = str(data.get("title") or "").strip()

        label = f"r/{name}"
        if subscribers is not None:
            try:
                label += f" · {int(subscribers):,} members"
            except (TypeError, ValueError):
                pass
        if title:
            label += f" · {title}"

        suggestions.append((label, name))

    return suggestions[:20]


@st.cache_data(ttl=300, show_spinner=False)
def live_subreddit_suggestions(searchterm):
    query = (searchterm or "").strip().removeprefix("r/")

    if len(query) < 2:
        return []

    headers = {
        "User-Agent": (
            "Private Streamlit Reddit "
            "research extractor/2.2"
        ),
        "Accept": "application/json",
    }
    errors = []

    try:
        response = requests.get(
            REDDIT_SUBREDDIT_AUTOCOMPLETE_URL,
            params={
                "query": query,
                "include_over_18": "on",
                "include_profiles": "off",
                "raw_json": 1,
            },
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        suggestions = _subreddit_suggestion_rows(
            payload.get("data", {}).get("children", [])
        )
        if suggestions:
            return suggestions
        errors.append("autocomplete returned 0")
    except Exception as exc:
        errors.append(
            f"autocomplete {type(exc).__name__}"
        )

    try:
        response = requests.get(
            REDDIT_SUBREDDIT_SEARCH_URL,
            params={
                "q": query,
                "limit": 20,
                "raw_json": 1,
            },
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        suggestions = _subreddit_suggestion_rows(
            payload.get("data", {}).get("children", [])
        )
        if suggestions:
            return suggestions
        errors.append("search returned 0")
    except Exception as exc:
        errors.append(
            f"search {type(exc).__name__}"
        )

    try:
        response = requests.post(
            REDDIT_SUBREDDIT_NAMES_URL,
            data={
                "query": query,
                "exact": "false",
                "include_over_18": "on",
            },
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        names = (
            payload.get("names", [])
            if isinstance(payload, dict)
            else []
        )
        suggestions = [
            (f"r/{name}", name)
            for name in names
            if str(name).strip()
        ][:20]
        if suggestions:
            return suggestions
        errors.append("name search returned 0")
    except Exception as exc:
        errors.append(
            f"name search {type(exc).__name__}"
        )

    diagnostic = "; ".join(errors)
    return [(
        f"No live matches for '{query}' ({diagnostic})",
        "",
    )]


def parse_csv(value):
    seen = set()
    result = []

    for item in value.split(","):
        item = item.strip()
        key = item.lower()

        if item and key not in seen:
            seen.add(key)
            result.append(item)

    return result


def parse_subreddits(value):
    return [
        item.removeprefix("r/")
        for item in parse_csv(value)
    ]


def request_json(
    session,
    url,
    params,
    attempts=6,
):
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            response = session.get(
                url,
                params=params,
                timeout=90,
            )

            if response.status_code == 429:
                time.sleep(
                    min(60, attempt * 5)
                )
                continue

            response.raise_for_status()
            return response.json()

        except requests.RequestException as exc:
            last_error = exc

            if attempt == attempts:
                break

            time.sleep(
                min(30, 2 ** attempt)
            )

    raise RuntimeError(
        f"API request failed: {last_error}"
    )


def extract_records(payload):
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in (
            "data",
            "comments",
            "posts",
            "results",
            "items",
        ):
            value = payload.get(key)

            if isinstance(value, list):
                return value

    return []


def sheets_client():
    info = dict(
        st.secrets["gcp_service_account"]
    )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = (
        Credentials.from_service_account_info(
            info,
            scopes=scopes,
        )
    )

    return gspread.authorize(credentials)


def clean_for_sheets(df):
    clean = df.copy()

    clean = clean.replace(
        [
            float("inf"),
            float("-inf"),
        ],
        "",
    )

    return clean.where(
        pd.notna(clean),
        "",
    )


def write_tab(
    spreadsheet,
    name,
    df,
    append=False,
    dedupe_columns=None,
):
    clean = clean_for_sheets(df)

    try:
        worksheet = spreadsheet.worksheet(
            name
        )

    except gspread.WorksheetNotFound:
        worksheet = (
            spreadsheet.add_worksheet(
                title=name,
                rows=max(
                    len(clean) + 1,
                    2,
                ),
                cols=max(
                    len(clean.columns),
                    1,
                ),
            )
        )

    if (
        append
        and worksheet.get_all_values()
    ):
        existing = pd.DataFrame(
            worksheet.get_all_records()
        )

        combined = pd.concat(
            [
                existing,
                clean,
            ],
            ignore_index=True,
        )

        if dedupe_columns:
            available = [
                column
                for column in dedupe_columns
                if column in combined.columns
            ]

            if available:
                combined = (
                    combined.drop_duplicates(
                        subset=available,
                        keep="last",
                    )
                )

        clean = combined

    worksheet.clear()

    worksheet.resize(
        rows=max(
            len(clean) + 1,
            2,
        ),
        cols=max(
            len(clean.columns),
            1,
        ),
    )

    set_with_dataframe(
        worksheet,
        clean,
        include_index=False,
        include_column_header=True,
        resize=False,
    )

    worksheet.freeze(rows=1)


def unix_start(selected_date):
    return int(
        datetime.combine(
            selected_date,
            dt_time.min,
            tzinfo=timezone.utc,
        ).timestamp()
    )


def unix_end(selected_date):
    return int(
        datetime.combine(
            selected_date,
            dt_time.max,
            tzinfo=timezone.utc,
        ).timestamp()
    )


def wildcard_to_regex(pattern):
    escaped = re.escape(pattern)

    escaped = escaped.replace(
        r"\*",
        ".*",
    ).replace(
        r"\?",
        ".",
    )

    return re.compile(
        escaped,
        re.IGNORECASE | re.DOTALL,
    )


def matched_patterns(
    text,
    patterns,
    match_mode,
    wildcard_logic,
):
    text = text or ""

    if match_mode == "Exact phrase":
        phrase = (
            patterns[0]
            if patterns
            else ""
        )

        return (
            [phrase]
            if phrase.lower()
            in text.lower()
            else []
        )

    if match_mode == "Any keyword":
        return [
            pattern
            for pattern in patterns
            if pattern.lower()
            in text.lower()
        ]

    if match_mode == "All keywords":
        hits = [
            pattern
            for pattern in patterns
            if pattern.lower()
            in text.lower()
        ]

        return (
            hits
            if len(hits)
            == len(patterns)
            else []
        )

    hits = [
        pattern
        for pattern in patterns
        if wildcard_to_regex(
            pattern
        ).search(text)
    ]

    if (
        wildcard_logic
        == "All patterns"
        and len(hits)
        != len(patterns)
    ):
        return []

    return hits


def pullpush_search(
    session,
    endpoint,
    query,
    subreddit,
    after,
    before,
    order,
    candidate_limit,
    status,
):
    collected = []
    seen = set()

    cursor_before = before
    previous_cursor = None

    while len(collected) < candidate_limit:
        size = min(
            100,
            candidate_limit
            - len(collected),
        )

        params = {
            "q": query,
            "after": after,
            "before": cursor_before,
            "size": size,
            "sort": (
                "desc"
                if order
                == "Newest first"
                else "asc"
            ),
            "sort_type": "created_utc",
        }

        if subreddit:
            params["subreddit"] = subreddit

        payload = request_json(
            session,
            endpoint,
            params,
        )

        batch = extract_records(
            payload
        )

        if not batch:
            break

        fresh = []

        for item in batch:
            item_id = str(
                item.get("id", "")
            )

            if (
                item_id
                and item_id not in seen
            ):
                seen.add(item_id)
                fresh.append(item)

        if not fresh:
            status.warning(
                "Stopped safely because "
                "the archive returned no "
                "new records."
            )
            break

        collected.extend(fresh)

        status.info(
            f"Retrieved "
            f"{len(collected):,}/"
            f"{candidate_limit:,} "
            f"server candidates"
        )

        timestamps = [
            int(
                item.get(
                    "created_utc",
                    0,
                )
            )
            for item in fresh
            if item.get("created_utc")
        ]

        if not timestamps:
            break

        if order == "Newest first":
            next_cursor = (
                min(timestamps) - 1
            )

            if (
                previous_cursor
                is not None
                and next_cursor
                >= previous_cursor
            ):
                status.warning(
                    "Stopped safely because "
                    "pagination did not advance."
                )
                break

            previous_cursor = (
                next_cursor
            )

            cursor_before = (
                next_cursor
            )

        else:
            status.warning(
                "Oldest-first server "
                "pagination is limited. "
                "Results were saved from "
                "the records returned."
            )
            break

        time.sleep(0.5)

    return collected[
        :candidate_limit
    ]


def normalize_search_results(
    items,
    result_type,
    patterns,
    match_mode,
    wildcard_logic,
):
    rows = []

    for item in items:
        if result_type == "post":
            title = (
                item.get("title")
                or ""
            )

            body = (
                item.get("selftext")
                or ""
            )

            text = (
                f"{title}\n{body}"
            )

            post_id = str(
                item.get("id", "")
            ).removeprefix("t3_")

            comment_id = None

            permalink = (
                item.get("permalink")
                or (
                    f"/comments/"
                    f"{post_id}/"
                )
            )

        else:
            title = ""

            body = (
                item.get("body")
                or ""
            )

            text = body

            post_id = str(
                item.get(
                    "link_id",
                    "",
                )
            ).removeprefix("t3_")

            comment_id = str(
                item.get("id", "")
            ).removeprefix("t1_")

            permalink = (
                item.get("permalink")
                or (
                    f"/comments/"
                    f"{post_id}/_/"
                    f"{comment_id}/"
                )
            )

        hits = matched_patterns(
            text,
            patterns,
            match_mode,
            wildcard_logic,
        )

        if not hits:
            continue

        created = item.get(
            "created_utc"
        )

        rows.append({
            "result_type": result_type,
            "matched_patterns": (
                ", ".join(hits)
            ),
            "subreddit": item.get(
                "subreddit"
            ),
            "post_id": post_id,
            "comment_id": comment_id,
            "title": title,
            "body": body,
            "author": item.get(
                "author"
            ),
            "created_utc": created,
            "created_iso_utc": (
                datetime.fromtimestamp(
                    int(created),
                    tz=timezone.utc,
                ).isoformat()
                if created
                else None
            ),
            "score": item.get(
                "score"
            ),
            "permalink": (
                "https://www.reddit.com"
                + permalink
                if str(
                    permalink
                ).startswith("/")
                else permalink
            ),
        })

    return rows


def download_subreddit_comments(
    session,
    subreddit,
    limit,
    direction,
    delay,
    status,
    progress,
):
    by_id = {}

    cursor = None
    previous_boundary = None

    while len(by_id) < limit:
        params = {
            "subreddit": subreddit,
            "sort": (
                "desc"
                if direction == "newest"
                else "asc"
            ),
            "limit": "auto",
            "fields": (
                "id,author,body,"
                "created_utc,link_id,"
                "parent_id,score,"
                "subreddit"
            ),
        }

        if cursor is not None:
            params[
                "before"
                if direction == "newest"
                else "after"
            ] = cursor

        payload = request_json(
            session,
            ARCTIC_COMMENTS_URL,
            params,
        )

        batch = [
            item
            for item in extract_records(
                payload
            )
            if item.get("id")
            and item.get(
                "created_utc"
            )
        ]

        if not batch:
            break

        batch.sort(
            key=lambda item: int(
                item["created_utc"]
            ),
            reverse=(
                direction == "newest"
            ),
        )

        for item in batch:
            comment_id = str(
                item["id"]
            ).removeprefix("t1_")

            item["id"] = comment_id

            by_id.setdefault(
                comment_id,
                item,
            )

            if len(by_id) >= limit:
                break

        timestamps = [
            int(
                item["created_utc"]
            )
            for item in batch
        ]

        boundary = (
            min(timestamps)
            if direction == "newest"
            else max(timestamps)
        )

        status.info(
            f"r/{subreddit}: "
            f"{len(by_id):,}/"
            f"{limit:,} comments"
        )

        progress.progress(
            min(
                len(by_id) / limit,
                1.0,
            )
        )

        if (
            previous_boundary
            is not None
        ):
            stuck = (
                boundary
                >= previous_boundary
                if direction == "newest"
                else boundary
                <= previous_boundary
            )

            if stuck:
                break

        previous_boundary = boundary
        cursor = boundary

        time.sleep(delay)

    return list(
        by_id.values()
    )[:limit]


def comments_dataframe(
    comments,
    sort_choice,
):
    rows = []

    for comment in comments:
        comment_id = str(
            comment.get("id", "")
        ).removeprefix("t1_")

        post_id = str(
            comment.get(
                "link_id",
                "",
            )
        ).removeprefix("t3_")

        parent = str(
            comment.get(
                "parent_id",
                "",
            )
        )

        created = int(
            comment.get(
                "created_utc",
                0,
            )
        )

        rows.append({
            "comment_id": comment_id,
            "subreddit": comment.get(
                "subreddit"
            ),
            "post_id": post_id,
            "parent_id": (
                parent
                .removeprefix("t1_")
                .removeprefix("t3_")
            ),
            "parent_type": (
                "post"
                if parent.startswith(
                    "t3_"
                )
                else "comment"
            ),
            "created_utc": created,
            "created_iso_utc": (
                datetime.fromtimestamp(
                    created,
                    tz=timezone.utc,
                ).isoformat()
            ),
            "author": comment.get(
                "author"
            ),
            "body": comment.get(
                "body"
            ),
            "score": comment.get(
                "score"
            ),
            "permalink": (
                "https://www.reddit.com/"
                f"comments/{post_id}/_/"
                f"{comment_id}/"
            ),
        })

    df = pd.DataFrame(rows)

    if df.empty:
        return df

    df = df.drop_duplicates(
        subset=["comment_id"]
    )

    if sort_choice == "Newest":
        return df.sort_values(
            "created_utc",
            ascending=False,
        )

    if sort_choice == "Oldest":
        return df.sort_values(
            "created_utc",
            ascending=True,
        )

    if (
        sort_choice
        == "Highest score"
    ):
        return df.sort_values(
            "score",
            ascending=False,
            na_position="last",
        )

    if (
        sort_choice
        == "Lowest score"
    ):
        return df.sort_values(
            "score",
            ascending=True,
            na_position="last",
        )

    if sort_choice == "Random":
        return df.sample(
            frac=1,
            random_state=42,
        )

    return df


def posts_dataframe(
    session,
    post_ids,
):
    columns = [
        "post_id",
        "subreddit",
        "title",
        "post_body",
        "post_author",
        "post_created_utc",
        "post_created_iso_utc",
        "post_score",
        "num_comments",
        "reddit_permalink",
    ]

    if not post_ids:
        return pd.DataFrame(
            columns=columns
        )

    normalized_ids = sorted({
        str(
            post_id
        ).removeprefix("t3_")
        for post_id in post_ids
        if str(post_id).strip()
    })

    found = {}

    batch_size = 75

    for start in range(
        0,
        len(normalized_ids),
        batch_size,
    ):
        batch = normalized_ids[
            start:start + batch_size
        ]

        payload = request_json(
            session,
            ARCTIC_POSTS_BY_ID_URL,
            {
                "ids": ",".join(
                    batch
                ),
                "fields": (
                    "id,subreddit,title,"
                    "selftext,author,"
                    "created_utc,score,"
                    "num_comments"
                ),
            },
        )

        for post in extract_records(
            payload
        ):
            post_id = str(
                post.get("id", "")
            ).removeprefix("t3_")

            if post_id:
                found[post_id] = post

        time.sleep(0.25)

    rows = []

    for post_id in normalized_ids:
        post = found.get(
            post_id,
            {},
        )

        created = post.get(
            "created_utc"
        )

        rows.append({
            "post_id": post_id,
            "subreddit": post.get(
                "subreddit"
            ),
            "title": post.get(
                "title"
            ),
            "post_body": post.get(
                "selftext"
            ),
            "post_author": post.get(
                "author"
            ),
            "post_created_utc": (
                created
            ),
            "post_created_iso_utc": (
                datetime.fromtimestamp(
                    int(created),
                    tz=timezone.utc,
                ).isoformat()
                if created
                else None
            ),
            "post_score": post.get(
                "score"
            ),
            "num_comments": post.get(
                "num_comments"
            ),
            "reddit_permalink": (
                "https://www.reddit.com/"
                f"comments/{post_id}/"
            ),
        })

    return pd.DataFrame(
        rows,
        columns=columns,
    )


mode = st.selectbox(
    "Mode",
    [
        "Subreddit extraction",
        "Keyword search",
    ],
)

sheet_name = st.text_input(
    "Google Sheet name",
    value="Reddit Comments Research",
)

delay = st.number_input(
    "Delay between requests (seconds)",
    min_value=0.0,
    max_value=10.0,
    value=0.0,
    step=0.5,
)


if mode == "Subreddit extraction":
    if "selected_subreddits" not in st.session_state:
        st.session_state.selected_subreddits = ["i130suffering"]

    selected_subreddit = st_searchbox(
        live_subreddit_suggestions,
        label="Find subreddit",
        placeholder="Start typing a subreddit name...",
        key="live_subreddit_search",
        debounce=300,
        clear_on_submit=True,
    )

    if (
        selected_subreddit
        and selected_subreddit
        not in st.session_state.selected_subreddits
    ):
        st.session_state.selected_subreddits.append(
            selected_subreddit
        )

    selected_subreddits = st.multiselect(
        "Subreddit(s) to scrape",
        options=st.session_state.selected_subreddits,
        default=st.session_state.selected_subreddits,
        help=(
            "Type above to get live Reddit suggestions. "
            "Remove a selected subreddit here if needed."
        ),
    )
    st.session_state.selected_subreddits = selected_subreddits

    manual_subreddits_text = st.text_input(
        "Manual subreddit(s) fallback (optional)",
        help=(
            "Comma-separated subreddit names. Use this only if "
            "live suggestions are unavailable or a subreddit is missing."
        ),
    )

    with st.form(
        "subreddit_form"
    ):
        comment_limit = (
            st.number_input(
                "Comments per subreddit",
                min_value=1,
                max_value=100000,
                value=1000,
                step=100,
            )
        )

        fetch_direction = (
            st.selectbox(
                "Which comments to collect",
                [
                    "Newest available",
                    "Oldest available",
                ],
            )
        )

        output_sort = st.selectbox(
            "Sort Comments tab by",
            [
                "Newest",
                "Oldest",
                "Highest score",
                "Lowest score",
                "Random",
            ],
            index=2,
        )

        submitted = (
            st.form_submit_button(
                "Start extraction",
                type="primary",
                use_container_width=True,
            )
        )

    if submitted:
        manual_subreddits = parse_subreddits(
            manual_subreddits_text
        )
        subreddits = []
        seen_subreddits = set()

        for subreddit in (
            selected_subreddits
            + manual_subreddits
        ):
            clean_name = str(subreddit).strip().removeprefix("r/")
            key = clean_name.lower()
            if clean_name and key not in seen_subreddits:
                seen_subreddits.add(key)
                subreddits.append(clean_name)

        if not subreddits:
            st.error(
                "Enter at least "
                "one subreddit."
            )
            st.stop()

        client = sheets_client()

        try:
            spreadsheet = client.open(
                sheet_name
            )

        except (
            gspread.SpreadsheetNotFound
        ):
            st.error(
                "Create the Google Sheet "
                "first, share it with the "
                "service-account email as "
                "Editor, then use that "
                "exact sheet name."
            )
            st.stop()

        session = requests.Session()

        session.headers.update({
            "User-Agent": (
                "Private Streamlit Reddit "
                "research extractor/2.1"
            )
        })

        status = st.empty()
        progress = st.progress(0.0)

        try:
            all_comments = []

            for subreddit in subreddits:
                all_comments.extend(
                    download_subreddit_comments(
                        session,
                        subreddit,
                        int(comment_limit),
                        (
                            "newest"
                            if fetch_direction
                            == "Newest available"
                            else "oldest"
                        ),
                        float(delay),
                        status,
                        progress,
                    )
                )

            unique = {
                str(
                    item.get("id")
                ): item
                for item in all_comments
            }

            comments_df = (
                comments_dataframe(
                    list(
                        unique.values()
                    ),
                    output_sort,
                )
            )

            post_ids = (
                set(
                    comments_df[
                        "post_id"
                    ].dropna().astype(str)
                )
                if not comments_df.empty
                else set()
            )

            status.info(
                "Retrieving post titles "
                "and post content..."
            )

            posts_df = posts_dataframe(
                session,
                post_ids,
            )

            write_tab(
                spreadsheet,
                "Comments",
                comments_df,
            )

            write_tab(
                spreadsheet,
                "Posts",
                posts_df,
            )

            progress.progress(1.0)

            status.success(
                f"Finished: "
                f"{len(comments_df):,} "
                f"comments and "
                f"{len(posts_df):,} posts."
            )

            st.link_button(
                "Open Google Sheet",
                spreadsheet.url,
                use_container_width=True,
            )

        except Exception as exc:
            status.error(
                "The extraction stopped "
                "because of an error."
            )

            st.exception(exc)


else:
    with st.form(
        "keyword_form"
    ):
        patterns_text = st.text_area(
            "Keywords or wildcard patterns",
            value="case was approved",
            help=(
                "Enter one keyword, "
                "phrase, or wildcard "
                "pattern per line."
            ),
        )

        match_mode = st.selectbox(
            "Match mode",
            [
                "Any keyword",
                "All keywords",
                "Exact phrase",
                "Wildcard patterns",
            ],
        )

        wildcard_logic = (
            st.selectbox(
                "Wildcard pattern logic",
                [
                    "Any pattern",
                    "All patterns",
                ],
                disabled=(
                    match_mode
                    != "Wildcard patterns"
                ),
            )
        )

        search_in = st.selectbox(
            "Search in",
            [
                "Posts and comments",
                "Posts only",
                "Comments only",
            ],
        )

        subreddits_text = (
            st.text_input(
                "Subreddits (optional)",
                help=(
                    "Leave blank to search "
                    "across all indexed "
                    "subreddits. Separate "
                    "several names with commas."
                ),
            )
        )

        date_from = st.date_input(
            "Date from",
            value=date(2020, 1, 1),
        )

        date_to = st.date_input(
            "Date to",
            value=date.today(),
        )

        maximum_results = (
            st.number_input(
                "Maximum matching results",
                min_value=1,
                max_value=10000,
                value=500,
                step=50,
            )
        )

        candidate_limit = (
            st.number_input(
                "Maximum server "
                "search candidates",
                min_value=100,
                max_value=20000,
                value=2000,
                step=100,
            )
        )

        search_order = st.selectbox(
            "Search order",
            [
                "Newest first",
                "Oldest first",
            ],
        )

        save_mode = st.selectbox(
            "Save mode",
            [
                "Replace previous "
                "search results",
                "Append to existing "
                "search results",
            ],
        )

        submitted = (
            st.form_submit_button(
                "Start keyword search",
                type="primary",
                use_container_width=True,
            )
        )

    if submitted:
        patterns = [
            item.strip()
            for item in (
                patterns_text.splitlines()
            )
            if item.strip()
        ]

        if not patterns:
            st.error(
                "Enter at least one "
                "keyword or wildcard pattern."
            )
            st.stop()

        if (
            match_mode == "Exact phrase"
            and len(patterns) > 1
        ):
            st.error(
                "Exact phrase mode accepts "
                "one phrase only."
            )
            st.stop()

        if date_from > date_to:
            st.error(
                "Date from must be on or "
                "before Date to."
            )
            st.stop()

        client = sheets_client()

        try:
            spreadsheet = client.open(
                sheet_name
            )

        except (
            gspread.SpreadsheetNotFound
        ):
            st.error(
                "Create the Google Sheet "
                "first, share it with the "
                "service-account email as "
                "Editor, then use that "
                "exact sheet name."
            )
            st.stop()

        session = requests.Session()

        session.headers.update({
            "User-Agent": (
                "Private Streamlit Reddit "
                "research extractor/2.1"
            )
        })

        status = st.empty()

        try:
            subreddits = (
                parse_subreddits(
                    subreddits_text
                )
            )

            scopes = (
                subreddits
                or [None]
            )

            query_seed = (
                patterns[0]
                .replace("*", " ")
                .replace("?", " ")
                .strip()
            )

            query_seed = (
                query_seed
                or patterns[0]
            )

            after = unix_start(
                date_from
            )

            before = unix_end(
                date_to
            )

            all_rows = []
            total_candidates = 0

            for subreddit in scopes:
                if search_in in (
                    "Posts and comments",
                    "Posts only",
                ):
                    candidates = (
                        pullpush_search(
                            session,
                            PULLPUSH_POSTS_URL,
                            query_seed,
                            subreddit,
                            after,
                            before,
                            search_order,
                            int(
                                candidate_limit
                            ),
                            status,
                        )
                    )

                    total_candidates += len(
                        candidates
                    )

                    all_rows.extend(
                        normalize_search_results(
                            candidates,
                            "post",
                            patterns,
                            match_mode,
                            wildcard_logic,
                        )
                    )

                if search_in in (
                    "Posts and comments",
                    "Comments only",
                ):
                    candidates = (
                        pullpush_search(
                            session,
                            PULLPUSH_COMMENTS_URL,
                            query_seed,
                            subreddit,
                            after,
                            before,
                            search_order,
                            int(
                                candidate_limit
                            ),
                            status,
                        )
                    )

                    total_candidates += len(
                        candidates
                    )

                    all_rows.extend(
                        normalize_search_results(
                            candidates,
                            "comment",
                            patterns,
                            match_mode,
                            wildcard_logic,
                        )
                    )

            results_df = (
                pd.DataFrame(all_rows)
            )

            if not results_df.empty:
                results_df = (
                    results_df
                    .drop_duplicates(
                        subset=[
                            "result_type",
                            "post_id",
                            "comment_id",
                        ]
                    )
                    .sort_values(
                        "created_utc",
                        ascending=(
                            search_order
                            == "Oldest first"
                        ),
                    )
                    .head(
                        int(
                            maximum_results
                        )
                    )
                )

            append = (
                save_mode
                == (
                    "Append to existing "
                    "search results"
                )
            )

            write_tab(
                spreadsheet,
                "Search Results",
                results_df,
                append=append,
                dedupe_columns=[
                    "result_type",
                    "post_id",
                    "comment_id",
                ],
            )

            log_df = pd.DataFrame([
                {
                    "run_time_utc": (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    ),
                    "patterns": (
                        " | ".join(patterns)
                    ),
                    "match_mode": (
                        match_mode
                    ),
                    "wildcard_logic": (
                        wildcard_logic
                        if match_mode
                        == "Wildcard patterns"
                        else ""
                    ),
                    "search_in": (
                        search_in
                    ),
                    "subreddits": (
                        ", ".join(
                            subreddits
                        )
                        if subreddits
                        else (
                            "ALL INDEXED "
                            "SUBREDDITS"
                        )
                    ),
                    "date_from": str(
                        date_from
                    ),
                    "date_to": str(
                        date_to
                    ),
                    "server_candidates": (
                        total_candidates
                    ),
                    "matches_saved": (
                        len(results_df)
                    ),
                    "save_mode": (
                        save_mode
                    ),
                }
            ])

            write_tab(
                spreadsheet,
                "Search Log",
                log_df,
                append=True,
            )

            status.success(
                f"Finished: retrieved "
                f"{total_candidates:,} "
                f"server candidates and "
                f"saved "
                f"{len(results_df):,} matches."
            )

            st.link_button(
                "Open Google Sheet",
                spreadsheet.url,
                use_container_width=True,
            )

        except Exception as exc:
            status.error(
                "The keyword search stopped "
                "because of an error."
            )

            st.exception(exc)
