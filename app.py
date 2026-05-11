# -*- coding: utf-8 -*-
from flask import Flask, render_template_string, request, redirect, url_for, Response
import os
import csv
import io
import base64
import datetime as dt
import json  # ← 追加
import unicodedata
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import japanize_matplotlib
import re
import threading
import queue
import time





app = Flask(__name__)

DATA_DIR = 'data'
CRED_DIR = 'unupload'
TASKS_CSV = os.path.join(DATA_DIR, 'tasks.csv')
TAGS_CSV = os.path.join(DATA_DIR, 'tags.csv')
TAG_RULES_JSON = os.path.join(DATA_DIR, 'tag_rules.json')  # ← 追加

TASK_FIELDS = [
    'id', 'title', 'tag', 'score', 'due_date',
    'completed', 'completed_at', 'parent_id', 'recur',
    'google_task_id', 'sync_pending'
]

GOOGLE_SYNC_ENABLED = os.environ.get('GOOGLE_SYNC_ENABLED', '1').lower() in ('1', 'true', 'yes', 'on')
GOOGLE_TASKLIST_TITLE = os.environ.get('GOOGLE_TASKLIST_TITLE', 'TODO同期')
GOOGLE_CREDENTIALS_JSON = os.environ.get(
    'GOOGLE_CREDENTIALS_JSON',
    os.path.join(CRED_DIR, 'credentials.json')
)
GOOGLE_TOKEN_JSON = os.path.join(CRED_DIR, 'google_token.json')
GOOGLE_SCOPES = ['https://www.googleapis.com/auth/tasks']

VALID_SCORES = {30, 40, 50, 60, 70, 80, 90, 100}
VALID_RECURS = {'none', 'weekly', 'monthly'}

TASKS_LOCK = threading.RLock()
SYNC_QUEUE = queue.Queue()
SYNC_WORKER_LOCK = threading.Lock()
SYNC_STATE_LOCK = threading.Lock()
SYNC_WORKER_STARTED = False
SYNC_PULL_REQUESTED = False
SYNC_PULL_LAST_ENQUEUED_AT = 0.0
GOOGLE_PULL_MIN_INTERVAL_SEC = 30

CHART_CACHE_LOCK = threading.Lock()
CHART_CACHE = {
    'version': None,
    'png_bytes': None
}


# ---------- 永続化 ----------
def ensure_files():
    if not os.path.isdir(DATA_DIR):
        os.makedirs(DATA_DIR)
    if not os.path.exists(TAGS_CSV):
        with open(TAGS_CSV, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['tag'])
            w.writerow(['マイタスク'])
    if not os.path.exists(TASKS_CSV):
        with open(TASKS_CSV, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=TASK_FIELDS)
            w.writeheader()

def read_tags():
    ensure_files()
    tags = []
    with open(TAGS_CSV, 'r', newline='', encoding='utf-8') as f:
        r = csv.DictReader(f)
        for row in r:
            tags.append(row['tag'])
    if 'マイタスク' not in tags:
        tags.insert(0, 'マイタスク')
        write_tags(tags)
    return tags

def write_tags(tags):
    with open(TAGS_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['tag'])
        for t in tags:
            w.writerow([t])

def read_tasks():
    ensure_files()
    tasks = []
    with open(TASKS_CSV, 'r', newline='', encoding='utf-8') as f:
        r = csv.DictReader(f)
        for row in r:
            task_id = to_int(row.get('id'), 0)
            if task_id <= 0:
                continue

            task = {
                'id': task_id,
                'title': (row.get('title') or '').strip(),
                'tag': (row.get('tag') or 'マイタスク').strip() or 'マイタスク',
                'score': to_int(row.get('score'), 0),
                'due_date': sanitize_due_date(row.get('due_date')),
                'completed': 1 if to_int(row.get('completed'), 0) else 0,
                'completed_at': (row.get('completed_at') or '').strip(),
                'parent_id': sanitize_parent_id(row.get('parent_id')),
                'recur': sanitize_recur(row.get('recur', 'none')),
                'google_task_id': (row.get('google_task_id') or '').strip(),
                'sync_pending': 1 if to_int(row.get('sync_pending'), 0) else 0
            }
            tasks.append(task)
    return tasks

def write_tasks(tasks):
    with open(TASKS_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=TASK_FIELDS)
        w.writeheader()
        for t in tasks:
            w.writerow({
                'id': t['id'],
                'title': t['title'],
                'tag': t['tag'],
                'score': t['score'],
                'due_date': t['due_date'],
                'completed': t['completed'],
                'completed_at': t['completed_at'],
                'parent_id': t['parent_id'],
                'recur': t['recur'],
                'google_task_id': t.get('google_task_id', ''),
                'sync_pending': t.get('sync_pending', 0)
            })
def next_task_id(tasks):
    return (max([t['id'] for t in tasks]) + 1) if tasks else 1


def tag_color(tag):
    # 適当なパレット（好きに増やしてOK）
    colors = [
        "#ffd7d7", "#ffe7c7", "#fff7c7",
        "#e3ffd1", "#d1fff6", "#d9e4ff",
        "#ead9ff", "#ffd9f2"
    ]
    s = sum(ord(c) for c in str(tag))
    return colors[s % len(colors)]

# テンプレートから呼べるようにする
app.jinja_env.globals['tag_color'] = tag_color

def read_tag_rules():
    """
    tag_rules.json を読み込む。
    形式は:
    [
      {"tag": "家事", "keywords": ["洗う", "掃除"]},
      ...
    ]
    """
    if not os.path.exists(TAG_RULES_JSON):
        return []

    with open(TAG_RULES_JSON, 'r', encoding='utf-8') as f:
        try:
            rules = json.load(f)
        except json.JSONDecodeError:
            return []

    if not isinstance(rules, list):
        return []

    # 最低限のバリデーション
    norm = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        tag = r.get('tag')
        kws = r.get('keywords', [])
        if not tag or not isinstance(kws, list):
            continue
        norm.append({
            'tag': tag,
            'keywords': [str(k) for k in kws]
        })
    return norm

def auto_tag(title, current_tag, tags):
    if current_tag and current_tag != 'マイタスク':
        return current_tag

    rules = read_tag_rules()
    if not rules:
        return current_tag or 'マイタスク'

    norm_title = normalize_text(title)

    for rule in rules:
        tag_name = rule['tag']
        for kw in rule['keywords']:
            if normalize_text(kw) in norm_title:
                # タグが未定義なら追加
                if tag_name not in tags:
                    tags.append(tag_name)
                    write_tags(tags)
                return tag_name

    return current_tag or 'マイタスク'




# ---------- 日付ユーティリティ ----------
def today_str():
    return dt.date.today().isoformat()

def parse_date(s):
    return dt.datetime.strptime(s, '%Y-%m-%d').date()

def parse_dt_iso(s):
    return dt.datetime.fromisoformat(s) if s else None

def last_day_of_month(y, m):
    if m == 12:
        return dt.date(y+1, 1, 1) - dt.timedelta(days=1)
    return dt.date(y, m+1, 1) - dt.timedelta(days=1)

def add_months(date_str, months):
    d = parse_date(date_str)
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last = last_day_of_month(y, m).day
    day = d.day if d.day <= last else last
    return dt.date(y, m, day).isoformat()

def normalize_text(text):
    # 全角 → 半角、濁点など正規化
    return unicodedata.normalize('NFKC', text).lower()

def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def sanitize_due_date(value):
    value = (value or '').strip()
    try:
        return parse_date(value).isoformat()
    except (TypeError, ValueError):
        return today_str()

def sanitize_score(value, default=30):
    score = to_int(value, default)
    if score not in VALID_SCORES:
        return default
    return score

def sanitize_recur(value):
    value = (value or 'none').strip()
    if value not in VALID_RECURS:
        return 'none'
    return value

def sanitize_parent_id(value):
    value = (value or '').strip()
    return value if value.isdigit() else ''

def task_sync_signature(task):
    return (
        task.get('title', ''),
        task.get('due_date', ''),
        1 if task.get('completed') else 0,
        task.get('completed_at', '')
    )

def score_total_last_14_days(tasks):
    today = dt.date.today()
    start = today - dt.timedelta(days=13)
    total = 0

    for t in tasks:
        if t.get('completed') != 1 or not t.get('completed_at'):
            continue
        try:
            done = parse_dt_iso(t['completed_at']).date()
        except Exception:
            continue
        if start <= done <= today:
            total += to_int(t.get('score'), 0)

    return total

def get_chart_version():
    try:
        return int(os.path.getmtime(TASKS_CSV) * 1000)
    except OSError:
        return 0

def get_chart_png_bytes():
    version = get_chart_version()

    with CHART_CACHE_LOCK:
        if CHART_CACHE['version'] == version and CHART_CACHE['png_bytes'] is not None:
            return CHART_CACHE['png_bytes']

    with TASKS_LOCK:
        tasks = read_tasks()

    chart_b64, _ = chart_last_14_days_png_b64(tasks)
    png_bytes = base64.b64decode(chart_b64)

    with CHART_CACHE_LOCK:
        CHART_CACHE['version'] = version
        CHART_CACHE['png_bytes'] = png_bytes

    return png_bytes

def enqueue_sync_job(job):
    if not GOOGLE_SYNC_ENABLED:
        return
    SYNC_QUEUE.put(job)

def enqueue_task_sync(local_task_id):
    if not GOOGLE_SYNC_ENABLED:
        return
    enqueue_sync_job({
        'action': 'sync_task',
        'local_task_id': int(local_task_id)
    })

def enqueue_google_delete(google_task_id):
    if not GOOGLE_SYNC_ENABLED or not google_task_id:
        return
    enqueue_sync_job({
        'action': 'delete_google_task',
        'google_task_id': google_task_id
    })

def request_google_pull(force=False):
    if not GOOGLE_SYNC_ENABLED:
        return

    global SYNC_PULL_REQUESTED
    global SYNC_PULL_LAST_ENQUEUED_AT

    now = time.time()

    with SYNC_STATE_LOCK:
        if not force:
            if SYNC_PULL_REQUESTED:
                return
            if now - SYNC_PULL_LAST_ENQUEUED_AT < GOOGLE_PULL_MIN_INTERVAL_SEC:
                return

        SYNC_PULL_REQUESTED = True
        SYNC_PULL_LAST_ENQUEUED_AT = now

    enqueue_sync_job({'action': 'pull'})

def mark_google_pull_done():
    global SYNC_PULL_REQUESTED
    with SYNC_STATE_LOCK:
        SYNC_PULL_REQUESTED = False

def start_sync_worker():
    global SYNC_WORKER_STARTED

    if not GOOGLE_SYNC_ENABLED:
        return

    with SYNC_WORKER_LOCK:
        if SYNC_WORKER_STARTED:
            return

        th = threading.Thread(target=sync_worker_loop, daemon=True)
        th.start()
        SYNC_WORKER_STARTED = True

    request_google_pull(force=True)

def get_local_task_snapshot(local_task_id):
    with TASKS_LOCK:
        tasks = read_tasks()
        for t in tasks:
            if t['id'] == local_task_id:
                return dict(t)
    return None

def clear_google_task_id_if_matches(local_task_id, google_task_id):
    with TASKS_LOCK:
        tasks = read_tasks()
        changed = False
        for t in tasks:
            if t['id'] == local_task_id and t.get('google_task_id') == google_task_id:
                t['google_task_id'] = ''
                t['sync_pending'] = 1
                changed = True
                break
        if changed:
            write_tasks(tasks)
        return changed

def create_google_task_for_local(local_task_id, snapshot):
    google_id = google_insert_task(snapshot)
    if not google_id:
        return ''

    delete_created = False
    existing_google_id = ''

    with TASKS_LOCK:
        tasks = read_tasks()
        local_task = None
        for t in tasks:
            if t['id'] == local_task_id:
                local_task = t
                break

        if not local_task:
            delete_created = True
        elif local_task.get('google_task_id') and local_task['google_task_id'] != google_id:
            delete_created = True
            existing_google_id = local_task['google_task_id']
        else:
            if local_task.get('google_task_id') != google_id:
                local_task['google_task_id'] = google_id
                write_tasks(tasks)

    if delete_created:
        google_delete_task(google_id)
        return existing_google_id

    return google_id

def mark_task_synced(local_task_id, snapshot, google_task_id):
    expected_signature = task_sync_signature(snapshot)

    with TASKS_LOCK:
        tasks = read_tasks()
        changed = False

        for t in tasks:
            if t['id'] != local_task_id:
                continue

            if t.get('google_task_id') != google_task_id and google_task_id:
                t['google_task_id'] = google_task_id
                changed = True

            if task_sync_signature(t) == expected_signature:
                if t.get('sync_pending', 0) != 0:
                    t['sync_pending'] = 0
                    changed = True

            if changed:
                write_tasks(tasks)

            return True

    return False

def sync_local_task_to_google(local_task_id, allow_recreate=True):
    snapshot = get_local_task_snapshot(local_task_id)
    if not snapshot:
        return True

    google_id = snapshot.get('google_task_id', '')

    if not google_id:
        google_id = create_google_task_for_local(local_task_id, snapshot)
        if not google_id:
            return False

        snapshot = get_local_task_snapshot(local_task_id)
        if not snapshot:
            return True

    body = {
        'title': snapshot['title'],
        'notes': make_google_notes(snapshot)
    }

    due = google_due_str(snapshot.get('due_date', ''))
    if due:
        body['due'] = due

    ok, error_status = google_patch_task_result(google_id, body)

    if ok:
        if snapshot.get('completed') == 1:
            ok, error_status = google_patch_task_result(
                google_id,
                {'status': 'completed'}
            )
        else:
            ok, error_status = google_patch_task_result(
                google_id,
                {'status': 'needsAction'}
            )

    if ok:
        mark_task_synced(local_task_id, snapshot, google_id)
        return True

    if allow_recreate and error_status == 404:
        clear_google_task_id_if_matches(local_task_id, google_id)
        return sync_local_task_to_google(local_task_id, allow_recreate=False)

    return False

def process_sync_job(job):
    action = job.get('action')

    if action == 'sync_task':
        local_task_id = to_int(job.get('local_task_id'), 0)
        if local_task_id > 0:
            sync_local_task_to_google(local_task_id)
        return

    if action == 'delete_google_task':
        google_task_id = (job.get('google_task_id') or '').strip()
        if google_task_id:
            google_delete_task(google_task_id)
        return

    if action == 'pull':
        try:
            sync_google_to_local()
        finally:
            mark_google_pull_done()
        return

def sync_worker_loop():
    while True:
        try:
            job = SYNC_QUEUE.get(timeout=GOOGLE_PULL_MIN_INTERVAL_SEC)
        except queue.Empty:
            try:
                sync_google_to_local()
            except Exception:
                app.logger.exception('バックグラウンド同期に失敗した')
            else:
                mark_google_pull_done()
            continue

        try:
            process_sync_job(job)
        except Exception:
            app.logger.exception('同期ジョブの処理に失敗した: %s', job)
        finally:
            SYNC_QUEUE.task_done()

# ---------- スコア集計＆折れ線描画（プロット内は英語） ----------
def chart_last_14_days_png_b64(tasks):
    today = dt.date.today()
    days = [today - dt.timedelta(days=i) for i in range(13, -1, -1)]  # 14日分(過去→今日)

    # --- 14日分の合計スコア ---
    sums = []
    for d in days:
        s = 0
        for t in tasks:
            if t['completed'] == 1 and t['completed_at']:
                done = parse_dt_iso(t['completed_at']).date()
                if done == d:
                    s += int(t['score'])
        sums.append(s)
    total = sum(sums)

    # --- 昨日・今日の個別タスク ---
    target_days = [today - dt.timedelta(days=1), today]  # [昨日, 今日]
    day_tasks = {d: [] for d in target_days}

    for t in tasks:
        if t['completed'] == 1 and t['completed_at']:
            done = parse_dt_iso(t['completed_at']).date()
            if done in day_tasks:
                day_tasks[done].append(t)

    # 完了時刻順に並べる
    for d in target_days:
        day_tasks[d].sort(
            key=lambda x: parse_dt_iso(x['completed_at']) if x['completed_at'] else dt.datetime.min
        )

    # --- Figure 作成 ---
    fig = plt.figure(figsize=(9.0, 3.4), dpi=120)
    gs = fig.add_gridspec(1, 2, width_ratios=[2, 1])

    # 左: 14日折れ線
    ax1 = fig.add_subplot(gs[0, 0])
    ax1.plot(range(len(days)), sums, marker='o')
    ax1.set_title('Scores (last 14 days)')
    ax1.set_xlabel('Date')
    ax1.set_ylabel('Score')
    ax1.set_xticks(range(len(days)))
    ax1.set_xticklabels([d.strftime('%m/%d') for d in days], rotation=45)
    ax1.grid(True, linestyle='--', linewidth=0.5)

    # 右: 縦積み棒
    ax2 = fig.add_subplot(gs[0, 1])

    x_pos = [0, 1]  # 0=昨日, 1=今日
    bar_width = 0.6
    max_stack = 0

    for i, d in enumerate(target_days):
        bottom = 0
        for t in day_tasks[d]:
            sc = int(t['score'])
            if sc <= 0:
                continue

            # 棒
            ax2.bar(i, sc, width=bar_width, bottom=bottom)

            # ★ 今日だけラベルを右に表示する
            if d == today:
                title = t['title']
                if len(title) > 15:
                    title = title[:14] + "…"

                label_y = bottom + sc / 2
                label_x = i + bar_width/2 + 0.15

                ax2.text(
                    label_x,
                    label_y,
                    title,
                    va='center',
                    ha='left',
                    fontsize=8
                )

            bottom += sc

        max_stack = max(max_stack, bottom)

    ax2.set_xticks(x_pos)
    ax2.set_xticklabels([d.strftime('%m/%d') for d in target_days], rotation=45)
    ax2.set_ylabel('Score (stacked)')
    ax2.set_title('Yesterday & Today')

    if max_stack > 0:
        ax2.set_ylim(0, max_stack * 1.15)

    # ラベル分の余白
    ax2.set_xlim(-0.5, 1.4)

    fig.tight_layout(rect=[0, 0, 0.92, 1])  # 右側の余白を大きめに

    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    plt.close(fig)
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return b64, total

def chart_today_progress_png_b64(tasks):
    def char_width(ch):
        return 2 if unicodedata.east_asian_width(ch) in ('F', 'W', 'A') else 1

    def wrap_label(text, max_width=24, max_lines=2):
        text = str(text)
        lines = []
        line = ''
        width = 0

        for ch in text:
            w = char_width(ch)
            if width + w > max_width and line:
                lines.append(line)
                line = ch
                width = w
            else:
                line += ch
                width += w

            if len(lines) >= max_lines:
                break

        if line and len(lines) < max_lines:
            lines.append(line)

        consumed = ''.join(lines)
        if len(consumed) < len(text):
            lines[-1] = lines[-1].rstrip('…') + '…'

        return '\n'.join(lines)

    today = dt.date.today()

    done_today = []
    for t in tasks:
        if t.get('completed') != 1 or not t.get('completed_at'):
            continue
        try:
            done = parse_dt_iso(t['completed_at']).date()
        except Exception:
            continue
        if done == today:
            done_today.append(t)

    done_today.sort(
        key=lambda x: parse_dt_iso(x['completed_at']) if x['completed_at'] else dt.datetime.min
    )

    n = len(done_today)

    fig_h = max(1.6, 0.65 * max(n, 1) + 0.2)
    fig = plt.figure(figsize=(11.0, fig_h), dpi=130)
    ax = fig.add_subplot(111)

    if n == 0:
        ax.text(
            0.5,
            0.5,
            'No completed tasks today',
            ha='center',
            va='center',
            fontsize=18
        )
        ax.set_axis_off()
    else:
        colors = [
            '#4f83f1',
            '#f45b69',
            '#f2c94c',
            '#2fb344',
            '#9b5de5',
            '#00a6a6',
            '#f2994a',
            '#6c757d'
        ]

        text_colors = [
            '#2457c5',
            '#c5303f',
            '#9a6b00',
            '#1f7a32',
            '#6f35c2',
            '#007575',
            '#b75f00',
            '#444444'
        ]

        scores = []
        for t in done_today:
            scores.append(max(to_int(t.get('score'), 0), 0))

        max_total = sum(scores)
        if max_total <= 0:
            max_total = 1

        for row in range(n):
            left = 0

            for j in range(row + 1):
                sc = scores[j]
                if sc <= 0:
                    continue

                ax.barh(
                    row,
                    sc,
                    left=left,
                    height=0.78,
                    color=colors[j % len(colors)],
                    edgecolor='white',
                    linewidth=0.8
                )
                left += sc

            title = wrap_label(done_today[row]['title'], max_width=24, max_lines=2)

            ax.text(
                left + max_total * 0.03,
                row,
                title,
                ha='left',
                va='center',
                fontsize=16,
                fontweight='bold',
                linespacing=1.15,
                color=text_colors[row % len(text_colors)]
            )

        ax.set_xlim(0, max_total * 2.15)
        ax.set_ylim(-0.55, n - 0.45)

        ax.set_yticks([])
        ax.tick_params(axis='x', labelsize=13, pad=1)

        ax.grid(True, axis='x', linestyle='--', linewidth=0.6, alpha=0.65)

        for spine in ['top', 'right', 'left']:
            ax.spines[spine].set_visible(False)

        ax.spines['bottom'].set_alpha(0.4)

    fig.subplots_adjust(
        left=0.03,
        right=0.99,
        top=0.99,
        bottom=0.07
    )

    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    plt.close(fig)

    return base64.b64encode(buf.getvalue()).decode('ascii')

def get_google_service():
    if not GOOGLE_SYNC_ENABLED:
        return None

    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from google.auth.exceptions import RefreshError  # ← ★ここに移動させる
    except ImportError as e:
        app.logger.warning('Google Tasks ライブラリの読み込みに失敗した: %s', e)
        return None

    try:
        creds = None

        if os.path.exists(GOOGLE_TOKEN_JSON):
            creds = Credentials.from_authorized_user_file(
                GOOGLE_TOKEN_JSON,
                GOOGLE_SCOPES
            )

        if not creds or not creds.valid:
            try:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(GoogleRequest())
                else:
                    raise RefreshError("no valid creds")
        
            except RefreshError:
                if os.path.exists(GOOGLE_TOKEN_JSON):
                    os.remove(GOOGLE_TOKEN_JSON)
        
                flow = InstalledAppFlow.from_client_secrets_file(
                    GOOGLE_CREDENTIALS_JSON,
                    GOOGLE_SCOPES
                )
                creds = flow.run_local_server(port=0)
        
            with open(GOOGLE_TOKEN_JSON, 'w', encoding='utf-8') as f:
                f.write(creds.to_json())

        return build('tasks', 'v1', credentials=creds)
    except Exception:
        app.logger.exception('Google Tasksサービスの初期化に失敗した')
        return None


def get_google_tasklist_id(service):
    if not service:
        return None

    try:
        res = service.tasklists().list(maxResults=100).execute()
        for item in res.get('items', []):
            if item.get('title') == GOOGLE_TASKLIST_TITLE:
                return item['id']

        res = service.tasklists().insert(
            body={'title': GOOGLE_TASKLIST_TITLE}
        ).execute()
        return res['id']
    except Exception:
        app.logger.exception('Googleタスクリストの取得に失敗した')
        return None


def google_due_str(date_str):
    if not date_str:
        return None
    return f'{date_str}T00:00:00.000Z'


def google_completed_to_local_str(s):
    if not s:
        return ''
    x = dt.datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone()
    return x.replace(tzinfo=None, microsecond=0).isoformat(sep=' ')


def local_id_from_notes(notes):
    if not notes:
        return None
    m = re.search(r'LOCAL_ID=(\d+)', notes)
    if not m:
        return None
    return int(m.group(1))


def make_google_notes(local_task):
    return f'LOCAL_ID={local_task["id"]}'


def google_insert_task(local_task):
    service = get_google_service()
    tasklist_id = get_google_tasklist_id(service)
    if not service or not tasklist_id:
        return ''

    body = {
        'title': local_task['title'],
        'notes': make_google_notes(local_task)
    }

    due = google_due_str(local_task.get('due_date', ''))
    if due:
        body['due'] = due

    try:
        res = service.tasks().insert(
            tasklist=tasklist_id,
            body=body
        ).execute()
        return res.get('id', '')
    except Exception:
        app.logger.exception('Googleタスクの作成に失敗した')
        return ''


def google_patch_task_result(task_id, body):
    if not task_id:
        return False, None

    service = get_google_service()
    tasklist_id = get_google_tasklist_id(service)
    if not service or not tasklist_id:
        return False, None

    try:
        service.tasks().patch(
            tasklist=tasklist_id,
            task=task_id,
            body=body
        ).execute()
        return True, None
    except Exception as e:
        status = getattr(getattr(e, 'resp', None), 'status', None)
        app.logger.exception('Googleタスクの更新に失敗した: %s', task_id)
        return False, status


def google_patch_task(task_id, body):
    ok, _ = google_patch_task_result(task_id, body)
    return ok

def google_patch_task_notes(task_id, notes):
    return google_patch_task(task_id, {'notes': notes})


def google_mark_task_completed(task_id):
    return google_patch_task(task_id, {'status': 'completed'})


def google_mark_task_uncompleted(task_id):
    return google_patch_task(task_id, {'status': 'needsAction'})


def google_update_task_due(task_id, due_date):
    if not due_date:
        return False
    return google_patch_task(task_id, {'due': google_due_str(due_date)})

def google_sync_available():
    return GOOGLE_SYNC_ENABLED and os.path.exists(GOOGLE_CREDENTIALS_JSON)

def sync_google_to_local():
    if not GOOGLE_SYNC_ENABLED:
        return

    service = get_google_service()
    tasklist_id = get_google_tasklist_id(service)
    if not service or not tasklist_id:
        return

    google_tasks = []
    page_token = None

    try:
        while True:
            res = service.tasks().list(
                tasklist=tasklist_id,
                showCompleted=True,
                showHidden=True,
                showDeleted=True,
                maxResults=100,
                pageToken=page_token
            ).execute()

            google_tasks.extend(res.get('items', []))
            page_token = res.get('nextPageToken')
            if not page_token:
                break
    except Exception:
        app.logger.exception('Googleタスクの同期に失敗した')
        return

    notes_to_patch = []

    with TASKS_LOCK:
        tasks = read_tasks()

        local_by_id = {t['id']: t for t in tasks}
        local_by_google_id = {
            t.get('google_task_id', ''): t
            for t in tasks
            if t.get('google_task_id')
        }

        changed = False

        for gt in google_tasks:
            google_id = gt.get('id', '')
            if not google_id:
                continue

            notes = gt.get('notes', '') or ''
            local_id = local_id_from_notes(notes)

            local_task = None

            if google_id in local_by_google_id:
                local_task = local_by_google_id[google_id]
            elif local_id is not None and local_id in local_by_id:
                local_task = local_by_id[local_id]
                if not local_task.get('google_task_id'):
                    local_task['google_task_id'] = google_id
                    local_by_google_id[google_id] = local_task
                    changed = True

            if gt.get('deleted'):
                if local_task and not local_task.get('sync_pending', 0):
                    if local_task.get('google_task_id'):
                        local_task['google_task_id'] = ''
                        changed = True
                continue

            if local_task is None:
                new_id = next_task_id(tasks)
                due_raw = gt.get('due', '') or ''
                due_date = due_raw[:10] if due_raw else today_str()

                local_task = {
                    'id': new_id,
                    'title': gt.get('title', '').strip() or '(no title)',
                    'tag': 'マイタスク',
                    'score': 30,
                    'due_date': due_date,
                    'completed': 0,
                    'completed_at': '',
                    'parent_id': '',
                    'recur': 'none',
                    'google_task_id': google_id,
                    'sync_pending': 0
                }

                if gt.get('status') == 'completed':
                    local_task['completed'] = 1
                    local_task['completed_at'] = google_completed_to_local_str(
                        gt.get('completed')
                    )

                tasks.append(local_task)
                local_by_id[new_id] = local_task
                local_by_google_id[google_id] = local_task
                changed = True
                notes_to_patch.append((google_id, make_google_notes(local_task)))
                continue

            if local_task.get('google_task_id') != google_id:
                local_task['google_task_id'] = google_id
                changed = True

            if local_id != local_task['id']:
                notes_to_patch.append((google_id, make_google_notes(local_task)))

            remote_completed = 1 if gt.get('status') == 'completed' else 0
            remote_completed_at = ''
            if remote_completed:
                remote_completed_at = google_completed_to_local_str(
                    gt.get('completed')
                )
            
            if local_task.get('sync_pending', 0):
                if remote_completed:
                    local_task['completed'] = 1
                    local_task['completed_at'] = remote_completed_at
                    local_task['sync_pending'] = 0
                    changed = True
                continue

            remote_title = gt.get('title', '').strip() or '(no title)'
            if local_task['title'] != remote_title:
                local_task['title'] = remote_title
                changed = True

            due_raw = gt.get('due', '') or ''
            remote_due_date = due_raw[:10] if due_raw else ''
            if remote_due_date and local_task['due_date'] != remote_due_date:
                local_task['due_date'] = remote_due_date
                changed = True

            remote_completed = 1 if gt.get('status') == 'completed' else 0
            remote_completed_at = ''
            if remote_completed:
                remote_completed_at = google_completed_to_local_str(
                    gt.get('completed')
                )

            if local_task['completed'] != remote_completed:
                local_task['completed'] = remote_completed
                local_task['completed_at'] = remote_completed_at
                changed = True
            elif remote_completed and local_task['completed_at'] != remote_completed_at:
                local_task['completed_at'] = remote_completed_at
                changed = True
            elif not remote_completed and local_task['completed_at']:
                local_task['completed_at'] = ''
                changed = True

        if changed:
            write_tasks(tasks)

    for google_id, notes in notes_to_patch:
        google_patch_task_notes(google_id, notes)

def google_delete_task(task_id):
    if not task_id:
        return False

    service = get_google_service()
    tasklist_id = get_google_tasklist_id(service)
    if not service or not tasklist_id:
        return False

    try:
        service.tasks().delete(
            tasklist=tasklist_id,
            task=task_id
        ).execute()
        return True
    except Exception:
        app.logger.exception('Googleタスクの削除に失敗した: %s', task_id)
        return False
# ---------- HTML（グラフは最下部に配置） ----------
INDEX_HTML = r"""
<!doctype html>
<meta charset="utf-8">
<title>TODO</title>
<style>
body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans JP", "Hiragino Kaku Gothic ProN", Meiryo, sans-serif; margin: 20px; }
section { margin-bottom: 24px; }
h1 { margin: 0 0 8px 0; }
h2 { margin: 16px 0 8px 0; font-size: 1.1rem; }
small { color: #666; }
input[type=text] { width: 20em; }
ul.tree, ul.tree ul { list-style: none; padding-left: 1em; border-left: 1px dotted #ccc; }
li.task { margin: 4px 0; padding-left: .3em; }

/* 1タスクの“1行表示”はこの箱（task-row）だけに適用する */
.task-row{
  display:flex;
  align-items:center;
  gap:6px;
}

/* タスク名は省略（…）して、ホバーで全文（title属性） */
.task-title{
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* バッジ類は縦に崩れないようにする */
.task-row .badge{
  flex: 0 0 auto;
  white-space: nowrap;
}

/* ✎編集リンクをボタン風に */
a.btn-edit{
  flex: 0 0 auto;
  display:inline-block;
  padding:2px 6px;
  border:1px solid #bbb;
  border-radius:4px;
  text-decoration:none;
  color:#333;
  background:#fff;
}
a.btn-edit:hover{ background:#f2f2f2; }

.badge {
  display:inline-block;
  padding:2px 6px;
  border-radius: 3px;
  margin-left:6px;
  font-size: .85em;
  border: 1px solid #ddd;
  background:#fafafa;
  color:#333;
}

/* タグ用（全部グレー系で統一） */
.badge-tag {
  background:#f2f2f2;
  border-color:#e0e0e0;
  color:#555;
}

/* 期限超過：背景は白、文字と枠だけ赤系 */
.badge-overdue {
  background:#fff;
  color:#c62828;
  border-color:#ffcdd2;
}

/* スコア用（だけ色付き） */
.badge-score-low  { background:#e0f3ff; border-color:#b3e0ff; }   /* 〜49 */
.badge-score-mid  { background:#fff4c4; border-color:#ffe08a; }   /* 50〜79 */
.badge-score-high { background:#ffd7d7; border-color:#ffb3b3; }   /* 80〜99 */

/* 100点だけ金色 */
.badge-score-max {
  background: linear-gradient(135deg, #ffd700, #ffea8a);
  color: #503000;
  font-weight: bold;
  border: 1px solid #c9a200;
}

.row { display:flex; gap: 16px; flex-wrap: wrap; }
.card { border:1px solid #ddd; border-radius:8px; padding:12px; }
button, input[type=submit] { cursor:pointer; }
table { border-collapse: collapse; }
td, th { padding: 4px 6px; border-bottom:1px solid #eee; }
.form-inline > * { margin-right: 8px; }


.score-choices label { margin-right:6px; }

.task-register-layout {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 16px;
}

.task-register-layout > section {
  margin-bottom: 0;
}

.task-register-card {
  flex: 0 0 430px;
}

.task-chart-card {
  flex: 1 1 720px;
  min-height: 0;
  padding: 4px 8px;
  display: flex;
  align-items: flex-start;
  justify-content: center;
}

.task-chart-card img {
  width: 100%;
  max-width: 980px;
  height: auto;
  display: block;
}

@media (max-width: 900px) {
  .task-register-layout {
    flex-direction: column;
  }

  .task-register-card {
    flex: auto;
  }

  .task-chart-card {
    width: 100%;
  }
}
</style>





{% if overdue %}
<section class="card">
  <h2>期限超過（再設定が必要）</h2>
  {% for t in overdue %}
  <form class="form-inline" method="post" action="{{ url_for('reschedule', task_id=t['id']) }}">
    <strong>{{ t['title'] }}</strong>
    <span class="badge badge-overdue">期限: {{ t['due_date'] }}</span>

    <input type="date" name="new_due_date" value="{{ today }}">
    <input type="submit" value="再設定">
  </form>
  {% endfor %}
</section>
{% endif %}

<div class="task-register-layout">

  <section class="card task-register-card">
    <h2>タスク登録</h2>

    <form method="post" action="{{ url_for('add') }}">
      <div class="form-inline">
        <label>タイトル</label>
        <input type="text" name="title" required>
      </div>

      <div class="form-inline" style="margin-top:8px;">
        <label>期日</label>
        <input type="date" name="due_date" value="{{ today }}">
      </div>

      <div style="margin-top:8px;">
        <div>点数（デフォルト30）</div>
        <div class="score-choices">
          {% for s in [30,40,50,60,70,80,90,100] %}
            <label><input type="radio" name="score" value="{{ s }}" {% if s==30 %}checked{% endif %}>{{ s }}</label>
          {% endfor %}
        </div>
      </div>

      <div style="margin-top:8px;">
        <input type="submit" value="追加">
      </div>
    </form>
  </section>

    <section class="card task-chart-card">
      <img
        alt="today progress chart"
        src="{{ url_for('chart_today_progress_png') }}?v={{ chart_version }}"
      >
    </section>

</div>

<section class="card">
  <h2>未完了タスク</h2>
  <div class="row">
    <!-- 左：ツリー -->
    <div style="flex:2; min-width: 260px;">
      <ul class="tree">
        {% macro render_children(pid) %}
          {% for t in children_by_parent.get(pid, []) %}
         <li class="task">
          <div class="task-row">
            <form style="display:inline;" method="post" action="{{ url_for('complete', task_id=t['id']) }}">
              <button title="完了">✔</button>
            </form>
        
            <form style="display:inline;" method="post"
                  action="{{ url_for('delete', task_id=t['id']) }}">
              <button title="削除">✖</button>
            </form>
        
            <strong class="task-title" title="{{ t['title'] }}">{{ t['title'] }}</strong>
        
            <span class="badge badge-tag">{{ t['tag'] }}</span>
        
            {% set score_class =
                'badge-score-max' if t['score'] == 100
                else ('badge-score-high' if t['score'] >= 80
                else ('badge-score-mid' if t['score'] >= 50
                else 'badge-score-low'))
            %}
            <span class="badge {{ score_class }}">点: {{ t['score'] }}</span>
        
            <span class="badge {% if t['is_overdue'] %}badge-overdue{% endif %}">
              期日: {{ t['due_date'] }}
            </span>
        
            {% if t['recur'] != 'none' %}
              <span class="badge">定期: {{ '毎週' if t['recur']=='weekly' else '毎月' }}</span>
            {% endif %}
        
            <a class="btn-edit" href="{{ url_for('edit_task', task_id=t['id']) }}" title="タグ/親タスクを編集">✎</a>
          </div>
        
          <ul>
            {{ render_children(t['id_str']) }}
          </ul>
        </li>
            
          {% endfor %}
        {% endmacro %}
        {{ render_children('') }}
      </ul>
    </div>

    <!-- 右：1週間カレンダー -->
    <div style="flex:1; min-width: 220px;">
      <h3>今週のカレンダー</h3>
      <table>
        <thead>
          <tr>
            <th>日付</th>
            <th>タスク</th>
          </tr>
        </thead>
        <tbody>
          {% for d in week_calendar %}
          <tr>
            <td>{{ d.date.strftime('%m/%d(%a)') }}</td>
            <td>
              {% if d.tasks %}
                <ul style="margin:0; padding-left:1em;">
                  {% for t in d.tasks %}
                    <li>{{ t['title'] }}</li>
                  {% endfor %}
                </ul>
              {% else %}
                なし
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</section>


<section class="card">
  <h2>過去2週間のスコア推移</h2>
  <div><img alt="chart" src="{{ url_for('chart_last_14_png') }}?v={{ chart_version }}"></div>
  <div>合計点: <strong>{{ total_14d }}</strong></div>
</section>


{% if google_sync_available %}
<section class="card">
  <form method="post" action="{{ url_for('refresh_google') }}">
    <button type="submit">Googleから更新</button>
  </form>
</section>
{% endif %}

<section class="card">
  <h2>最近完了</h2>
  <table>
    <thead>
      <tr>
        <th>タイトル</th>
        <th>点</th>
        <th>完了時刻</th>
        <th>タグ</th>
        <th>操作</th>
      </tr>
    </thead>
    <tbody>
      {% for t in recent_done %}
      <tr>
        <td>{{ t['title'] }}</td>
        {% set scls =
          'badge-score-max' if t['score'] == 100
          else ('badge-score-high' if t['score'] >= 80
          else ('badge-score-mid' if t['score'] >= 50
          else 'badge-score-low'))
        %}
        <td class="{{ scls }}" style="text-align:right">{{ t['score'] }}</td>



        <td>{{ t['completed_at'] }}</td>
        <td>
          <span class="badge badge-tag">{{ t['tag'] }}</span>
        </td>

        <td>
          <form method="post" action="{{ url_for('undo', task_id=t['id']) }}">
            <button title="完了を元に戻す">戻す</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>

"""

TAGS_HTML = r"""
<!doctype html>
<meta charset="utf-8">
<title>タグ管理</title>
<style>
body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans JP", "Hiragino Kaku Gothic ProN", Meiryo, sans-serif; margin: 20px; }
section { margin-bottom: 20px; }
.badge { display:inline-block; padding:2px 6px; border-radius: 3px; background:#eee; margin-right:6px; }
form { display:inline-block; margin-right:8px; }
</style>

<h1>タグ管理</h1>
<section>
  <h2>追加</h2>
  <form method="post" action="{{ url_for('add_tag') }}">
    <input type="text" name="new_tag" required>
    <input type="submit" value="追加">
  </form>
</section>

<section>
  <h2>削除</h2>
  {% for t in tags %}
    <span class="badge">{{ t }}</span>
    {% if t != 'マイタスク' %}
      <form method="post" action="{{ url_for('delete_tag') }}" onsubmit="return confirm('このタグを削除し、付与済みタスクは「マイタスク」に移動する。よいか？');">
        <input type="hidden" name="tag" value="{{ t }}">
        <input type="submit" value="削除">
      </form>
    {% endif %}
    <br>
  {% endfor %}
</section>

<div><a href="{{ url_for('index') }}">戻る</a></div>
"""
EDIT_HTML = r"""
<!doctype html>
<meta charset="utf-8">
<title>タスク編集</title>
<style>
body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans JP", "Hiragino Kaku Gothic ProN", Meiryo, sans-serif; margin: 20px; }
.card { border:1px solid #ddd; border-radius:8px; padding:12px; max-width: 900px; }
.form-inline > * { margin-right: 10px; }
</style>

<h1>タスク編集</h1>

<section class="card">
  <div style="margin-bottom:10px;">
    <strong>{{ task['title'] }}</strong>
  </div>

  <form method="post">
    <div class="form-inline">
      <label>タグ</label>
      <select name="tag">
        {% for tg in tags %}
          <option value="{{ tg }}" {% if tg == task['tag'] %}selected{% endif %}>{{ tg }}</option>
        {% endfor %}
      </select>

    <label>親タスク</label>
    <select name="parent_id">
      <option value="" {% if current_parent_id is none %}selected{% endif %}>なし</option>
      {% for p in parent_candidates %}
        <option value="{{ p['id'] }}" {% if current_parent_id == p['id'] %}selected{% endif %}>{{ p['title'] }}</option>
      {% endfor %}
    </select>
    
    <label>期日</label>
    <input type="date" name="due_date" value="{{ task['due_date'] }}">
    <label>定期</label>
    <select name="recur">
      <option value="none" {% if task['recur'] == 'none' %}selected{% endif %}>なし</option>
      <option value="weekly" {% if task['recur'] == 'weekly' %}selected{% endif %}>毎週</option>
      <option value="monthly" {% if task['recur'] == 'monthly' %}selected{% endif %}>毎月</option>
    </select>
    <button>保存</button>
    </div>
  </form>
</section>

<div style="margin-top:12px;"><a href="{{ url_for('index') }}">戻る</a></div>
"""
# ---------- ルーティング ----------
@app.before_request
def ensure_background_sync():
    start_sync_worker()

@app.route('/')
def index():
    request_google_pull()

    with TASKS_LOCK:
        tasks = read_tasks()
    tags = read_tags()

    today = dt.date.today()
    active = []
    for t in tasks:
        if t['completed'] == 0:
            t['is_overdue'] = parse_date(t['due_date']) < today
            t['id_str'] = str(t['id'])
            active.append(t)

    active.sort(key=lambda t: (parse_date(t['due_date']), -t['id']))

    overdue = [t for t in active if t['is_overdue']]

    active_ids = {str(t['id']) for t in active}

    children_by_parent = {}
    for t in active:
        pid = t['parent_id'] if t['parent_id'] in active_ids else ''
        t['parent_id_effective'] = pid
        children_by_parent.setdefault(pid, []).append(t)

    selectable_parents = sorted(
        active,
        key=lambda x: (parse_date(x['due_date']), -x['id'])
    )

    for t in active:
        forbidden = {t['id']}
        stack = [t['id_str']]
        while stack:
            pid = stack.pop()
            for ch in children_by_parent.get(pid, []):
                if ch['id'] not in forbidden:
                    forbidden.add(ch['id'])
                    stack.append(ch['id_str'])
        t['forbidden_parent_ids'] = forbidden

    week_calendar = []
    for offset in range(7):
        d = today + dt.timedelta(days=offset)
        ds = d.isoformat()
        day_tasks = [t for t in active if t['due_date'] == ds]
        week_calendar.append({
            'date': d,
            'tasks': day_tasks,
        })

    total_14d = score_total_last_14_days(tasks)

    done = [t for t in tasks if t['completed'] == 1 and t['completed_at']]
    done.sort(key=lambda x: parse_dt_iso(x['completed_at']), reverse=True)
    recent_done = done[:20]

    return render_template_string(
        INDEX_HTML,
        tags=tags,
        overdue=overdue,
        children_by_parent=children_by_parent,
        selectable_parents=selectable_parents,
        today=today_str(),
        chart_version=get_chart_version(),
        total_14d=total_14d,
        recent_done=recent_done,
        week_calendar=week_calendar,
        google_sync_available=google_sync_available(),
    )



@app.route('/chart_last_14.png')
def chart_last_14_png():
    png_bytes = get_chart_png_bytes()
    resp = Response(png_bytes, mimetype='image/png')
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp

@app.route('/chart_today_progress.png')
def chart_today_progress_png():
    with TASKS_LOCK:
        tasks = read_tasks()

    chart_b64 = chart_today_progress_png_b64(tasks)
    png_bytes = base64.b64decode(chart_b64)

    resp = Response(png_bytes, mimetype='image/png')
    resp.headers['Cache-Control'] = 'no-store, max-age=0'
    return resp

@app.route('/add', methods=['POST'])
def add():
    title = request.form.get('title', '').strip()
    if not title:
        return redirect(url_for('index'))

    tag = request.form.get('tag', 'マイタスク').strip() or 'マイタスク'
    score = sanitize_score(request.form.get('score', '30'))
    due_date = sanitize_due_date(request.form.get('due_date', today_str()))
    recur = sanitize_recur(request.form.get('recur', 'none'))
    parent_id = sanitize_parent_id(request.form.get('parent_id', ''))

    tags = read_tags()
    tag = auto_tag(title, tag, tags)

    with TASKS_LOCK:
        tasks = read_tasks()
        tid = next_task_id(tasks)
        new_task = {
            'id': tid,
            'title': title,
            'tag': tag,
            'score': score,
            'due_date': due_date,
            'completed': 0,
            'completed_at': '',
            'parent_id': parent_id,
            'recur': recur,
            'google_task_id': '',
            'sync_pending': 1 if GOOGLE_SYNC_ENABLED else 0
        }
        tasks.append(new_task)
        write_tasks(tasks)

    enqueue_task_sync(tid)
    return redirect(url_for('index'))

@app.route('/refresh_google', methods=['POST'])
def refresh_google():
    if google_sync_available():
        try:
            sync_google_to_local()
        except Exception:
            app.logger.exception('手動Google同期に失敗した')
    return redirect(url_for('index'))

@app.route('/complete/<int:task_id>', methods=['POST'])
def complete(task_id):
    now = dt.datetime.now().replace(microsecond=0)
    new_task_id = 0

    with TASKS_LOCK:
        tasks = read_tasks()

        for t in tasks:
            if t['id'] == task_id and t['completed'] == 0:
                t['completed'] = 1
                t['completed_at'] = now.isoformat(sep=' ')
                t['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0

                if t['recur'] == 'weekly':
                    next_due = (parse_date(t['due_date']) + dt.timedelta(days=7)).isoformat()
                elif t['recur'] == 'monthly':
                    next_due = add_months(t['due_date'], 1)
                else:
                    next_due = None

                if next_due:
                    new_task_id = next_task_id(tasks)
                    new_task = {
                        'id': new_task_id,
                        'title': t['title'],
                        'tag': t['tag'],
                        'score': t['score'],
                        'due_date': next_due,
                        'completed': 0,
                        'completed_at': '',
                        'parent_id': t['parent_id'],
                        'recur': t['recur'],
                        'google_task_id': '',
                        'sync_pending': 1 if GOOGLE_SYNC_ENABLED else 0
                    }
                    tasks.append(new_task)
                break

        write_tasks(tasks)

    enqueue_task_sync(task_id)
    if new_task_id:
        enqueue_task_sync(new_task_id)

    return redirect(url_for('index'))

@app.route('/reschedule/<int:task_id>', methods=['POST'])
def reschedule(task_id):
    new_due = sanitize_due_date(request.form.get('new_due_date', today_str()))

    with TASKS_LOCK:
        tasks = read_tasks()

        for t in tasks:
            if t['id'] == task_id and t['completed'] == 0:
                t['due_date'] = new_due
                t['score'] = to_int(t['score'], 0) + 30
                t['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0
                break

        write_tasks(tasks)

    enqueue_task_sync(task_id)
    return redirect(url_for('index'))


# --- 追加: タスク削除（自分＋子孫を再帰的に削除） ---
@app.route('/delete/<int:task_id>', methods=['POST'])
def delete(task_id):
    with TASKS_LOCK:
        tasks = read_tasks()

        to_delete = set([task_id])
        changed = True
        while changed:
            changed = False
            for t in tasks:
                pid = t.get('parent_id', '')
                if pid and str(pid).isdigit() and int(pid) in to_delete and t['id'] not in to_delete:
                    to_delete.add(t['id'])
                    changed = True

        delete_google_ids = [
            t.get('google_task_id', '')
            for t in tasks
            if t['id'] in to_delete and t.get('google_task_id')
        ]

        tasks = [t for t in tasks if t['id'] not in to_delete]
        write_tasks(tasks)

    for gid in delete_google_ids:
        enqueue_google_delete(gid)

    return redirect(url_for('index'))

# --- 追加: 完了取り消し（未完了に戻す） ---
@app.route('/undo/<int:task_id>', methods=['POST'])
def undo(task_id):
    with TASKS_LOCK:
        tasks = read_tasks()
        for t in tasks:
            if t['id'] == task_id:
                t['completed'] = 0
                t['completed_at'] = ''
                t['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0
                break
        write_tasks(tasks)

    enqueue_task_sync(task_id)
    return redirect(url_for('index'))

@app.route('/tags')
def tags_page():
    tags = read_tags()
    return render_template_string(TAGS_HTML, tags=tags)

@app.route('/tags/add', methods=['POST'])
def add_tag():
    new_tag = request.form.get('new_tag', '').strip()
    if new_tag:
        tags = read_tags()
        if new_tag not in tags:
            tags.append(new_tag)
            write_tags(tags)
    return redirect(url_for('tags_page'))

@app.route('/tags/delete', methods=['POST'])
def delete_tag():
    tag = request.form.get('tag', '')
    if tag and tag != 'マイタスク':
        tags = read_tags()
        tags = [t for t in tags if t != tag]
        if 'マイタスク' not in tags:
            tags.insert(0, 'マイタスク')
        write_tags(tags)
        # 紐づくタスクは「マイタスク」へ移行
        with TASKS_LOCK:
            tasks = read_tasks()
            changed = False
            for t in tasks:
                if t['tag'] == tag:
                    t['tag'] = 'マイタスク'
                    changed = True
            if changed:
                write_tasks(tasks)
    return redirect(url_for('tags_page'))


@app.route('/update_meta/<int:task_id>', methods=['POST'])
def update_meta(task_id):
    new_tag = (request.form.get('tag') or 'マイタスク').strip() or 'マイタスク'
    new_parent_id = (request.form.get('parent_id') or '').strip()

    tags = read_tags()

    if new_tag not in tags:
        new_tag = 'マイタスク'

    with TASKS_LOCK:
        tasks = read_tasks()

        active = [t for t in tasks if t['completed'] == 0]
        active_ids = {str(t['id']) for t in active}

        children_by_parent = {}
        for t in active:
            pid = t['parent_id'] if t['parent_id'] in active_ids else ''
            children_by_parent.setdefault(pid, []).append(t)

        forbidden = {task_id}
        stack = [str(task_id)]
        while stack:
            pid = stack.pop()
            for ch in children_by_parent.get(pid, []):
                cid = ch['id']
                if cid not in forbidden:
                    forbidden.add(cid)
                    stack.append(str(cid))

        if not (new_parent_id and new_parent_id.isdigit() and new_parent_id in active_ids):
            new_parent_id = ''
        elif int(new_parent_id) in forbidden:
            new_parent_id = ''

        for t in tasks:
            if t['id'] == task_id and t['completed'] == 0:
                t['tag'] = new_tag
                t['parent_id'] = new_parent_id
                break

        write_tasks(tasks)

    return redirect(url_for('index'))

@app.route('/edit/<int:task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    tags = read_tags()

    with TASKS_LOCK:
        tasks = read_tasks()

    task = None
    for t in tasks:
        if t['id'] == task_id:
            task = t
            break

    if (not task) or int(task.get('completed', 0)) == 1:
        return redirect(url_for('index'))

    active = [t for t in tasks if int(t.get('completed', 0)) == 0]
    active_ids = {str(t['id']) for t in active}

    children_by_parent = {}
    for t in active:
        pid = t.get('parent_id', '')
        if pid not in active_ids:
            pid = ''
        children_by_parent.setdefault(pid, []).append(t)

    forbidden = {task_id}
    stack = [str(task_id)]
    while stack:
        pid = stack.pop()
        for ch in children_by_parent.get(pid, []):
            cid = ch['id']
            if cid not in forbidden:
                forbidden.add(cid)
                stack.append(str(cid))

    parent_candidates = sorted(
        [t for t in active if t['id'] not in forbidden],
        key=lambda x: (parse_date(x['due_date']), -x['id'])
    )

    current_parent = task.get('parent_id', '')
    if current_parent in active_ids and current_parent.isdigit():
        current_parent_id = int(current_parent)
    else:
        current_parent_id = None

    if request.method == 'POST':
        new_tag = (request.form.get('tag') or 'マイタスク').strip() or 'マイタスク'
        if new_tag not in tags:
            new_tag = 'マイタスク'
    
        new_parent_id = (request.form.get('parent_id') or '').strip()
        if not (new_parent_id and new_parent_id.isdigit() and new_parent_id in active_ids and int(new_parent_id) not in forbidden):
            new_parent_id = ''
    
        new_due_date = sanitize_due_date(
            request.form.get('due_date') or task.get('due_date') or today_str()
        )
    
        with TASKS_LOCK:
            tasks = read_tasks()
            for current in tasks:
                if current['id'] == task_id:
                    current['tag'] = new_tag
                    current['parent_id'] = new_parent_id
                    current['due_date'] = new_due_date
                    current['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0
                    break
            write_tasks(tasks)
    
        enqueue_task_sync(task_id)
    
        return redirect(url_for('index'))

    return render_template_string(
        EDIT_HTML,
        task=task,
        tags=tags,
        parent_candidates=parent_candidates,
        current_parent_id=current_parent_id
    )

if __name__ == '__main__':
    ensure_files()
    app.run(debug=True)
