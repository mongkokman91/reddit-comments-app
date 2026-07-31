import json
import time
from datetime import datetime, timezone

import gspread
import pandas as pd
import requests
import streamlit as st
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe

st.set_page_config(page_title='Reddit Comments Extractor', page_icon='💬')
st.title('Reddit Comments Extractor')
st.caption('Extract Reddit comments into one Google Sheet with Comments and Posts tabs.')

COMMENTS_URL = 'https://arctic-shift.photon-reddit.com/api/comments/search'
POSTS_URL = 'https://arctic-shift.photon-reddit.com/api/posts/ids'


def parse_subreddits(value):
    seen = set()
    result = []
    for item in value.split(','):
        name = item.strip().removeprefix('r/')
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def extract_records(payload, keys):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for value in payload.values():
            if isinstance(value, dict):
                for key in keys:
                    nested = value.get(key)
                    if isinstance(nested, list):
                        return nested
    raise ValueError('Unrecognized API response: ' + json.dumps(payload)[:500])


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
    raise RuntimeError('Request failed after retries.')


def download_comments(session, subreddit, limit, direction, delay, status, progress):
    by_id = {}
    cursor = None
    previous_boundary = None
    page = 0

    while len(by_id) < limit:
        params = {
            'subreddit': subreddit,
            'sort': 'desc' if direction == 'newest' else 'asc',
            'limit': 'auto',
            'fields': 'id,author,author_flair_text,body,created_utc,distinguished,link_id,parent_id,retrieved_on,score,subreddit',
        }
        if cursor is not None:
            params['before' if direction == 'newest' else 'after'] = cursor

        payload = api_get(session, COMMENTS_URL, params)
        records = extract_records(payload, ('data', 'comments', 'results', 'items'))
        page += 1
        valid = [x for x in records if isinstance(x, dict) and x.get('id') and x.get('created_utc') is not None]
        if not valid:
            break

        valid.sort(key=lambda x: int(x['created_utc']), reverse=(direction == 'newest'))
        for item in valid:
            if len(by_id) >= limit:
                break
            cid = str(item['id']).removeprefix('t1_')
            item['id'] = cid
            by_id.setdefault(cid, item)

        timestamps = [int(x['created_utc']) for x in valid]
        oldest, newest = min(timestamps), max(timestamps)
        boundary = oldest if direction == 'newest' else newest

        status.info(f'r/{subreddit}: page {page} — {len(by_id):,}/{limit:,} comments')
        progress.progress(min(len(by_id) / limit, 1.0))

        if previous_boundary is not None:
            stuck = boundary >= previous_boundary if direction == 'newest' else boundary <= previous_boundary
            if stuck:
                break
        previous_boundary = boundary
        cursor = boundary
        time.sleep(delay)

    return sorted(by_id.values(), key=lambda x: int(x['created_utc']), reverse=(direction == 'newest'))[:limit]


def clean_id(value):
    if value is None:
        return None
    return str(value).removeprefix('t1_').removeprefix('t3_')


def comments_dataframe(comments, sort_choice):
    id_map = {str(c['id']).removeprefix('t1_'): c for c in comments}
    cache = {}

    def depth(cid, visiting=None):
        if cid in cache:
            return cache[cid]
        visiting = set() if visiting is None else visiting
        if cid in visiting:
            cache[cid] = None
            return None
        visiting.add(cid)
        comment = id_map.get(cid)
        if not comment:
            cache[cid] = None
            return None
        parent = comment.get('parent_id')
        if not parent or str(parent).startswith('t3_'):
            cache[cid] = 0
            return 0
        parent_id = str(parent).removeprefix('t1_')
        if parent_id not in id_map:
            cache[cid] = None
            return None
        parent_depth = depth(parent_id, visiting.copy())
        cache[cid] = None if parent_depth is None else parent_depth + 1
        return cache[cid]

    rows = []
    for c in comments:
        cid = str(c['id']).removeprefix('t1_')
        post_id = clean_id(c.get('link_id'))
        parent = c.get('parent_id')
        parent_type = 'post' if parent and str(parent).startswith('t3_') else 'comment' if parent else None
        created = int(c['created_utc'])
        rows.append({
            'comment_id': cid,
            'subreddit': c.get('subreddit'),
            'post_id': post_id,
            'parent_id': clean_id(parent),
            'parent_type': parent_type,
            'depth': depth(cid),
            'created_utc': created,
            'created_iso_utc': datetime.fromtimestamp(created, tz=timezone.utc).isoformat(),
            'author': c.get('author'),
            'body': c.get('body'),
            'score': c.get('score'),
            'permalink': f'https://www.reddit.com/comments/{post_id}/_/{cid}/' if post_id else None,
        })

    df = pd.DataFrame(rows).drop_duplicates(subset=['comment_id'])
    df['_author'] = df['author'].fillna('').astype(str).str.lower()
    df['_length'] = df['body'].fillna('').astype(str).str.len()

    sort_map = {
        'Newest': (['created_utc', 'comment_id'], [False, True]),
        'Oldest': (['created_utc', 'comment_id'], [True, True]),
        'Highest score': (['score', 'created_utc'], [False, False]),
        'Lowest score': (['score', 'created_utc'], [True, False]),
        'Deepest replies': (['depth', 'created_utc'], [False, False]),
        'Top-level first': (['depth', 'created_utc'], [True, False]),
        'Author A–Z': (['_author', 'created_utc'], [True, False]),
        'Author Z–A': (['_author', 'created_utc'], [False, False]),
        'Longest comments': (['_length', 'created_utc'], [False, False]),
        'Shortest comments': (['_length', 'created_utc'], [True, False]),
        'Subreddit A–Z': (['subreddit', 'created_utc'], [True, False]),
    }

    if sort_choice == 'Random':
        df = df.sample(frac=1, random_state=42)
    else:
        cols, asc = sort_map[sort_choice]
        df = df.sort_values(cols, ascending=asc, na_position='last')

    return df.drop(columns=['_author', '_length']).reset_index(drop=True)


def chunked(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def posts_dataframe(session, post_ids, delay):
    found = {}
    for batch in chunked(sorted(post_ids), 500):
        payload = api_get(session, POSTS_URL, {
            'ids': ','.join(batch),
            'fields': 'id,subreddit,title,selftext,author,created_utc,score,num_comments,url,link_flair_text',
        })
        for post in extract_records(payload, ('data', 'posts', 'results', 'items')):
            if isinstance(post, dict) and post.get('id'):
                found[str(post['id']).removeprefix('t3_')] = post
        time.sleep(delay)

    rows = []
    for post_id in sorted(post_ids):
        post = found.get(post_id, {})
        created = post.get('created_utc')
        rows.append({
            'post_id': post_id,
            'subreddit': post.get('subreddit'),
            'title': post.get('title'),
            'post_body': post.get('selftext'),
            'post_author': post.get('author'),
            'post_created_utc': created,
            'post_created_iso_utc': datetime.fromtimestamp(int(created), tz=timezone.utc).isoformat() if created is not None else None,
            'post_score': post.get('score'),
            'num_comments': post.get('num_comments'),
            'reddit_permalink': f'https://www.reddit.com/comments/{post_id}/',
            'archive_post_found': bool(post),
        })
    return pd.DataFrame(rows)


def sheets_client():
    info = dict(st.secrets['gcp_service_account'])
    scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    credentials = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(credentials)


def write_tab(spreadsheet, name, df):
    clean = df.replace([float('inf'), float('-inf')], '').where(pd.notna(df), '')
    rows, cols = max(len(clean) + 1, 2), max(len(clean.columns), 1)
    try:
        ws = spreadsheet.worksheet(name)
        ws.clear()
        ws.resize(rows=rows, cols=cols)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=rows, cols=cols)
    set_with_dataframe(ws, clean, include_index=False, include_column_header=True, resize=False)
    ws.freeze(rows=1)


with st.form('extractor'):
    subreddits_text = st.text_input('Subreddit(s)', 'i130suffering')
    comment_limit = st.number_input('Comments per subreddit', min_value=1, max_value=100000, value=5000, step=100)
    fetch_direction = st.selectbox('Which comments to collect', ['Newest available', 'Oldest available'])
    output_sort = st.selectbox('Sort Comments tab by', ['Newest', 'Oldest', 'Highest score', 'Lowest score', 'Deepest replies', 'Top-level first', 'Author A–Z', 'Author Z–A', 'Longest comments', 'Shortest comments', 'Subreddit A–Z', 'Random'])
    sheet_name = st.text_input('Google Sheet name', 'Reddit Comments Research')
    delay = st.number_input('Delay between requests (seconds)', min_value=0.0, max_value=10.0, value=1.0, step=0.5)
    submitted = st.form_submit_button('Start extraction', type='primary', use_container_width=True)

if submitted:
    subreddits = parse_subreddits(subreddits_text)
    if not subreddits:
        st.error('Enter at least one subreddit.')
        st.stop()

    try:
        client = sheets_client()
    except Exception as exc:
        st.error('Google Sheets credentials are not configured yet in Streamlit Secrets.')
        st.exception(exc)
        st.stop()

    session = requests.Session()
    session.headers.update({'User-Agent': 'Private Streamlit Reddit extractor/1.0'})
    status = st.empty()
    progress = st.progress(0.0)

    try:
        all_comments = []
        for subreddit in subreddits:
            all_comments.extend(download_comments(
                session, subreddit, int(comment_limit),
                'newest' if fetch_direction == 'Newest available' else 'oldest',
                float(delay), status, progress,
            ))

        unique = {str(x['id']).removeprefix('t1_'): x for x in all_comments}
        comments_df = comments_dataframe(list(unique.values()), output_sort)
        post_ids = {str(x) for x in comments_df['post_id'].dropna().unique() if str(x).strip()}

        status.info('Retrieving post titles and content...')
        posts_df = posts_dataframe(session, post_ids, float(delay))

        try:
            spreadsheet = client.open(sheet_name)
        except gspread.SpreadsheetNotFound:
            spreadsheet = client.create(sheet_name)

        status.info('Writing Google Sheets tabs...')
        write_tab(spreadsheet, 'Comments', comments_df)
        write_tab(spreadsheet, 'Posts', posts_df)

        progress.progress(1.0)
        status.success(f'Finished: {len(comments_df):,} comments and {len(posts_df):,} posts.')
        st.link_button('Open Google Sheet', spreadsheet.url, use_container_width=True)

    except Exception as exc:
        status.error('The extraction stopped because of an error.')
        st.exception(exc)
Oldest chats
