# -*- coding: utf-8 -*-
from flask import Flask, render_template_string, request, redirect, url_for
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
app = Flask(__name__)

DATA_DIR = 'data'
TASKS_CSV = os.path.join(DATA_DIR, 'tasks.csv')
TAGS_CSV = os.path.join(DATA_DIR, 'tags.csv')
TAG_RULES_JSON = os.path.join(DATA_DIR, 'tag_rules.json')  # ← 追加
TASK_FIELDS = ['id', 'title', 'tag', 'score', 'due_date', 'completed', 'completed_at', 'parent_id', 'recur']

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
            row['id'] = int(row['id'])
            row['score'] = int(row['score']) if row['score'] else 0
            row['completed'] = int(row['completed']) if row['completed'] else 0
            tasks.append(row)
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
                'recur': t['recur']
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
li.task {
  margin: 4px 0;
  padding-left: .3em;
  display: flex;
  align-items: center;
  gap: 6px;
}

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
</style>


<h1>TODOダッシュボード</h1>

<section class="card">
  <h2>タグ</h2>
  <div>
    既存タグ:
        {% for t in tags %}
          <span class="badge badge-tag">{{ t }}</span>
        {% endfor %}
  </div>
  <div style="margin-top:8px;"><a href="{{ url_for('tags_page') }}">タグの追加・削除</a></div>
</section>

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

<section class="card">
  <h2>タスク登録</h2>
  <form method="post" action="{{ url_for('add') }}">
    <div class="form-inline">
    
    <label>タイトル</label><input type="text" name="title" required>
    
    <label>タグ</label>
    <select name="tag">
      {% for t in tags %}
        <option value="{{ t }}" {% if t == 'マイタスク' %}selected{% endif %}>{{ t }}</option>
      {% endfor %}
    </select>
    
    <label>親タスク</label>
    <select name="parent_id">
      <option value="">なし</option>
      {% for rt in selectable_parents %}
        <option value="{{ rt['id'] }}">{{ rt['title'] }}</option>
      {% endfor %}
    </select>
    
    <label>期日</label>
    <input type="date" name="due_date" value="{{ today }}">
    
    <label>定期</label>
    <select name="recur">
      <option value="none" selected>なし</option>
      <option value="weekly">毎週</option>
      <option value="monthly">毎月</option>
    </select>

      
    </div>
    <div style="margin-top:8px;">
      <div>点数（デフォルト30）</div>
      <div class="score-choices">
        {% for s in [30,40,50,60,70,80,90,100] %}
          <label><input type="radio" name="score" value="{{ s }}" {% if s==30 %}checked{% endif %}>{{ s }}</label>
        {% endfor %}
      </div>
    </div>
    <div style="margin-top:8px;"><input type="submit" value="追加"></div>
  </form>
</section>

<section class="card">
  <h2>未完了タスク</h2>
  <div class="row">
    <!-- 左：ツリー -->
    <div style="flex:2; min-width: 260px;">
      <ul class="tree">
        {% macro render_children(pid) %}
          {% for t in children_by_parent.get(pid, []) %}
            <li class="task">
              <form style="display:inline;" method="post" action="{{ url_for('complete', task_id=t['id']) }}">
                <button title="完了">✔</button>
              </form>
              <form style="display:inline;" method="post"
                    action="{{ url_for('delete', task_id=t['id']) }}"
                    onsubmit="return confirm('このタスクを削除しますか？');">
                <button title="削除">✖</button>
              </form>
              <strong>{{ t['title'] }}</strong>

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

                <form style="display:inline-flex; align-items:center; gap:4px;" method="post" action="{{ url_for('update_meta', task_id=t['id']) }}">
           
                  <select name="tag">
                    {% for tg in tags %}
                      <option value="{{ tg }}" {% if tg == t['tag'] %}selected{% endif %}>{{ tg }}</option>
                    {% endfor %}
                  </select>
                
                  <select name="parent_id">
                    <option value="" {% if not t['parent_id_effective'] %}selected{% endif %}>なし</option>
                    {% for rt in selectable_parents %}
                      {% if rt['id'] not in t['forbidden_parent_ids'] %}
                        <option value="{{ rt['id'] }}" {% if rt['id_str'] == t['parent_id_effective'] %}selected{% endif %}>
                          {{ rt['title'] }}
                        </option>
                      {% endif %}
                    {% endfor %}
                  </select>
                
                  <button title="タグ/親を更新">更新</button>
                </form>

              {% if t['recur'] != 'none' %}
                <span class="badge">定期: {{ '毎週' if t['recur']=='weekly' else '毎月' }}</span>
              {% endif %}
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
  <div><img alt="chart" src="data:image/png;base64,{{ chart_b64 }}"></div>
  <div>合計点: <strong>{{ total_14d }}</strong></div>
</section>

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

# ---------- ルーティング ----------
@app.route('/')
def index():
    tasks = read_tasks()
    tags = read_tags()

    # 未完・期限超過判定
    today = dt.date.today()
    active = []
    for t in tasks:
        if t['completed'] == 0:
            t['is_overdue'] = parse_date(t['due_date']) < today
            t['id_str'] = str(t['id'])
            active.append(t)
        # 期日が近いものから順に並べる（期日 → id）
    active.sort(key=lambda t: (parse_date(t['due_date']), -t['id']))

   



    overdue = [t for t in active if t['is_overdue']]

    # ツリー構築
    active_ids = {str(t['id']) for t in active}
    
    # ツリー構築（親が未完了に存在しない場合はルート扱いにする）
    children_by_parent = {}
    for t in active:
        pid = t['parent_id'] if t['parent_id'] in active_ids else ''
        t['parent_id_effective'] = pid
        children_by_parent.setdefault(pid, []).append(t)
    
    # 親候補（全未完了タスク）も期日昇順＋新しいid優先
    selectable_parents = sorted(
        active,
        key=lambda x: (parse_date(x['due_date']), -x['id'])
    )
    
    # 「自分＋子孫」を親にできないようにする（循環防止）
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
    
    
    # ★ 今週カレンダー用データ（今日〜6日後の未完了タスク）
    week_calendar = []
    for offset in range(7):
        d = today + dt.timedelta(days=offset)
        ds = d.isoformat()
        day_tasks = [t for t in active if t['due_date'] == ds]
        week_calendar.append({
            'date': d,
            'tasks': day_tasks,
        })
    # 折れ線グラフ（最下部に表示するが、データはここで用意）
    chart_b64, total_14d = chart_last_14_days_png_b64(tasks)

    # 最近完了
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
        chart_b64=chart_b64,
        total_14d=total_14d,
        recent_done=recent_done,
        week_calendar=week_calendar,  # ← ここを追加
    )

@app.route('/add', methods=['POST'])
def add():
    title = request.form.get('title', '').strip()
    tag = request.form.get('tag', 'マイタスク').strip() or 'マイタスク'
    score = int(request.form.get('score', '30'))
    due_date = request.form.get('due_date', today_str())
    recur = request.form.get('recur', 'none')
    parent_id = request.form.get('parent_id', '')

    tasks = read_tasks()
    tags = read_tags()

    # ここで自動タグ付け
    tag = auto_tag(title, tag, tags)

    tid = next_task_id(tasks)
    tasks.append({
        'id': tid,
        'title': title,
        'tag': tag,
        'score': score,
        'due_date': due_date,
        'completed': 0,
        'completed_at': '',
        'parent_id': parent_id,
        'recur': recur
    })
    write_tasks(tasks)
    return redirect(url_for('index'))


@app.route('/complete/<int:task_id>', methods=['POST'])
def complete(task_id):
    tasks = read_tasks()
    now = dt.datetime.now().replace(microsecond=0)
    for t in tasks:
        if t['id'] == task_id and t['completed'] == 0:
            t['completed'] = 1
            t['completed_at'] = now.isoformat(sep=' ')
            # 定期タスク生成
            if t['recur'] == 'weekly':
                next_due = (parse_date(t['due_date']) + dt.timedelta(days=7)).isoformat()
            elif t['recur'] == 'monthly':
                next_due = add_months(t['due_date'], 1)
            else:
                next_due = None
            if next_due:
                new_id = next_task_id(tasks)
                tasks.append({
                    'id': new_id,
                    'title': t['title'],
                    'tag': t['tag'],
                    'score': t['score'],
                    'due_date': next_due,
                    'completed': 0,
                    'completed_at': '',
                    'parent_id': t['parent_id'],
                    'recur': t['recur']
                })
            break
    write_tasks(tasks)
    return redirect(url_for('index'))

@app.route('/reschedule/<int:task_id>', methods=['POST'])
def reschedule(task_id):
    new_due = request.form.get('new_due_date', today_str())
    tasks = read_tasks()

    for t in tasks:
        if t['id'] == task_id and t['completed'] == 0:

            # 期日を新しく更新
            t['due_date'] = new_due

            # ★ ここで +30 点する（100超えてOK）
            t['score'] = int(t['score']) + 30

            break

    write_tasks(tasks)
    return redirect(url_for('index'))


# --- 追加: タスク削除（自分＋子孫を再帰的に削除） ---
@app.route('/delete/<int:task_id>', methods=['POST'])
def delete(task_id):
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
    tasks = [t for t in tasks if t['id'] not in to_delete]
    write_tasks(tasks)
    return redirect(url_for('index'))

# --- 追加: 完了取り消し（未完了に戻す） ---
@app.route('/undo/<int:task_id>', methods=['POST'])
def undo(task_id):
    tasks = read_tasks()
    for t in tasks:
        if t['id'] == task_id:
            t['completed'] = 0
            t['completed_at'] = ''
            break
    write_tasks(tasks)
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

    tasks = read_tasks()
    tags = read_tags()

    if new_tag not in tags:
        new_tag = 'マイタスク'

    active = [t for t in tasks if t['completed'] == 0]
    active_ids = {str(t['id']) for t in active}

    # 子参照（未完了のみ・存在しない親はルート扱い）
    children_by_parent = {}
    for t in active:
        pid = t['parent_id'] if t['parent_id'] in active_ids else ''
        children_by_parent.setdefault(pid, []).append(t)

    # self + descendants を禁止（循環防止）
    forbidden = {task_id}
    stack = [str(task_id)]
    while stack:
        pid = stack.pop()
        for ch in children_by_parent.get(pid, []):
            cid = ch['id']
            if cid not in forbidden:
                forbidden.add(cid)
                stack.append(str(cid))

    # 親は「未完了タスクのみ」から選べるように制限
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


if __name__ == '__main__':
    ensure_files()
    app.run(debug=True)
