# -*- coding: utf-8 -*-
from flask import Flask, render_template_string, request, redirect, url_for, Response, jsonify
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
    'id', 'title', 'tag', 'score', 'base_score',
    'extension_count', 'link_bonus_awarded', 'sort_order', 'due_date',
    'completed', 'completed_at', 'parent_id', 'recur',
    'google_task_id', 'sync_pending'
]

GOOGLE_TASKLIST_TITLE = os.environ.get('GOOGLE_TASKLIST_TITLE', 'TODO同期')
GOOGLE_CREDENTIALS_JSON = os.environ.get(
    'GOOGLE_CREDENTIALS_JSON',
    os.path.join(CRED_DIR, 'credentials.json')
)
GOOGLE_TOKEN_JSON = os.path.join(CRED_DIR, 'google_token.json')
GOOGLE_SCOPES = ['https://www.googleapis.com/auth/tasks']
CODEX_TASK_API_TOKEN = os.environ.get('CODEX_TASK_API_TOKEN', '').strip()

def google_sync_enabled_from_setting(setting):
    return (setting or '0').strip().lower() in ('1', 'true', 'yes', 'on')


GOOGLE_SYNC_ENABLED = google_sync_enabled_from_setting(
    os.environ.get('GOOGLE_SYNC_ENABLED', '0')
)

VALID_SCORES = {30, 60, 100}
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

            sort_order_raw = (row.get('sort_order') or '').strip()
            task = {
                'id': task_id,
                'title': (row.get('title') or '').strip(),
                'tag': (row.get('tag') or 'マイタスク').strip() or 'マイタスク',
                'score': to_int(row.get('score'), 0),
                'base_score': to_int(row.get('base_score'), to_int(row.get('score'), 0)),
                'extension_count': max(to_int(row.get('extension_count'), 0), 0),
                'link_bonus_awarded': 1 if to_int(row.get('link_bonus_awarded'), 0) else 0,
                'sort_order': to_int(sort_order_raw, 0),
                '_sort_order_missing': not bool(sort_order_raw),
                'due_date': sanitize_due_date(row.get('due_date')),
                'completed': 1 if to_int(row.get('completed'), 0) else 0,
                'completed_at': (row.get('completed_at') or '').strip(),
                'parent_id': sanitize_parent_id(row.get('parent_id')),
                'recur': sanitize_recur(row.get('recur', 'none')),
                'google_task_id': (row.get('google_task_id') or '').strip(),
                'sync_pending': 1 if to_int(row.get('sync_pending'), 0) else 0
            }
            tasks.append(task)

    tasks_by_parent = {}
    for task in tasks:
        tasks_by_parent.setdefault(task['parent_id'], []).append(task)

    for siblings in tasks_by_parent.values():
        missing = [task for task in siblings if task['_sort_order_missing']]
        if not missing:
            continue

        if len(missing) == len(siblings):
            ordered_missing = sorted(
                missing,
                key=lambda task: (parse_date(task['due_date']), -task['id'])
            )
            next_order = 10
        else:
            ordered_missing = sorted(
                missing,
                key=lambda task: (parse_date(task['due_date']), -task['id'])
            )
            next_order = max(
                [task['sort_order'] for task in siblings if not task['_sort_order_missing']],
                default=0
            ) + 10

        for task in ordered_missing:
            task['sort_order'] = next_order
            next_order += 10

    for task in tasks:
        task.pop('_sort_order_missing', None)

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
                'base_score': t.get('base_score', t['score']),
                'extension_count': t.get('extension_count', 0),
                'link_bonus_awarded': t.get('link_bonus_awarded', 0),
                'sort_order': t.get('sort_order', t['id'] * 10),
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

def next_sibling_sort_order(tasks, parent_id, due_date=None, exclude_task_id=None):
    parent_id = sanitize_parent_id(str(parent_id) if parent_id is not None else '')
    sibling_orders = [
        to_int(task.get('sort_order'), task['id'] * 10)
        for task in tasks
        if (
            task.get('parent_id', '') == parent_id
            and (due_date is None or task.get('due_date') == due_date)
            and task['id'] != exclude_task_id
        )
    ]
    return max(sibling_orders, default=0) + 10

def first_sibling_sort_order(tasks, parent_id, due_date, exclude_task_id=None):
    parent_id = sanitize_parent_id(str(parent_id) if parent_id is not None else '')
    sibling_orders = [
        to_int(task.get('sort_order'), task['id'] * 10)
        for task in tasks
        if (
            task.get('parent_id', '') == parent_id
            and task.get('due_date') == due_date
            and task['id'] != exclude_task_id
        )
    ]
    return min(sibling_orders, default=20) - 10

def task_sort_key(task):
    return (
        parse_date(task['due_date']),
        to_int(task.get('sort_order'), task['id'] * 10),
        task['id']
    )


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
    default = to_int(default, 30)
    if default not in VALID_SCORES:
        default = 30
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

def set_task_base_score(task, new_base_score):
    new_base_score = sanitize_score(new_base_score)
    old_base_score = to_int(task.get('base_score'), to_int(task.get('score'), 0))
    task['score'] = to_int(task.get('score'), 0) - old_base_score + new_base_score
    task['base_score'] = new_base_score

def apply_link_bonuses(tasks):
    """
    親・子の直接リンクを1本ずつ数え、4本以上になったタスクへ
    初回だけ1000点を加算する。完了済みタスクとのリンクも数えるため、
    実質的に累計に近い挙動になる。
    """
    tasks_by_id = {t['id']: t for t in tasks}
    link_counts = {t['id']: 0 for t in tasks}

    for child in tasks:
        parent_id = to_int(child.get('parent_id'), 0)
        if parent_id <= 0 or parent_id == child['id'] or parent_id not in tasks_by_id:
            continue
        link_counts[child['id']] += 1
        link_counts[parent_id] += 1

    awarded_ids = []
    for task in tasks:
        task['link_count'] = link_counts.get(task['id'], 0)
        if task['link_count'] < 4 or task.get('link_bonus_awarded', 0):
            continue
        task['score'] = to_int(task.get('score'), 0) + 1000
        task['link_bonus_awarded'] = 1
        task['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0
        awarded_ids.append(task['id'])

    return awarded_ids

def annotate_effective_scores(tasks):
    """
    自分の点数に、完了した直接の子タスクの実効点を加える。
    子側の実効点にも完了済みの子が含まれるため、階層的に積み上がる。
    """
    tasks_by_id = {task['id']: task for task in tasks}
    children_by_parent = {}

    for child in tasks:
        parent_id = to_int(child.get('parent_id'), 0)
        if parent_id <= 0 or parent_id == child['id'] or parent_id not in tasks_by_id:
            continue
        children_by_parent.setdefault(parent_id, []).append(child)

    memo = {}

    def effective_score(task_id, path):
        if task_id in memo:
            return memo[task_id]

        task = tasks_by_id[task_id]
        own_score = to_int(task.get('score'), 0)
        if task_id in path:
            return own_score

        child_score = 0
        next_path = path | {task_id}
        for child in children_by_parent.get(task_id, []):
            if child.get('completed') == 1:
                child_score += effective_score(child['id'], next_path)

        total = own_score + child_score
        task['own_score'] = own_score
        task['completed_children_score'] = child_score
        task['effective_score'] = total
        memo[task_id] = total
        return total

    for task in tasks:
        effective_score(task['id'], set())

    return memo

def task_effective_score(task):
    return to_int(task.get('effective_score'), to_int(task.get('score'), 0))

def collect_descendant_rows(root_task_id, tasks):
    children_by_parent = {}
    for task in tasks:
        parent_id = to_int(task.get('parent_id'), 0)
        if parent_id > 0:
            children_by_parent.setdefault(parent_id, []).append(task)

    for children in children_by_parent.values():
        children.sort(key=task_sort_key)

    rows = []

    def walk(parent_id, depth, path):
        for child in children_by_parent.get(parent_id, []):
            if child['id'] in path:
                continue
            rows.append({'task': child, 'depth': depth})
            walk(child['id'], depth + 1, path | {child['id']})

    walk(root_task_id, 0, {root_task_id})
    return rows

def parent_candidates_for_task(tasks, task_id):
    active = [task for task in tasks if task['completed'] == 0]
    active_ids = {str(task['id']) for task in active}
    forbidden_ids = {
        task_id,
        *[
            row['task']['id']
            for row in collect_descendant_rows(task_id, tasks)
        ]
    }
    candidates = sorted(
        [task for task in active if task['id'] not in forbidden_ids],
        key=task_sort_key
    )

    task = next((item for item in tasks if item['id'] == task_id), None)
    current_parent_id = None
    if task:
        current_parent = task.get('parent_id', '')
        if current_parent in active_ids and current_parent.isdigit():
            current_parent_id = int(current_parent)

    return candidates, current_parent_id

def task_sync_signature(task):
    return (
        task.get('title', ''),
        task.get('due_date', ''),
        1 if task.get('completed') else 0,
        task.get('completed_at', '')
    )

def score_total_last_14_days(tasks):
    annotate_effective_scores(tasks)
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
            total += task_effective_score(t)

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

# ---------- スコア集計＆折れ線描画 ----------
def chart_last_14_days_png_b64(tasks):
    annotate_effective_scores(tasks)
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
                    s += task_effective_score(t)
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
    ax1.set_title('過去14日のスコア')
    ax1.set_xlabel('日付')
    ax1.set_ylabel('点')
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
            sc = task_effective_score(t)
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
    ax2.set_ylabel('点（積み上げ）')
    ax2.set_title('昨日と今日')

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
    annotate_effective_scores(tasks)
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
            '今日はまだ完了タスクがありません',
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
            scores.append(max(task_effective_score(t), 0))

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
        ax.text(
            max_total * 1.45,
            0,
            f'合計 {max_total} 点',
            ha='left',
            va='center',
            fontsize=28,
            fontweight='bold'
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
        bottom=0.15
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
                    'base_score': 30,
                    'extension_count': 0,
                    'link_bonus_awarded': 0,
                    'sort_order': next_sibling_sort_order(tasks, '', due_date),
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TODO</title>
<style>
:root {
  color-scheme: light;
  --bg: #f4f6fb;
  --surface: #ffffff;
  --surface-soft: #f8fafc;
  --text: #172033;
  --muted: #667085;
  --line: #e4e7ec;
  --primary: #405cf5;
  --primary-dark: #2f46d3;
  --danger: #c93636;
  --danger-soft: #fff1f1;
  --shadow: 0 10px 28px rgba(23, 32, 51, .07);
}
* { box-sizing: border-box; }
body {
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans JP", "Hiragino Kaku Gothic ProN", Meiryo, sans-serif;
  margin: 0;
  padding: 28px;
  color: var(--text);
  background: var(--bg);
}
.page-shell { width: min(1440px, 100%); margin: 0 auto; }
section { margin-bottom: 20px; }
h1 { margin: 2px 0 6px; font-size: clamp(1.65rem, 2.8vw, 2.35rem); letter-spacing: -.035em; }
h2 { margin: 0; font-size: 1.08rem; }
h3 { margin: 0 0 12px; font-size: .98rem; }
small, .muted { color: var(--muted); }
a { color: var(--primary-dark); }
.topbar {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  align-items: flex-start;
  margin-bottom: 18px;
}
.eyebrow {
  margin: 0;
  color: var(--primary);
  font-size: .72rem;
  font-weight: 800;
  letter-spacing: .15em;
}
.subtitle { margin: 0; color: var(--muted); }
.nav-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.nav-link {
  display: inline-flex;
  align-items: center;
  min-height: 38px;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  color: var(--text);
  background: var(--surface);
  text-decoration: none;
  font-size: .9rem;
  font-weight: 650;
}
.nav-link:hover { border-color: #c8cfdb; background: var(--surface-soft); }
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}
.summary-card {
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface);
  box-shadow: 0 5px 16px rgba(23, 32, 51, .04);
}
.summary-label { display: block; color: var(--muted); font-size: .8rem; }
.summary-value { display: block; margin-top: 2px; font-size: 1.5rem; line-height: 1.1; }
.summary-card.is-alert .summary-value { color: var(--danger); }
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.section-head p { margin: 0; color: var(--muted); font-size: .86rem; }
input, select, button { font: inherit; }
input[type=text], input[type=search], input[type=date], select {
  min-height: 40px;
  padding: 8px 10px;
  border: 1px solid #cfd5df;
  border-radius: 9px;
  color: var(--text);
  background: var(--surface);
}
input:focus, select:focus, button:focus-visible, a:focus-visible {
  outline: 3px solid rgba(64, 92, 245, .18);
  outline-offset: 1px;
  border-color: var(--primary);
}
button, input[type=submit] {
  min-height: 38px;
  padding: 7px 12px;
  border: 1px solid #cfd5df;
  border-radius: 9px;
  color: var(--text);
  background: var(--surface);
  cursor: pointer;
  font-weight: 650;
}
button:hover, input[type=submit]:hover { background: var(--surface-soft); }
.btn-primary {
  border-color: var(--primary);
  color: #fff;
  background: var(--primary);
}
.btn-primary:hover { background: var(--primary-dark); }
.task-add-submit,
.task-add-submit:hover {
  border-color: #000 !important;
  color: #fff !important;
  background: #000 !important;
  font-weight: 850;
}
.btn-danger {
  min-width: 38px;
  padding: 6px 9px;
  border-color: #f2c5c5;
  color: var(--danger);
  background: var(--danger-soft);
}
.btn-complete {
  min-width: 38px;
  padding: 6px 9px;
  border-color: #b8dec7;
  color: #147a3d;
  background: #eefaf2;
}
ul.tree, ul.tree ul {
  list-style: none;
  margin: 0;
  padding-left: 16px;
  border-left: 1px solid #d9deea;
}
ul.tree { padding-left: 0; border-left: 0; }
li.task { margin: 4px 0; }
li.task[hidden] { display: none; }
li.task-date-gap {
  height: 12px;
}
.task-row{
  position: relative;
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 48px;
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: 10px;
  cursor: grab;
}
.task-row:active { cursor: grabbing; }
.task-row:hover {
  border-color: var(--line);
  background: var(--surface-soft);
}
.task-title{
  flex: 1 1 260px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: inherit;
  text-decoration: none;
}
.task-title:hover { color: var(--primary-dark); text-decoration: underline; }
.drag-handle {
  flex: 0 0 auto;
  display: inline-flex;
  gap: 3px;
  place-items: center;
  align-items: center;
  justify-content: center;
  min-width: 54px;
  min-height: 38px;
  padding: 0 7px;
  border: 1px solid #172033;
  border-radius: 8px;
  color: #fff;
  background: #172033;
  cursor: grab;
  user-select: none;
  font-size: .78rem;
  font-weight: 850;
  line-height: 1;
  white-space: nowrap;
  touch-action: none;
}
.drag-handle:hover { color: #fff; background: #2f3a50; }
.drag-handle:active { cursor: grabbing; }
li.task.dragging > .task-row { opacity: .35; border-style: dashed; }
.task-order-gap {
  position: relative;
  height: 0;
  margin: 0;
  padding: 0;
  overflow: visible;
}
#task-tree.is-order-dragging > .task-order-gap::before {
  content: "";
  position: absolute;
  z-index: 3;
  top: -4px;
  right: 0;
  left: 0;
  height: 8px;
  background: transparent;
  pointer-events: auto;
}
.task-order-gap::after {
  content: "";
  position: absolute;
  inset: 50% 0 auto;
  height: 1px;
  background: transparent;
  transform: translateY(-50%);
}
.task-order-gap.is-order-target::after {
  height: 3px;
  background: var(--primary);
}
li.task.drop-as-parent > .task-row {
  border-color: var(--primary);
  background: #eef1ff;
  box-shadow: inset 0 0 0 1px var(--primary);
}
li.task.drop-as-parent > .task-row::after {
  content: attr(data-drop-label);
  position: absolute;
  left: 50%;
  top: 50%;
  z-index: 2;
  padding: 2px 7px;
  color: #fff;
  background: #172033;
  font-size: .78rem;
  font-weight: 850;
  white-space: nowrap;
  pointer-events: none;
  transform: translate(-50%, -50%);
}
.detach-parent-drop {
  display: none;
  position: fixed;
  left: 50%;
  bottom: 16px;
  z-index: 20;
  min-width: 260px;
  padding: 7px 14px;
  border: 2px dashed var(--danger);
  color: var(--danger);
  background: #fff;
  font-weight: 850;
  text-align: center;
  transform: translateX(-50%);
}
.detach-parent-drop.is-visible { display: block; }
.detach-parent-drop.is-active {
  color: #fff;
  background: var(--danger);
}
.task-row .badge{
  flex: 0 0 auto;
  white-space: nowrap;
}
a.btn-edit{
  flex: 0 0 auto;
  display: inline-grid;
  place-items: center;
  min-width: 38px;
  min-height: 38px;
  padding: 5px;
  border: 1px solid #cfd5df;
  border-radius: 9px;
  text-decoration: none;
  color: var(--text);
  background: var(--surface);
}
a.btn-edit:hover{ background: var(--surface-soft); }

.badge {
  display: inline-block;
  padding: 3px 7px;
  border-radius: 999px;
  font-size: .85em;
  border: 1px solid var(--line);
  background: var(--surface-soft);
  color: var(--text);
}
.badge-tag {
  background: #f0f2f7;
  border-color: #e0e4ec;
  color: #566074;
}
.badge-overdue {
  background: var(--danger-soft);
  color: var(--danger);
  border-color: #f2c5c5;
}
.badge-score-low  { background:#e0f3ff; border-color:#b3e0ff; }   /* 〜49 */
.badge-score-mid  { background:#fff4c4; border-color:#ffe08a; }   /* 50〜79 */
.badge-score-high { background:#ffd7d7; border-color:#ffb3b3; }   /* 80〜99 */
.badge-score-max {
  background: linear-gradient(135deg, #ffd700, #ffea8a);
  color: #503000;
  font-weight: bold;
  border: 1px solid #c9a200;
}
.badge-score-bonus {
  background: linear-gradient(135deg, #ebe2ff, #d9f4ff);
  color: #4a278f;
  font-weight: bold;
  border-color: #bca9ed;
}
.badge-link-bonus {
  background: #f3edff;
  color: #6336a8;
  border-color: #d5c3f4;
}
.row { display: flex; gap: 20px; flex-wrap: wrap; }
.card {
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 18px;
  background: var(--surface);
  box-shadow: var(--shadow);
}
.table-wrap { max-width: 100%; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
td, th { padding: 9px 8px; border-bottom: 1px solid #edf0f4; text-align: left; }
th { color: var(--muted); font-size: .8rem; font-weight: 700; }
.form-inline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.field-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(150px, .55fr);
  gap: 12px;
}
.field { display: grid; gap: 6px; }
.field > label { color: #475467; font-size: .82rem; font-weight: 700; }
.field-wide { grid-column: 1 / -1; }
.score-choices { display: flex; gap: 6px; flex-wrap: wrap; }
.score-choices label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 8px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
  cursor: pointer;
  font-size: .86rem;
}
.quick-score-buttons { display: flex; gap: 0; flex-wrap: wrap; }
.quick-score-button {
  min-width: 48px;
  color: var(--text);
  background: var(--surface);
}
.quick-score-button[aria-pressed="true"] {
  color: #fff;
  background: #172033;
  border-color: #172033;
  font-weight: 850;
}
.advanced-options {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface-soft);
}
.advanced-options summary { cursor: pointer; color: #475467; font-size: .86rem; font-weight: 700; }
.advanced-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-top: 10px;
}
.form-actions { display: flex; align-items: center; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
.form-note { color: var(--muted); font-size: .78rem; }
.filters {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(140px, .42fr) minmax(140px, .42fr) auto;
  gap: 8px;
  margin-bottom: 12px;
}
.filters input, .filters select { width: 100%; }
.empty-state {
  margin: 12px 0 0;
  padding: 18px;
  border: 1px dashed #cfd5df;
  border-radius: 10px;
  color: var(--muted);
  text-align: center;
  background: var(--surface-soft);
}
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.overdue-list { display: grid; gap: 2px; }
.overdue-item {
  display: grid;
  grid-template-columns: minmax(200px, 1fr) auto auto auto auto;
  gap: 6px;
  align-items: center;
  padding: 3px 6px;
  border-radius: 6px;
  background: #fffafa;
}
.overdue-item input {
  min-height: 34px;
  padding: 4px 8px;
}

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
  flex: 0 0 480px;
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

@media (max-width: 980px) {
  body { padding: 18px; }
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .task-register-layout {
    flex-direction: column;
  }
  .task-register-card {
    flex: auto;
    width: 100%;
  }
  .task-chart-card {
    width: 100%;
  }
  .filters { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 640px) {
  body { padding: 12px; }
  .topbar { flex-direction: column; }
  .summary-grid { gap: 8px; }
  .summary-card { padding: 12px; }
  .field-grid, .advanced-grid, .filters { grid-template-columns: 1fr; }
  .card { padding: 14px; border-radius: 13px; }
  .section-head { display: block; }
  .section-head p { margin-top: 6px; overflow-wrap: anywhere; }
  .task-row { flex-wrap: wrap; }
  .task-title { flex-basis: calc(100% - 100px); }
  .task-row .badge { font-size: .76rem; }
  .overdue-item { grid-template-columns: 1fr; }
  .overdue-item strong { grid-column: auto; }
  .overdue-item input, .overdue-item button { width: 100%; }
  .overdue-item .badge { justify-self: start; }
}

/* 余白ゼロ基準の高密度表示 */
body { padding: clamp(12px, 1.5vw, 24px); }
.page-shell { width: 100%; margin: 0; }
section, h1, h2, h3, p { margin: 0; }
h1, h2, h3, p, span, strong, a, label { line-height: 1.15; }
.summary-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0;
  margin: 0;
}
.summary-card {
  display: flex;
  align-items: baseline;
  gap: 0;
  padding: 3px 10px;
  border-radius: 0;
  box-shadow: none;
}
.summary-label { display: inline; font-size: .72rem; }
.summary-value { display: inline; margin: 0; font-size: 1.05rem; line-height: 1; }
.section-head { gap: 0; margin: 0; }
.section-head p { margin: 0; }
.nav-actions, .row, .form-inline, .score-choices, .form-actions { gap: 0; }
.nav-link {
  min-height: 26px;
  padding: 3px 10px;
  border-radius: 0;
}
.card {
  padding: 3px 10px;
  border-radius: 0;
  box-shadow: none;
}
input[type=text], input[type=search], input[type=date], select,
button, input[type=submit] {
  min-height: 26px;
  padding: 3px 10px;
  border-radius: 0;
}
.btn-danger, .btn-complete {
  min-width: 26px;
  padding: 3px 6px;
}
ul.tree, ul.tree ul {
  margin: 0;
  padding-left: 40px;
}
ul.tree { padding-left: 0; }
li.task { margin: 0; }
li.task-date-gap { height: 0; }
.task-row {
  gap: 0;
  min-height: 26px;
  padding: 3px 10px;
  border-radius: 0;
}
.drag-handle {
  width: auto;
  min-width: 48px;
  min-height: 26px;
  padding: 0 5px;
  border-radius: 0;
  font-size: .72rem;
}
a.btn-edit {
  min-width: 26px;
  min-height: 26px;
  padding: 3px 6px;
  border-radius: 0;
}
.badge {
  padding: 1px 4px;
  border-radius: 0;
  line-height: 1.15;
}
.row { gap: 0; }
td, th { padding: 3px 10px; }
.field-grid, .advanced-grid { gap: 0; }
.field { gap: 0; }
.score-choices label { gap: 0; padding: 3px 10px; border-radius: 0; }
.advanced-options {
  margin: 0;
  padding: 3px 10px;
  border-radius: 0;
}
.advanced-grid, .form-actions { margin: 0; }
.empty-state {
  margin: 0;
  padding: 3px 10px;
  border-radius: 0;
}
.overdue-list { gap: 0; }
.overdue-item {
  gap: 0;
  padding: 3px 10px;
  border-radius: 0;
}
.overdue-item input { min-height: 26px; padding: 3px 10px; }
.task-register-layout { gap: 0; margin: 0; }
.task-register-layout > section { margin: 0; }
.task-chart-card { padding: 3px 10px; }
.summary-card, .nav-link, .card,
input[type=text], input[type=search], input[type=date], select,
button, input[type=submit], .task-row, a.btn-edit,
td, th, .score-choices label, .advanced-options,
.empty-state, .overdue-item, .task-chart-card {
  padding-block: 3px;
}
.score-total-14d {
  color: #fff;
  background: #172033;
  font-size: 1.35rem;
  font-weight: 900;
  line-height: 1;
}
</style>

<body>
<main class="page-shell">
<section class="summary-grid" aria-label="タスク概要">
  <div class="summary-card">
    <span class="summary-label">未完了</span>
    <strong class="summary-value">{{ task_summary.open }}</strong>
  </div>
  <div class="summary-card">
    <span class="summary-label">今日が期限</span>
    <strong class="summary-value">{{ task_summary.today }}</strong>
  </div>
  <div class="summary-card {% if task_summary.overdue %}is-alert{% endif %}">
    <span class="summary-label">期限超過</span>
    <strong class="summary-value">{{ task_summary.overdue }}</strong>
  </div>
  <div class="summary-card">
    <span class="summary-label">過去14日スコア</span>
    <strong class="summary-value">{{ total_14d }}</strong>
  </div>
</section>

{% if overdue %}
<section class="card">
  <div class="section-head">
    <h2>期限超過</h2>
    <p>延長するたび +30、+60、+90…と加点が増えます</p>
  </div>
  <div class="overdue-list">
  {% for t in overdue %}
  <form class="overdue-item" method="post" action="{{ url_for('reschedule', task_id=t['id']) }}">
    <strong>{{ t['title'] }}</strong>
    <span class="badge badge-overdue">期限: {{ t['due_date'] }}</span>
    <span class="badge">次の延長: +{{ 30 * (t['extension_count'] + 1) }}点</span>
    <label class="visually-hidden" for="overdue-due-{{ t['id'] }}">新しい期日</label>
    <input id="overdue-due-{{ t['id'] }}" type="date" name="new_due_date" value="{{ today }}">
    <input class="btn-primary" type="submit" value="再設定">
  </form>
  {% endfor %}
  </div>
</section>
{% endif %}

<div class="task-register-layout">

  <section class="card task-register-card">
    <div class="section-head">
      <h2>タスク登録</h2>
      <a class="nav-link" href="{{ url_for('tags_page') }}">タグ管理</a>
    </div>

    <form id="task-add-form" method="post" action="{{ url_for('add') }}">
      <div class="field-grid">
        <div class="field field-wide">
          <label for="new-title">タイトル</label>
          <input id="new-title" type="text" name="title" autocomplete="off" placeholder="次にやることを入力" required>
        </div>
        <div class="field field-wide">
          <label>基本点</label>
          <input id="quick-score-value" type="hidden" name="score" value="30">
          <div class="quick-score-buttons" role="group" aria-label="基本点を選択">
            {% for s in [30,60,100] %}
              <button
                class="quick-score-button"
                type="button"
                data-score="{{ s }}"
                aria-pressed="{{ 'true' if s == 30 else 'false' }}"
              >{{ s }}</button>
            {% endfor %}
          </div>
        </div>
        <div class="field">
          <label for="new-due-date">期日</label>
          <input id="new-due-date" type="date" name="due_date" value="{{ today }}">
        </div>
        <div class="field">
          <label for="new-tag">タグ</label>
          <select id="new-tag" name="tag">
            {% for tg in tags %}
              <option value="{{ tg }}">{{ tg }}</option>
            {% endfor %}
          </select>
        </div>
      </div>

      <details class="advanced-options" open>
        <summary>詳細設定（定期・親タスク）</summary>
        <div class="advanced-grid">
          <div class="field">
            <label for="new-recur">繰り返し</label>
            <select id="new-recur" name="recur">
              <option value="none">なし</option>
              <option value="weekly">毎週</option>
              <option value="monthly">毎月</option>
            </select>
          </div>
          <div class="field">
            <label for="new-parent">親タスク</label>
            <select id="new-parent" name="parent_id">
              <option value="">なし</option>
              {% for p in selectable_parents %}
                <option value="{{ p['id'] }}">{{ p['title'] }}</option>
              {% endfor %}
            </select>
          </div>
        </div>
      </details>

      <div class="form-actions">
        <input class="btn-primary task-add-submit" type="submit" value="タスクを追加">
        <span class="form-note">直接の親子紐付けが4本になると、そのタスクへ一度だけ+1000点</span>
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
  <div class="section-head">
    <h2>未完了タスク</h2>
    <p>{{ task_summary.open }}件</p>
  </div>
  <p id="reorder-status" class="visually-hidden" aria-live="polite"></p>
  <div id="detach-parent-drop" class="detach-parent-drop" aria-hidden="true">
    ここへ落とすと親から外す
  </div>
  <div class="row">
    <!-- 左：ツリー -->
    <div style="flex:2; min-width: 260px;">
      <ul class="tree" id="task-tree" data-parent-id="">
        {% macro render_children(pid) %}
          {% for t in children_by_parent.get(pid, []) %}
          {% if pid == '' %}
          <li
            class="task-order-gap"
            data-before-task-id="{{ t['id'] }}"
            data-before-due-date="{{ t['due_date'] }}"
            {% if not loop.first %}
            data-after-task-id="{{ loop.previtem['id'] }}"
            data-after-due-date="{{ loop.previtem['due_date'] }}"
            {% endif %}
            aria-hidden="true"
          ></li>
          {% endif %}
         <li
           class="task"
           data-task-id="{{ t['id'] }}"
           data-parent-id="{{ t['parent_id'] }}"
           data-parent-url="{{ url_for('set_task_parent', task_id=t['id']) }}"
           data-detail-url="{{ url_for('task_detail', task_id=t['id']) }}"
            data-title="{{ t['title'] }}"
            data-tag="{{ t['tag'] }}"
            data-due-date="{{ t['due_date'] }}"
            data-due-scope="{{ 'overdue' if t['is_overdue'] else ('today' if t['due_date'] == today else 'future') }}"
          >
           <div class="task-row">
             <span class="drag-handle" draggable="true" title="ここをドラッグして移動" aria-label="{{ t['title'] }}をドラッグして移動">↕ 移動</span>

            <form style="display:inline;" method="post" action="{{ url_for('complete', task_id=t['id']) }}">
              <button class="btn-complete" title="完了" aria-label="{{ t['title'] }}を完了">✔</button>
            </form>
        
            <form style="display:inline;" method="post"
                  action="{{ url_for('delete', task_id=t['id']) }}"
                  onsubmit="return confirm('このタスクと子タスクを削除します。よろしいですか？');">
              <button class="btn-danger" title="削除" aria-label="{{ t['title'] }}を削除">✖</button>
            </form>
        
            <a class="task-title" draggable="false" href="{{ url_for('task_detail', task_id=t['id']) }}" title="子タスクと詳細を表示">{{ t['title'] }}</a>
        
            <span class="badge badge-tag">{{ t['tag'] }}</span>
        
            {% set shown_score = t['effective_score'] %}
            {% set score_class =
                'badge-score-bonus' if t['link_bonus_awarded']
                else ('badge-score-max' if shown_score == 100
                else ('badge-score-high' if shown_score >= 80
                else ('badge-score-mid' if shown_score >= 50
                else 'badge-score-low')))
            %}
            <span class="badge {{ score_class }}">点: {{ shown_score }}</span>

            {% if t['completed_children_score'] %}
              <span class="badge badge-link-bonus">完了した子 +{{ t['completed_children_score'] }}</span>
            {% endif %}

            {% if t['extension_count'] %}
              <span class="badge">延長: {{ t['extension_count'] }}回</span>
            {% endif %}

            {% if t['link_count'] %}
              <span class="badge">紐付け: {{ t['link_count'] }}本</span>
            {% endif %}

            {% if t['link_bonus_awarded'] %}
              <span class="badge badge-link-bonus">4紐付け +1000</span>
            {% endif %}
        
            <span class="badge {% if t['is_overdue'] %}badge-overdue{% endif %}">
              期日: {{ t['due_date'] }}
            </span>
        
            {% if t['recur'] != 'none' %}
              <span class="badge">定期: {{ '毎週' if t['recur']=='weekly' else '毎月' }}</span>
            {% endif %}
        
            <a class="btn-edit" href="{{ url_for('edit_task', task_id=t['id']) }}" title="タスクを編集" aria-label="{{ t['title'] }}を編集">✎</a>
          </div>
        
          <ul data-parent-id="{{ t['id'] }}">
            {{ render_children(t['id_str']) }}
          </ul>
        </li>
          {% if pid == '' and loop.last %}
          <li
            class="task-order-gap"
            data-after-task-id="{{ t['id'] }}"
            data-after-due-date="{{ t['due_date'] }}"
            aria-hidden="true"
          ></li>
          {% endif %}
          {% endfor %}
        {% endmacro %}
        {{ render_children('') }}
      </ul>
      {% if not task_summary.open %}
        <p class="empty-state">未完了タスクはありません。気持ちよく空っぽです。</p>
      {% endif %}
    </div>

    <!-- 右：1週間カレンダー -->
    <div style="flex:1; min-width: 220px;">
      <h3>これから7日間</h3>
      <div class="table-wrap">
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
            <td>{{ d.date.strftime('%m/%d') }}（{{ d.weekday }}）</td>
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
  </div>
</section>


<section class="card">
  <div class="section-head">
    <h2>過去14日のスコア推移</h2>
    <p class="score-total-14d">合計 <strong>{{ total_14d }}</strong> 点</p>
  </div>
  <div><img style="max-width:100%; height:auto;" loading="lazy" alt="過去14日のスコア推移" src="{{ url_for('chart_last_14_png') }}?v={{ chart_version }}"></div>
</section>


{% if google_sync_available %}
<section class="card">
  <form method="post" action="{{ url_for('refresh_google') }}">
    <button type="submit">Googleから更新</button>
  </form>
</section>
{% endif %}

<section class="card">
  <div class="section-head">
    <h2>最近完了</h2>
    <p>直近20件</p>
  </div>
  {% if recent_done %}
  <div class="table-wrap">
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
        <td><a href="{{ url_for('task_detail', task_id=t['id']) }}">{{ t['title'] }}</a></td>
        {% set shown_score = t['effective_score'] %}
        {% set scls =
          'badge-score-bonus' if t['link_bonus_awarded']
          else ('badge-score-max' if shown_score == 100
          else ('badge-score-high' if shown_score >= 80
          else ('badge-score-mid' if shown_score >= 50
          else 'badge-score-low')))
        %}
        <td class="{{ scls }}" style="text-align:right">{{ shown_score }}</td>



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
  </div>
  {% else %}
    <p class="empty-state">完了したタスクはまだありません。</p>
  {% endif %}
</section>

<script>
(() => {
  const addForm = document.getElementById('task-add-form');
  const scoreValue = document.getElementById('quick-score-value');
  const scoreButtons = Array.from(document.querySelectorAll('.quick-score-button'));

  function selectQuickScore(button) {
    if (!scoreValue) return;
    scoreValue.value = button.dataset.score;
    scoreButtons.forEach((item) => {
      item.setAttribute('aria-pressed', item === button ? 'true' : 'false');
    });
  }

  scoreButtons.forEach((button) => {
    button.addEventListener('click', () => selectQuickScore(button));
    button.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' || !addForm) return;
      event.preventDefault();
      selectQuickScore(button);
      addForm.requestSubmit();
    });
  });

  const tree = document.getElementById('task-tree');
  const reorderStatus = document.getElementById('reorder-status');
  const detachParentDrop = document.getElementById('detach-parent-drop');
  if (!tree) return;

  let draggedTask = null;
  let draggedList = null;
  let dropMode = null;
  let orderReference = null;
  let parentTarget = null;
  let suppressRowClickUntil = 0;
  let pointerStart = null;
  const dragClickThreshold = 4;
  const dragClickSuppressMs = 350;

  function clearDropMarkers() {
    tree.querySelectorAll('.is-order-target').forEach((node) => {
      node.classList.remove('is-order-target');
    });
    tree.querySelectorAll('.drop-as-parent').forEach((node) => {
      node.classList.remove('drop-as-parent');
      const row = node.querySelector(':scope > .task-row');
      if (row) row.removeAttribute('data-drop-label');
    });
    detachParentDrop?.classList.remove('is-active');
  }

  function clearDropIntent() {
    dropMode = null;
    orderReference = null;
    parentTarget = null;
  }

  async function persistOrder(list, dueDate) {
    const orderedIds = Array.from(list.children)
      .filter((node) => (
        node.matches('li.task')
        && node.dataset.dueDate === dueDate
      ))
      .map((node) => Number(node.dataset.taskId));
    const response = await fetch('{{ url_for("reorder_tasks") }}', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        parent_id: '',
        due_date: dueDate,
        ordered_ids: orderedIds
      })
    });
    if (!response.ok) throw new Error('並び順を保存できませんでした');
  }

  async function persistParent(task, parentId) {
    const response = await fetch(task.dataset.parentUrl, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'},
      body: new URLSearchParams({parent_id: parentId})
    });
    if (!response.ok) {
      const message = (await response.text()).trim();
      throw new Error(message || '親子関係を保存できませんでした');
    }
  }

  function orderReferenceForGap(gap) {
    const dueDate = draggedTask?.dataset.dueDate;
    if (!dueDate || draggedList !== tree) return null;

    if (
      gap.dataset.beforeDueDate === dueDate
      && gap.dataset.beforeTaskId
    ) {
      return {position: 'before', taskId: gap.dataset.beforeTaskId};
    }
    if (
      gap.dataset.afterDueDate === dueDate
      && gap.dataset.afterTaskId
    ) {
      return {position: 'after', taskId: gap.dataset.afterTaskId};
    }
    return null;
  }

  function moveTaskToOrderReference(task, reference) {
    const referenceTask = Array.from(tree.children).find((node) => (
      node.matches('li.task')
      && node.dataset.taskId === reference.taskId
    ));
    if (!referenceTask) return false;

    if (reference.position === 'before') {
      tree.insertBefore(task, referenceTask);
    } else {
      tree.insertBefore(task, referenceTask.nextElementSibling);
    }
    return true;
  }

  function finishDrag() {
    clearDropMarkers();
    clearDropIntent();
    tree.classList.remove('is-dragging', 'is-order-dragging');
    detachParentDrop?.classList.remove('is-visible', 'is-active');
    detachParentDrop?.setAttribute('aria-hidden', 'true');
    if (draggedTask) draggedTask.classList.remove('dragging');
    draggedTask = null;
    draggedList = null;
  }

  function finishPointerTracking() {
    pointerStart = null;
  }

  tree.addEventListener('pointerdown', (event) => {
    const row = event.target.closest('.task-row');
    if (
      !row
      || event.button !== 0
      || event.target.closest('button, input, select, form')
    ) {
      pointerStart = null;
      return;
    }
    pointerStart = {
      x: event.clientX,
      y: event.clientY
    };
  });

  window.addEventListener('pointermove', (event) => {
    if (!pointerStart) return;
    const moved = Math.hypot(
      event.clientX - pointerStart.x,
      event.clientY - pointerStart.y
    );
    if (moved >= dragClickThreshold) {
      suppressRowClickUntil = performance.now() + dragClickSuppressMs;
    }
  });

  window.addEventListener('pointerup', finishPointerTracking);
  window.addEventListener('pointercancel', finishPointerTracking);

  tree.addEventListener('dragstart', (event) => {
    const handle = event.target.closest('.drag-handle');
    if (!handle) {
      event.preventDefault();
      return;
    }
    const row = handle.closest('.task-row');
    if (!row) return;
    draggedTask = row.closest('li.task');
    draggedList = draggedTask.parentElement;
    draggedTask.classList.add('dragging');
    tree.classList.add('is-dragging');
    if (draggedList === tree) tree.classList.add('is-order-dragging');
    if (draggedTask.dataset.parentId && detachParentDrop) {
      detachParentDrop.classList.add('is-visible');
      detachParentDrop.setAttribute('aria-hidden', 'false');
    }
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', draggedTask.dataset.taskId);
  });

  tree.addEventListener('dragover', (event) => {
    if (!draggedTask) return;

    const gap = event.target.closest('.task-order-gap');
    if (gap) {
      const reference = orderReferenceForGap(gap);
      if (!reference) return;
      event.preventDefault();
      clearDropMarkers();
      clearDropIntent();
      dropMode = 'order';
      orderReference = reference;
      gap.classList.add('is-order-target');
      event.dataTransfer.dropEffect = 'move';
      return;
    }

    const row = event.target.closest('.task-row');
    const target = row?.closest('li.task');
    if (
      !row
      || !target
      || target === draggedTask
      || draggedTask.contains(target)
    ) {
      clearDropMarkers();
      clearDropIntent();
      return;
    }

    event.preventDefault();
    clearDropMarkers();
    clearDropIntent();
    dropMode = 'parent';
    parentTarget = target;
    target.classList.add('drop-as-parent');
    row.dataset.dropLabel = `${target.dataset.title} の子にする`;
    event.dataTransfer.dropEffect = 'move';
  });

  tree.addEventListener('drop', async (event) => {
    if (!draggedTask) return;
    const sourceTask = draggedTask;
    const sourceList = draggedList;
    const selectedMode = dropMode;
    const selectedOrderReference = orderReference;
    const selectedParentTarget = parentTarget;
    event.preventDefault();
    clearDropMarkers();
    try {
      if (selectedMode === 'order' && selectedOrderReference) {
        if (!moveTaskToOrderReference(sourceTask, selectedOrderReference)) {
          throw new Error('並び替え先を見つけられませんでした');
        }
        await persistOrder(sourceList, sourceTask.dataset.dueDate);
        reorderStatus.textContent = '並び順を保存しました';
      } else if (selectedMode === 'parent' && selectedParentTarget) {
        await persistParent(sourceTask, selectedParentTarget.dataset.taskId);
        reorderStatus.textContent = `${selectedParentTarget.dataset.title}の子にしました`;
      } else {
        return;
      }
      window.location.reload();
    } catch (error) {
      reorderStatus.textContent = error.message;
      window.location.reload();
    }
  });

  detachParentDrop?.addEventListener('dragover', (event) => {
    if (!draggedTask || !draggedTask.dataset.parentId) return;
    event.preventDefault();
    clearDropMarkers();
    clearDropIntent();
    dropMode = 'detach';
    detachParentDrop.classList.add('is-active');
    event.dataTransfer.dropEffect = 'move';
  });

  detachParentDrop?.addEventListener('drop', async (event) => {
    if (!draggedTask || !draggedTask.dataset.parentId) return;
    const sourceTask = draggedTask;
    event.preventDefault();
    event.stopPropagation();
    clearDropMarkers();
    try {
      await persistParent(sourceTask, '');
      reorderStatus.textContent = '親から外しました';
      window.location.reload();
    } catch (error) {
      reorderStatus.textContent = error.message;
      window.location.reload();
    }
  });

  tree.addEventListener('dragend', () => {
    suppressRowClickUntil = performance.now() + dragClickSuppressMs;
    finishDrag();
  });

  tree.addEventListener('click', (event) => {
    if (performance.now() < suppressRowClickUntil) {
      event.preventDefault();
      event.stopPropagation();
      suppressRowClickUntil = 0;
      return;
    }
    const row = event.target.closest('.task-row');
    if (!row || event.target.closest('a, button, input, select, form, .drag-handle')) return;
    const task = row.closest('li.task');
    if (task?.dataset.detailUrl) window.location.assign(task.dataset.detailUrl);
  });
})();
</script>
</main>
</body>
"""

TASK_DETAIL_HTML = r"""
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ task['title'] }} - TODO</title>
<style>
:root {
  --bg:#f4f6fb; --surface:#fff; --surface-soft:#f8fafc; --text:#172033;
  --muted:#667085; --line:#e4e7ec; --primary:#405cf5; --primary-dark:#2f46d3;
  --success:#147a3d; --danger:#c93636;
}
* { box-sizing:border-box; }
body {
  margin:0; padding:28px;
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans JP",Meiryo,sans-serif;
  color:var(--text); background:var(--bg);
}
main { width:min(1040px,100%); margin:0 auto; }
.topbar { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:18px; }
h1 { margin:3px 0 6px; font-size:clamp(1.55rem,3vw,2.2rem); letter-spacing:-.03em; }
h2 { margin:0; font-size:1.08rem; }
.eyebrow { margin:0; color:var(--primary); font-size:.72rem; font-weight:800; letter-spacing:.14em; }
.subtitle { margin:0; color:var(--muted); }
.back-link {
  display:inline-flex; min-height:42px; align-items:center; padding:8px 12px;
  border:1px solid var(--line); border-radius:10px; color:var(--text); background:#fff; text-decoration:none; font-weight:700;
}
.card {
  margin-bottom:18px; padding:20px; border:1px solid var(--line); border-radius:16px;
  background:var(--surface); box-shadow:0 10px 28px rgba(23,32,51,.07);
}
.section-head { display:flex; justify-content:space-between; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:14px; }
.section-head p { margin:0; color:var(--muted); font-size:.86rem; }
.badge {
  display:inline-block; padding:4px 8px; border:1px solid var(--line);
  border-radius:999px; color:#566074; background:#f0f2f7; font-size:.82rem;
}
.badge-done { color:var(--success); border-color:#b8dec7; background:#eefaf2; }
.badge-open { color:#9a6500; border-color:#f2d49b; background:#fff8e6; }
.badge-bonus { color:#6336a8; border-color:#d5c3f4; background:#f3edff; }
.meta-row { display:flex; gap:7px; flex-wrap:wrap; margin-top:14px; }
.score-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:16px; }
.score-box { padding:14px; border:1px solid var(--line); border-radius:12px; background:var(--surface-soft); }
.score-box span { display:block; color:var(--muted); font-size:.78rem; }
.score-box strong { display:block; margin-top:3px; font-size:1.5rem; }
.score-box.total { color:#fff; border-color:var(--primary); background:linear-gradient(135deg,var(--primary),#7082ff); }
.score-box.total span { color:#e9edff; }
.field-grid { display:grid; grid-template-columns:minmax(0,1fr) 170px 170px; gap:10px; }
.field { display:grid; gap:6px; }
.field-wide { grid-column:1 / -1; }
label { color:#475467; font-size:.82rem; font-weight:700; }
input,select,button { min-height:41px; padding:8px 10px; border:1px solid #cfd5df; border-radius:9px; font:inherit; }
input,select { width:100%; color:var(--text); background:#fff; }
button { cursor:pointer; color:var(--text); background:#fff; font-weight:700; }
.score-choices { display:flex; gap:7px; flex-wrap:wrap; }
.score-choices label {
  display:inline-flex; align-items:center; gap:4px; padding:6px 9px;
  border:1px solid var(--line); border-radius:8px; background:var(--surface-soft); cursor:pointer;
}
.score-choices input { width:auto; min-height:auto; }
.btn-primary { color:#fff; border-color:var(--primary); background:var(--primary); }
.btn-primary:hover { background:var(--primary-dark); }
.task-add-submit,
.task-add-submit:hover {
  color:#fff !important; border-color:#000 !important;
  background:#000 !important; font-weight:850;
}
.btn-complete { color:var(--success); border-color:#b8dec7; background:#eefaf2; }
.child-list { display:grid; gap:8px; }
.child-row {
  display:flex; align-items:center; gap:8px; min-height:52px; padding:8px 10px;
  border:1px solid var(--line); border-radius:11px; background:var(--surface-soft);
}
.child-row.is-done { opacity:.72; }
.child-row.is-done .child-title { text-decoration:line-through; }
.tree-guide { flex:0 0 auto; color:#a0a7b4; white-space:pre; }
.child-title { flex:1 1 240px; min-width:0; color:var(--text); font-weight:750; text-decoration:none; }
.child-title:hover { color:var(--primary-dark); text-decoration:underline; }
.child-actions { display:flex; gap:6px; margin-left:auto; }
.child-actions form { margin:0; }
.empty { padding:20px; border:1px dashed #cfd5df; border-radius:11px; color:var(--muted); text-align:center; background:var(--surface-soft); }
.completed-time { color:var(--muted); font-size:.78rem; }
@media(max-width:760px) {
  body{padding:14px;} .topbar{flex-direction:column;} .score-grid{grid-template-columns:1fr;}
  .field-grid{grid-template-columns:1fr;} .field-wide{grid-column:auto;}
  .section-head{display:block;} .section-head p{margin-top:6px; overflow-wrap:anywhere;}
  .child-row{flex-wrap:wrap;} .child-title{flex-basis:calc(100% - 60px);}
  .child-actions{width:100%; justify-content:flex-end;}
}
body { padding:clamp(12px,1.5vw,24px); }
main { width:100%; margin:0; }
h1, h2, p { margin:0; line-height:1.15; }
.topbar { gap:0; margin:0; }
.back-link { min-height:26px; padding:3px 10px; border-radius:0; }
.card { margin:0; padding:3px 10px; border-radius:0; box-shadow:none; }
.section-head { gap:0; margin:0; }
.badge { padding:1px 4px; border-radius:0; line-height:1.15; }
.meta-row { gap:0; margin:0; }
.score-grid { gap:0; margin:0; }
.score-box { padding:3px 10px; border-radius:0; }
.score-box strong { margin:0; font-size:1.1rem; }
.field-grid { gap:0; }
.field { gap:0; }
input, select, button { min-height:26px; padding:3px 10px; border-radius:0; }
.score-choices { gap:0; }
.score-choices label { gap:0; padding:3px 10px; border-radius:0; }
.parent-link-form { display:flex; align-items:center; gap:0; }
.parent-link-form select { flex:1 1 auto; }
.parent-link-form button { white-space:nowrap; }
.child-list { gap:0; }
.child-row {
  gap:0;
  min-height:26px;
  margin-left:calc(var(--depth, 1) * 20px);
  padding:3px 10px;
  border-radius:0;
}
.child-actions { gap:0; }
.empty { padding:3px 10px; border-radius:0; }
.back-link, .card, .score-box, input, select, button,
.score-choices label, .child-row, .empty {
  padding-block:1px;
}
</style>

<body>
<main>
<header class="topbar">
  <div>
    <p class="eyebrow">TASK DETAILS</p>
    <h1>{{ task['title'] }}</h1>
    <p class="subtitle">クリックしたタスクの子タスクと完了履歴</p>
  </div>
  <a class="back-link" href="{{ url_for('index') }}">← TODOへ戻る</a>
</header>

<section class="card">
  <div class="section-head">
    <h2>タスク概要</h2>
    <span class="badge {{ 'badge-done' if task['completed'] else 'badge-open' }}">
      {{ '完了' if task['completed'] else '未完了' }}
    </span>
  </div>
  <div class="score-grid">
    <div class="score-box">
      <span>このタスク自身</span>
      <strong>{{ task['own_score'] }}点</strong>
    </div>
    <div class="score-box">
      <span>完了した直接の子</span>
      <strong>+{{ task['completed_children_score'] }}点</strong>
    </div>
    <div class="score-box total">
      <span>現在の合計</span>
      <strong>{{ task['effective_score'] }}点</strong>
    </div>
  </div>
  <div class="meta-row">
    <span class="badge">{{ task['tag'] }}</span>
    <span class="badge">期日: {{ task['due_date'] }}</span>
    {% if task['extension_count'] %}<span class="badge">延長: {{ task['extension_count'] }}回</span>{% endif %}
    {% if task['link_count'] %}<span class="badge">紐付け: {{ task['link_count'] }}本</span>{% endif %}
    {% if task['link_bonus_awarded'] %}<span class="badge badge-bonus">4紐付け +1000</span>{% endif %}
  </div>
</section>

{% if not task['completed'] %}
<section class="card">
  <div class="section-head">
    <h2>子タスクを追加</h2>
    <p>親タスクは「{{ task['title'] }}」に固定されます</p>
  </div>
  <form class="parent-link-form" method="post" action="{{ url_for('set_task_parent', task_id=task['id']) }}">
    <label for="detail-parent">このタスクの親</label>
    <select id="detail-parent" name="parent_id">
      <option value="" {% if current_parent_id is none %}selected{% endif %}>親なし</option>
      {% for parent in parent_candidates %}
        <option value="{{ parent['id'] }}" {% if current_parent_id == parent['id'] %}selected{% endif %}>{{ parent['title'] }}</option>
      {% endfor %}
    </select>
    <button type="submit">親を更新</button>
  </form>
  <form method="post" action="{{ url_for('add_child', task_id=task['id']) }}">
    <div class="field-grid">
      <div class="field field-wide">
        <label for="child-title">タイトル</label>
        <input id="child-title" type="text" name="title" placeholder="子タスクの内容" autocomplete="off" required>
      </div>
      <div class="field">
        <label for="child-due">期日</label>
        <input id="child-due" type="date" name="due_date" value="{{ today }}">
      </div>
      <div class="field">
        <label for="child-tag">タグ</label>
        <select id="child-tag" name="tag">
          {% for tag in tags %}
            <option value="{{ tag }}" {% if tag == task['tag'] %}selected{% endif %}>{{ tag }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="field">
        <label for="child-recur">繰り返し</label>
        <select id="child-recur" name="recur">
          <option value="none">なし</option>
          <option value="weekly">毎週</option>
          <option value="monthly">毎月</option>
        </select>
      </div>
      <div class="field field-wide">
        <label>基本点</label>
        <div class="score-choices">
          {% for score in [30,60,100] %}
            <label><input type="radio" name="score" value="{{ score }}" {% if score == 30 %}checked{% endif %}>{{ score }}</label>
          {% endfor %}
        </div>
      </div>
    </div>
    <button class="btn-primary task-add-submit" type="submit">子タスクを追加</button>
  </form>
</section>
{% endif %}

<section class="card">
  <div class="section-head">
    <h2>子タスク</h2>
    <p>{{ descendant_rows|length }}件 · 完了 {{ completed_descendant_count }}件</p>
  </div>
  {% if descendant_rows %}
    <div class="child-list">
      {% for row in descendant_rows %}
        {% set child = row.task %}
        <div class="child-row {% if child['completed'] %}is-done{% endif %}" style="--depth:{{ row.depth + 1 }}">
          <span class="tree-guide">{{ '　' * row.depth }}{{ '└' if row.depth else '•' }}</span>
          <span class="badge {{ 'badge-done' if child['completed'] else 'badge-open' }}">
            {{ '完了' if child['completed'] else '未完了' }}
          </span>
          <a class="child-title" href="{{ url_for('task_detail', task_id=child['id']) }}">{{ child['title'] }}</a>
          <span class="badge">{{ child['effective_score'] }}点</span>
          <span class="badge">{{ child['due_date'] }}</span>
          {% if child['completed_at'] %}<span class="completed-time">{{ child['completed_at'] }}</span>{% endif %}
          <div class="child-actions">
            {% if child['completed'] %}
              <form method="post" action="{{ url_for('undo', task_id=child['id']) }}">
                <input type="hidden" name="return_to" value="{{ url_for('task_detail', task_id=task['id']) }}">
                <button type="submit">再開</button>
              </form>
            {% else %}
              <form method="post" action="{{ url_for('complete', task_id=child['id']) }}">
                <input type="hidden" name="return_to" value="{{ url_for('task_detail', task_id=task['id']) }}">
                <button class="btn-complete" type="submit">完了</button>
              </form>
            {% endif %}
          </div>
        </div>
      {% endfor %}
    </div>
  {% else %}
    <p class="empty">子タスクはまだありません。上のフォームから追加できます。</p>
  {% endif %}
</section>
</main>
</body>
"""

TAGS_HTML = r"""
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>タグ管理</title>
<style>
:root {
  --bg:#f4f6fb; --surface:#fff; --text:#172033; --muted:#667085;
  --line:#e4e7ec; --primary:#405cf5; --danger:#c93636;
}
* { box-sizing:border-box; }
body {
  margin:0; padding:28px;
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans JP",Meiryo,sans-serif;
  color:var(--text); background:var(--bg);
}
main { width:min(760px,100%); margin:0 auto; }
.topbar { display:flex; justify-content:space-between; gap:16px; align-items:center; margin-bottom:18px; }
h1 { margin:0; font-size:1.8rem; }
.subtitle { margin:5px 0 0; color:var(--muted); }
.card {
  margin-bottom:16px; padding:18px; border:1px solid var(--line); border-radius:16px;
  background:var(--surface); box-shadow:0 10px 28px rgba(23,32,51,.07);
}
h2 { margin:0 0 12px; font-size:1.05rem; }
input,button { min-height:40px; padding:8px 11px; border:1px solid #cfd5df; border-radius:9px; font:inherit; }
input[type=text] { width:min(360px,100%); }
button { cursor:pointer; background:#fff; font-weight:650; }
.btn-primary { color:#fff; border-color:var(--primary); background:var(--primary); }
.btn-danger { color:var(--danger); border-color:#f2c5c5; background:#fff1f1; }
.back-link {
  display:inline-flex; min-height:40px; align-items:center; padding:8px 12px;
  border:1px solid var(--line); border-radius:9px; color:var(--text); background:#fff; text-decoration:none;
}
.tag-list { display:grid; gap:8px; }
.tag-item {
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  min-height:48px; padding:8px 10px; border:1px solid var(--line); border-radius:10px; background:#f8fafc;
}
.badge { display:inline-block; padding:4px 9px; border-radius:999px; background:#eef1f6; color:#566074; }
.protected { color:var(--muted); font-size:.82rem; }
form { margin:0; }
@media(max-width:640px) { body{padding:14px;} .topbar{align-items:flex-start;} }
body { padding:clamp(12px,1.5vw,24px); }
main { width:100%; margin:0; }
h1, h2, p { margin:0; line-height:1.15; }
.topbar { gap:0; margin:0; }
.card { margin:0; padding:3px 10px; border-radius:0; box-shadow:none; }
input, button { min-height:26px; padding:3px 10px; border-radius:0; }
.back-link { min-height:26px; padding:3px 10px; border-radius:0; }
.tag-list { gap:0; }
.tag-item { gap:0; min-height:26px; padding:3px 10px; border-radius:0; }
.badge { padding:1px 4px; border-radius:0; }
.card, input, button, .back-link, .tag-item { padding-block:1px; }
</style>

<body>
<main>
<header class="topbar">
  <div>
    <h1>タグ管理</h1>
    <p class="subtitle">タスクを見つけやすい分類に整えます</p>
  </div>
  <a class="back-link" href="{{ url_for('index') }}">TODOへ戻る</a>
</header>

<section class="card">
  <h2>新しいタグ</h2>
  <form method="post" action="{{ url_for('add_tag') }}">
    <input type="text" name="new_tag" placeholder="タグ名" autocomplete="off" required autofocus>
    <button class="btn-primary" type="submit">追加</button>
  </form>
</section>

<section class="card">
  <h2>登録済みタグ</h2>
  <div class="tag-list">
  {% for t in tags %}
    <div class="tag-item">
      <span class="badge">{{ t }}</span>
      {% if t != 'マイタスク' %}
        <form method="post" action="{{ url_for('delete_tag') }}" onsubmit="return confirm('このタグを削除し、付与済みタスクを「マイタスク」に移動します。よろしいですか？');">
          <input type="hidden" name="tag" value="{{ t }}">
          <button class="btn-danger" type="submit">削除</button>
        </form>
      {% else %}
        <span class="protected">標準タグ</span>
      {% endif %}
    </div>
  {% endfor %}
  </div>
</section>
</main>
</body>
"""
EDIT_HTML = r"""
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>タスク編集</title>
<style>
:root {
  --bg:#f4f6fb; --surface:#fff; --text:#172033; --muted:#667085;
  --line:#e4e7ec; --primary:#405cf5; --primary-dark:#2f46d3;
}
* { box-sizing:border-box; }
body {
  margin:0; padding:28px;
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans JP",Meiryo,sans-serif;
  color:var(--text); background:var(--bg);
}
main { width:min(900px,100%); margin:0 auto; }
.topbar { display:flex; justify-content:space-between; gap:16px; align-items:center; margin-bottom:18px; }
h1 { margin:0; font-size:1.8rem; }
.subtitle { margin:5px 0 0; color:var(--muted); }
.card {
  padding:20px; border:1px solid var(--line); border-radius:16px;
  background:var(--surface); box-shadow:0 10px 28px rgba(23,32,51,.07);
}
.field-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
.field { display:grid; gap:6px; }
.field-wide { grid-column:1 / -1; }
label { color:#475467; font-size:.84rem; font-weight:700; }
input,select,button {
  min-height:42px; padding:8px 11px; border:1px solid #cfd5df; border-radius:9px;
  color:var(--text); background:#fff; font:inherit;
}
input:focus,select:focus,button:focus-visible,a:focus-visible {
  outline:3px solid rgba(64,92,245,.18); outline-offset:1px; border-color:var(--primary);
}
.actions { display:flex; gap:10px; align-items:center; margin-top:18px; }
button { cursor:pointer; font-weight:700; }
.btn-primary { color:#fff; border-color:var(--primary); background:var(--primary); }
.btn-primary:hover { background:var(--primary-dark); }
.back-link {
  display:inline-flex; min-height:42px; align-items:center; padding:8px 12px;
  border:1px solid var(--line); border-radius:9px; color:var(--text); background:#fff; text-decoration:none;
}
.cancel-link { color:var(--muted); font-size:.9rem; }
.score-breakdown {
  padding:10px 12px; border:1px solid var(--line); border-radius:10px;
  color:var(--muted); background:#f8fafc; font-size:.88rem;
}
@media(max-width:640px) {
  body{padding:14px;} .topbar{align-items:flex-start;} .field-grid{grid-template-columns:1fr;} .field-wide{grid-column:auto;}
}
body { padding:clamp(12px,1.5vw,24px); }
main { width:100%; margin:0; }
h1, p { margin:0; line-height:1.15; }
.topbar { gap:0; margin:0; }
.card { padding:3px 10px; border-radius:0; box-shadow:none; }
.field-grid { gap:0; }
.field { gap:0; }
input, select, button { min-height:26px; padding:3px 10px; border-radius:0; }
.actions { gap:0; margin:0; }
.back-link { min-height:26px; padding:3px 10px; border-radius:0; }
.score-breakdown { padding:3px 10px; border-radius:0; }
.card, input, select, button, .back-link, .score-breakdown { padding-block:1px; }
</style>

<body>
<main>
<header class="topbar">
  <div>
    <h1>タスク編集</h1>
    <p class="subtitle">内容・分類・期限・繰り返しをまとめて変更できます</p>
  </div>
  <a class="back-link" href="{{ url_for('index') }}">TODOへ戻る</a>
</header>

<section class="card">
  <form method="post">
    <div class="field-grid">
      <div class="field field-wide">
        <label for="edit-title">タイトル</label>
        <input id="edit-title" type="text" name="title" value="{{ task['title'] }}" autocomplete="off" required autofocus>
      </div>

      <div class="field">
        <label for="edit-tag">タグ</label>
        <select id="edit-tag" name="tag">
          {% for tg in tags %}
            <option value="{{ tg }}" {% if tg == task['tag'] %}selected{% endif %}>{{ tg }}</option>
          {% endfor %}
        </select>
      </div>

      <div class="field">
        <label for="edit-score">基本点</label>
        <select id="edit-score" name="score">
          {% for s in [30,60,100] %}
            <option value="{{ s }}" {% if s == task['base_score'] %}selected{% endif %}>{{ s }}</option>
          {% endfor %}
        </select>
      </div>

      <div class="field">
        <label for="edit-due-date">期日</label>
        <input id="edit-due-date" type="date" name="due_date" value="{{ task['due_date'] }}">
      </div>

      <div class="field">
        <label for="edit-recur">繰り返し</label>
        <select id="edit-recur" name="recur">
          <option value="none" {% if task['recur'] == 'none' %}selected{% endif %}>なし</option>
          <option value="weekly" {% if task['recur'] == 'weekly' %}selected{% endif %}>毎週</option>
          <option value="monthly" {% if task['recur'] == 'monthly' %}selected{% endif %}>毎月</option>
        </select>
      </div>

      <div class="field field-wide">
        <label for="edit-parent">親タスク</label>
        <select id="edit-parent" name="parent_id">
          <option value="" {% if current_parent_id is none %}selected{% endif %}>なし</option>
          {% for p in parent_candidates %}
            <option value="{{ p['id'] }}" {% if current_parent_id == p['id'] %}selected{% endif %}>{{ p['title'] }}</option>
          {% endfor %}
        </select>
      </div>

      <div class="score-breakdown field-wide">
        現在の合計: <strong>{{ task['effective_score'] }}点</strong>
        {% if task['completed_children_score'] %} · 完了した子 +{{ task['completed_children_score'] }}点{% endif %}
        {% if task['extension_count'] %} · 延長 {{ task['extension_count'] }}回{% endif %}
        {% if task['link_bonus_awarded'] %} · 4紐付けボーナス +1000点{% endif %}
      </div>
    </div>

    <div class="actions">
      <button class="btn-primary" type="submit">変更を保存</button>
      <a class="cancel-link" href="{{ url_for('index') }}">キャンセル</a>
    </div>
  </form>
</section>
</main>
</body>
"""
# ---------- ルーティング ----------
def task_for_api(task):
    return {
        'id': task['id'],
        'title': task['title'],
        'tag': task['tag'],
        'score': task['score'],
        'effective_score': task_effective_score(task),
        'completed_children_score': to_int(task.get('completed_children_score'), 0),
        'base_score': task.get('base_score', task['score']),
        'extension_count': task.get('extension_count', 0),
        'link_bonus_awarded': bool(task.get('link_bonus_awarded', 0)),
        'sort_order': to_int(task.get('sort_order'), task['id'] * 10),
        'due_date': task['due_date'],
        'completed': bool(task['completed']),
        'completed_at': task['completed_at'],
        'parent_id': task['parent_id'],
        'recur': task['recur'],
        'sync_pending': bool(task.get('sync_pending', 0))
    }


def codex_api_guard_response():
    remote_addr = (request.remote_addr or '').strip()
    if remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'ok': False, 'error': 'Codex API is available only from this PC.'}), 403

    if CODEX_TASK_API_TOKEN:
        supplied = request.headers.get('X-Codex-Task-Token', '').strip()
        authorization = request.headers.get('Authorization', '').strip()
        if authorization.lower().startswith('bearer '):
            supplied = authorization[7:].strip()
        if supplied != CODEX_TASK_API_TOKEN:
            return jsonify({'ok': False, 'error': 'Invalid Codex task API token.'}), 401

    return None


def requested_return_url(default_url):
    target = (request.form.get('return_to') or '').strip()
    if target.startswith('/') and not target.startswith('//'):
        return target
    return default_url


def create_local_task(title, tag='マイタスク', score=30, due_date=None,
                      recur='none', parent_id=''):
    title = (title or '').strip()
    if not title:
        raise ValueError('title is required')

    tag = (tag or 'マイタスク').strip() or 'マイタスク'
    score = sanitize_score(score)
    due_date = sanitize_due_date(due_date or today_str())
    recur = sanitize_recur(recur or 'none')
    parent_id = sanitize_parent_id(str(parent_id) if parent_id is not None else '')
    tag = auto_tag(title, tag, read_tags())

    with TASKS_LOCK:
        tasks = read_tasks()
        tid = next_task_id(tasks)
        sort_order = first_sibling_sort_order(tasks, parent_id, due_date)
        new_task = {
            'id': tid,
            'title': title,
            'tag': tag,
            'score': score,
            'base_score': score,
            'extension_count': 0,
            'link_bonus_awarded': 0,
            'sort_order': sort_order,
            'due_date': due_date,
            'completed': 0,
            'completed_at': '',
            'parent_id': parent_id,
            'recur': recur,
            'google_task_id': '',
            'sync_pending': 1 if GOOGLE_SYNC_ENABLED else 0
        }
        tasks.append(new_task)
        bonus_task_ids = apply_link_bonuses(tasks)
        annotate_effective_scores(tasks)
        write_tasks(tasks)

    for sync_task_id in {tid, *bonus_task_ids}:
        enqueue_task_sync(sync_task_id)
    return dict(new_task)


def complete_local_task(task_id):
    now = dt.datetime.now().replace(microsecond=0)
    completed_task = None
    next_task = None
    bonus_task_ids = []

    with TASKS_LOCK:
        tasks = read_tasks()

        for task in tasks:
            if task['id'] != task_id or task['completed'] != 0:
                continue

            task['completed'] = 1
            task['completed_at'] = now.isoformat(sep=' ')
            task['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0

            if task['recur'] == 'weekly':
                next_due = (parse_date(task['due_date']) + dt.timedelta(days=7)).isoformat()
            elif task['recur'] == 'monthly':
                next_due = add_months(task['due_date'], 1)
            else:
                next_due = None

            if next_due:
                next_base_score = to_int(task.get('base_score'), task['score'])
                next_id = next_task_id(tasks)
                next_task = {
                    'id': next_id,
                    'title': task['title'],
                    'tag': task['tag'],
                    'score': next_base_score,
                    'base_score': next_base_score,
                    'extension_count': 0,
                    'link_bonus_awarded': 0,
                    'sort_order': next_sibling_sort_order(
                        tasks,
                        task['parent_id'],
                        next_due
                    ),
                    'due_date': next_due,
                    'completed': 0,
                    'completed_at': '',
                    'parent_id': task['parent_id'],
                    'recur': task['recur'],
                    'google_task_id': '',
                    'sync_pending': 1 if GOOGLE_SYNC_ENABLED else 0
                }
                tasks.append(next_task)
                bonus_task_ids = apply_link_bonuses(tasks)

            annotate_effective_scores(tasks)
            completed_task = dict(task)
            if next_task:
                next_task = dict(next_task)
            write_tasks(tasks)
            break

    if completed_task:
        enqueue_task_sync(task_id)
    if next_task:
        enqueue_task_sync(next_task['id'])
    for bonus_task_id in bonus_task_ids:
        enqueue_task_sync(bonus_task_id)

    return completed_task, next_task


def reopen_local_task(task_id):
    reopened_task = None

    with TASKS_LOCK:
        tasks = read_tasks()
        for task in tasks:
            if task['id'] != task_id:
                continue
            task['completed'] = 0
            task['completed_at'] = ''
            task['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0
            annotate_effective_scores(tasks)
            reopened_task = dict(task)
            write_tasks(tasks)
            break

    if reopened_task:
        enqueue_task_sync(task_id)
    return reopened_task


@app.before_request
def ensure_background_sync():
    if request.path == '/api/codex' or request.path.startswith('/api/codex/'):
        denied = codex_api_guard_response()
        if denied:
            return denied
    start_sync_worker()


@app.route('/api/codex/health')
def codex_api_health():
    return jsonify({'ok': True, 'service': 'tasklist', 'version': 1})


@app.route('/api/codex/tasks')
def codex_api_tasks():
    status = request.args.get('status', 'open').strip().lower()
    if status not in ('open', 'completed', 'all'):
        return jsonify({'ok': False, 'error': 'status must be open, completed, or all'}), 400

    with TASKS_LOCK:
        tasks = read_tasks()
        bonus_task_ids = apply_link_bonuses(tasks)
        annotate_effective_scores(tasks)
        if bonus_task_ids:
            write_tasks(tasks)
    for bonus_task_id in bonus_task_ids:
        enqueue_task_sync(bonus_task_id)

    if status == 'open':
        tasks = [task for task in tasks if task['completed'] == 0]
    elif status == 'completed':
        tasks = [task for task in tasks if task['completed'] != 0]

    tasks.sort(key=task_sort_key)
    return jsonify({'ok': True, 'count': len(tasks), 'tasks': [task_for_api(task) for task in tasks]})


@app.route('/api/codex/tasks', methods=['POST'])
def codex_api_add_task():
    payload = request.get_json(silent=True) or {}
    try:
        task = create_local_task(
            title=payload.get('title'),
            tag=payload.get('tag', 'マイタスク'),
            score=payload.get('score', 30),
            due_date=payload.get('due_date'),
            recur=payload.get('recur', 'none'),
            parent_id=payload.get('parent_id', '')
        )
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400

    return jsonify({'ok': True, 'task': task_for_api(task)}), 201


@app.route('/api/codex/tasks/<int:task_id>/complete', methods=['POST'])
def codex_api_complete_task(task_id):
    task, next_task = complete_local_task(task_id)
    if not task:
        return jsonify({'ok': False, 'error': 'Open task not found.'}), 404

    response = {'ok': True, 'task': task_for_api(task)}
    if next_task:
        response['next_task'] = task_for_api(next_task)
    return jsonify(response)


@app.route('/api/codex/tasks/<int:task_id>/reopen', methods=['POST'])
def codex_api_reopen_task(task_id):
    task = reopen_local_task(task_id)
    if not task:
        return jsonify({'ok': False, 'error': 'Task not found.'}), 404
    return jsonify({'ok': True, 'task': task_for_api(task)})


@app.route('/reorder', methods=['POST'])
def reorder_tasks():
    payload = request.get_json(silent=True) or {}
    raw_parent_id = payload.get('parent_id', '')
    parent_id = sanitize_parent_id(str(raw_parent_id) if raw_parent_id is not None else '')
    due_date = str(payload.get('due_date') or '').strip()
    raw_ordered_ids = payload.get('ordered_ids')

    if parent_id:
        return jsonify({'ok': False, 'error': 'Only top-level tasks can be reordered'}), 400

    try:
        dt.date.fromisoformat(due_date)
    except ValueError:
        return jsonify({'ok': False, 'error': 'due_date must be YYYY-MM-DD'}), 400

    if not isinstance(raw_ordered_ids, list):
        return jsonify({'ok': False, 'error': 'ordered_ids must be a list'}), 400

    try:
        ordered_ids = [int(task_id) for task_id in raw_ordered_ids]
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'ordered_ids must contain integers'}), 400

    if len(ordered_ids) != len(set(ordered_ids)):
        return jsonify({'ok': False, 'error': 'ordered_ids contains duplicates'}), 400

    with TASKS_LOCK:
        tasks = read_tasks()
        active = [task for task in tasks if task['completed'] == 0]
        active_ids = {str(task['id']) for task in active}

        siblings = []
        for task in active:
            effective_parent = task['parent_id'] if task['parent_id'] in active_ids else ''
            if effective_parent == '' and task['due_date'] == due_date:
                siblings.append(task)

        sibling_ids = {task['id'] for task in siblings}
        if set(ordered_ids) != sibling_ids:
            return jsonify({
                'ok': False,
                'error': 'ordered_ids must match all top-level tasks with the same due date'
            }), 400

        tasks_by_id = {task['id']: task for task in tasks}
        for index, task_id in enumerate(ordered_ids, start=1):
            tasks_by_id[task_id]['sort_order'] = index * 10

        write_tasks(tasks)

    return jsonify({'ok': True, 'due_date': due_date, 'ordered_ids': ordered_ids})


@app.route('/task/<int:task_id>')
def task_detail(task_id):
    with TASKS_LOCK:
        tasks = read_tasks()
        bonus_task_ids = apply_link_bonuses(tasks)
        annotate_effective_scores(tasks)
        if bonus_task_ids:
            write_tasks(tasks)

    for bonus_task_id in bonus_task_ids:
        enqueue_task_sync(bonus_task_id)

    task = next((item for item in tasks if item['id'] == task_id), None)
    if not task:
        return redirect(url_for('index'))

    descendant_rows = collect_descendant_rows(task_id, tasks)
    completed_descendant_count = sum(
        1 for row in descendant_rows if row['task']['completed'] == 1
    )
    parent_candidates, current_parent_id = parent_candidates_for_task(
        tasks,
        task_id
    )

    return render_template_string(
        TASK_DETAIL_HTML,
        task=task,
        descendant_rows=descendant_rows,
        completed_descendant_count=completed_descendant_count,
        parent_candidates=parent_candidates,
        current_parent_id=current_parent_id,
        tags=read_tags(),
        today=today_str(),
    )


@app.route('/task/<int:task_id>/parent', methods=['POST'])
def set_task_parent(task_id):
    raw_parent_id = (request.form.get('parent_id') or '').strip()
    updated = False
    bonus_task_ids = []

    with TASKS_LOCK:
        tasks = read_tasks()
        task = next(
            (
                item for item in tasks
                if item['id'] == task_id and item['completed'] == 0
            ),
            None
        )
        if not task:
            return 'Open task not found', 404

        parent_candidates, _ = parent_candidates_for_task(tasks, task_id)
        allowed_parent_ids = {str(item['id']) for item in parent_candidates}

        if raw_parent_id and raw_parent_id not in allowed_parent_ids:
            return 'Invalid parent task', 400

        new_parent_id = raw_parent_id
        if task['parent_id'] != new_parent_id:
            task['sort_order'] = next_sibling_sort_order(
                tasks,
                new_parent_id,
                task['due_date'],
                exclude_task_id=task_id
            )
            task['parent_id'] = new_parent_id
            task['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0
            updated = True

        if updated:
            bonus_task_ids = apply_link_bonuses(tasks)
            annotate_effective_scores(tasks)
            write_tasks(tasks)

    if updated:
        enqueue_task_sync(task_id)
    for bonus_task_id in bonus_task_ids:
        enqueue_task_sync(bonus_task_id)

    return redirect(url_for('task_detail', task_id=task_id))


@app.route('/task/<int:task_id>/children', methods=['POST'])
def add_child(task_id):
    with TASKS_LOCK:
        tasks = read_tasks()
        parent = next(
            (task for task in tasks if task['id'] == task_id and task['completed'] == 0),
            None
        )

    if not parent:
        return redirect(url_for('index'))

    title = (request.form.get('title') or '').strip()
    if title:
        create_local_task(
            title=title,
            tag=request.form.get('tag', parent['tag']),
            score=request.form.get('score', '30'),
            due_date=request.form.get('due_date', today_str()),
            recur=request.form.get('recur', 'none'),
            parent_id=str(task_id)
        )

    return redirect(url_for('task_detail', task_id=task_id))

@app.route('/')
def index():
    request_google_pull()

    with TASKS_LOCK:
        tasks = read_tasks()
        bonus_task_ids = apply_link_bonuses(tasks)
        annotate_effective_scores(tasks)
        if bonus_task_ids:
            write_tasks(tasks)
    for bonus_task_id in bonus_task_ids:
        enqueue_task_sync(bonus_task_id)
    tags = read_tags()

    today = dt.date.today()
    active = []
    for t in tasks:
        if t['completed'] == 0:
            t['is_overdue'] = parse_date(t['due_date']) < today
            t['id_str'] = str(t['id'])
            active.append(t)

    active.sort(key=task_sort_key)

    overdue = [t for t in active if t['is_overdue']]
    task_summary = {
        'open': len(active),
        'today': sum(1 for t in active if t['due_date'] == today_str()),
        'overdue': len(overdue),
    }

    active_ids = {str(t['id']) for t in active}

    children_by_parent = {}
    for t in active:
        pid = t['parent_id'] if t['parent_id'] in active_ids else ''
        t['parent_id_effective'] = pid
        children_by_parent.setdefault(pid, []).append(t)

    for children in children_by_parent.values():
        children.sort(key=task_sort_key)

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
            'weekday': '月火水木金土日'[d.weekday()],
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
        task_summary=task_summary,
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

    create_local_task(
        title=title,
        tag=request.form.get('tag', 'マイタスク'),
        score=request.form.get('score', '30'),
        due_date=request.form.get('due_date', today_str()),
        recur=request.form.get('recur', 'none'),
        parent_id=request.form.get('parent_id', '')
    )
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
    complete_local_task(task_id)
    return redirect(requested_return_url(url_for('index')))

@app.route('/reschedule/<int:task_id>', methods=['POST'])
def reschedule(task_id):
    new_due = sanitize_due_date(request.form.get('new_due_date', today_str()))
    rescheduled = False

    with TASKS_LOCK:
        tasks = read_tasks()

        for t in tasks:
            if t['id'] == task_id and t['completed'] == 0:
                if t['due_date'] != new_due:
                    t['sort_order'] = next_sibling_sort_order(
                        tasks,
                        t['parent_id'],
                        new_due,
                        exclude_task_id=task_id
                    )
                t['due_date'] = new_due
                t['extension_count'] = max(to_int(t.get('extension_count'), 0), 0) + 1
                t['score'] = to_int(t['score'], 0) + 30 * t['extension_count']
                t['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0
                rescheduled = True
                break

        if rescheduled:
            write_tasks(tasks)

    if rescheduled:
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
    reopen_local_task(task_id)
    return redirect(requested_return_url(url_for('index')))

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

    updated = False
    bonus_task_ids = []

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
                if t['parent_id'] != new_parent_id:
                    t['sort_order'] = next_sibling_sort_order(
                        tasks,
                        new_parent_id,
                        t['due_date'],
                        exclude_task_id=task_id
                    )
                t['parent_id'] = new_parent_id
                t['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0
                updated = True
                break

        if updated:
            bonus_task_ids = apply_link_bonuses(tasks)
            write_tasks(tasks)

    if updated:
        enqueue_task_sync(task_id)
    for bonus_task_id in bonus_task_ids:
        enqueue_task_sync(bonus_task_id)

    return redirect(url_for('index'))

@app.route('/edit/<int:task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    tags = read_tags()

    with TASKS_LOCK:
        tasks = read_tasks()
        annotate_effective_scores(tasks)

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
        new_title = (request.form.get('title') or '').strip()
        if not new_title:
            new_title = task['title']

        new_tag = (request.form.get('tag') or 'マイタスク').strip() or 'マイタスク'
        if new_tag not in tags:
            new_tag = 'マイタスク'

        new_base_score = sanitize_score(
            request.form.get('score'),
            sanitize_score(task.get('base_score'), 30)
        )
        new_recur = sanitize_recur(
            request.form.get('recur') or task.get('recur') or 'none'
        )

        new_parent_id = (request.form.get('parent_id') or '').strip()
        if not (new_parent_id and new_parent_id.isdigit() and new_parent_id in active_ids and int(new_parent_id) not in forbidden):
            new_parent_id = ''

        new_due_date = sanitize_due_date(
            request.form.get('due_date') or task.get('due_date') or today_str()
        )

        bonus_task_ids = []
        with TASKS_LOCK:
            tasks = read_tasks()
            for current in tasks:
                if current['id'] == task_id:
                    current['title'] = new_title
                    current['tag'] = new_tag
                    set_task_base_score(current, new_base_score)
                    if (
                        current['parent_id'] != new_parent_id
                        or current['due_date'] != new_due_date
                    ):
                        current['sort_order'] = next_sibling_sort_order(
                            tasks,
                            new_parent_id,
                            new_due_date,
                            exclude_task_id=task_id
                        )
                    current['parent_id'] = new_parent_id
                    current['due_date'] = new_due_date
                    current['recur'] = new_recur
                    current['sync_pending'] = 1 if GOOGLE_SYNC_ENABLED else 0
                    break
            bonus_task_ids = apply_link_bonuses(tasks)
            annotate_effective_scores(tasks)
            write_tasks(tasks)

        enqueue_task_sync(task_id)
        for bonus_task_id in bonus_task_ids:
            enqueue_task_sync(bonus_task_id)

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
    app.run(debug=False, use_reloader=False)
