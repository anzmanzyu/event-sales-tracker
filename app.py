"""
イベント現場 リアルタイム実績入力アプリ
=====================================================

携帯販売のイベント現場で、スタッフがスマホからワンタップで獲得実績を入力し、
会場全体の目標達成率をリアルタイムに共有するアプリ。

■ アーキテクチャ（本番: A案）
    スマホ（このStreamlitアプリ）
      → Supabase（リアルタイムの本体DB / Postgres）
      → Notion（管理者向けダッシュボード・報告用）

■ 動作モードの自動切替
    - Supabaseの接続情報が設定されていれば「Supabaseモード」（本番）
    - 無ければ「SQLiteモード」（ローカル検証用・従来どおり）
    どちらでも同じUI・同じ関数で動くよう、データ層を抽象化している。

起動方法（ローカル / SQLite）:
    pip install -r requirements.txt
    streamlit run app.py

本番（Supabase）にするには .streamlit/secrets.toml に接続情報を入れる。
テンプレートは .streamlit/secrets.toml.example を参照。
"""

import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone

import streamlit as st
import streamlit.components.v1 as components

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# 実績のカテゴリ（合算目標に対して、すべて1件としてカウントする）
CATEGORIES = ["機種変更", "MNP", "新規契約", "LTV商材"]

# 各カテゴリのアイコン（ボタン識別用）
CATEGORY_ICONS = {
    "機種変更": "📱",
    "MNP": "🔁",
    "新規契約": "✨",
    "LTV商材": "💡",
}

# ローカルSQLiteの保存先（Supabase未設定時のフォールバック）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "event_sales.db")

# 画面の自動更新間隔（ミリ秒）。他スタッフの入力を取り込む間隔。
AUTO_REFRESH_MS = 5000


# ---------------------------------------------------------------------------
# 設定（secrets）ヘルパー
# ---------------------------------------------------------------------------

def _secrets_section(name: str):
    """st.secrets の任意セクションを安全に取得する（無ければ None）。

    ローカルで secrets.toml が無い場合、st.secrets へのアクセスは
    例外を投げるため try で握りつぶす。
    """
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return None


def get_backend_mode() -> str:
    """使用するデータ層を判定する。"supabase" or "sqlite"。"""
    # 環境変数での明示指定を優先（テスト・ローカル用）
    forced = os.environ.get("EVENT_APP_BACKEND")
    if forced in ("sqlite", "supabase"):
        return forced
    return "supabase" if _secrets_section("supabase") else "sqlite"


def auth_required() -> bool:
    """secrets に [auth] pin があればログインを要求する。

    ローカル検証（secretsに[auth]なし）では要求しない＝そのまま開ける。
    Streamlit Cloudの公開URLは誰でも開けてしまうため、本番では必ず設定する。
    """
    return _secrets_section("auth") is not None


def check_pin(entered: str) -> bool:
    """入力PINが会場共通の合言葉と一致するか。"""
    conf = _secrets_section("auth")
    if not conf:
        return True
    return str(entered).strip() == str(conf.get("pin", "")).strip()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_local_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ===========================================================================
# データ層（バックエンド）
#   - どちらのバックエンドも同じメソッド名・同じ戻り値の形をそろえている
#   - UI側は get_backend() 経由で呼ぶだけで、中身の違いを意識しない
# ===========================================================================

class SQLiteBackend:
    """ローカル検証用。1台のPC上のSQLiteファイルを共有する。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS venues (
                    venue        TEXT PRIMARY KEY,
                    target       INTEGER NOT NULL DEFAULT 0,
                    period_start TEXT,
                    period_end   TEXT,
                    updated_at   TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue      TEXT    NOT NULL,
                    staff      TEXT    NOT NULL,
                    category   TEXT    NOT NULL,
                    delta      INTEGER NOT NULL,
                    created_at TEXT    NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_venue ON events(venue)")

    def set_venue(self, venue: str, target: int, period_start: str, period_end: str):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO venues (venue, target, period_start, period_end, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(venue) DO UPDATE SET
                    target = excluded.target,
                    period_start = excluded.period_start,
                    period_end = excluded.period_end,
                    updated_at = excluded.updated_at
                """,
                (venue, int(target), period_start, period_end, _now_local_iso()),
            )

    def get_venue_meta(self, venue: str):
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT target, period_start, period_end FROM venues WHERE venue = ?",
                (venue,),
            ).fetchone()
        if not row:
            return None
        return {"target": int(row[0]), "period_start": row[1], "period_end": row[2]}

    def record_event(self, venue: str, staff: str, category: str, delta: int):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO events (venue, staff, category, delta, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (venue, staff, category, int(delta), _now_local_iso()),
            )

    def _venue_events(self, venue: str, staff: str = None):
        """会場（と任意でスタッフ）に絞ったイベント行を返す。"""
        sql = "SELECT staff, category, delta FROM events WHERE venue = ?"
        params = [venue]
        if staff is not None:
            sql += " AND staff = ?"
            params.append(staff)
        with closing(self._conn()) as conn:
            return conn.execute(sql, params).fetchall()

    def get_venue_total(self, venue: str) -> int:
        return sum(r[2] for r in self._venue_events(venue))

    def get_my_counts(self, venue: str, staff: str) -> dict:
        counts = {c: 0 for c in CATEGORIES}
        for _staff, category, delta in self._venue_events(venue, staff):
            if category in counts:
                counts[category] += int(delta)
        return counts

    def get_breakdown(self, venue: str) -> list:
        return _aggregate_breakdown(self._venue_events(venue))

    def get_venues(self) -> list:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT venue FROM venues ORDER BY venue"
            ).fetchall()
        return [r[0] for r in rows]


class SupabaseBackend:
    """本番用。Supabase(Postgres)を共有DBとして使う。

    集計（合計・内訳）は、会場に絞ったイベント行を取得してPython側で
    合算する方式（RPC/ビュー不要でスキーマがシンプル）。規模が大きく
    なったらPostgres側のビュー/集計関数に寄せる余地を残している。
    """

    def __init__(self, url: str, key: str):
        # supabase-py は遅延import（ローカルSQLite運用では不要なため）
        from supabase import create_client

        self.client = create_client(url, key)

    def set_venue(self, venue: str, target: int, period_start: str, period_end: str):
        self.client.table("venues").upsert(
            {
                "venue": venue,
                "target": int(target),
                "period_start": period_start,
                "period_end": period_end,
                "updated_at": _now_utc_iso(),
            }
        ).execute()

    def get_venue_meta(self, venue: str):
        res = (
            self.client.table("venues")
            .select("target,period_start,period_end")
            .eq("venue", venue)
            .limit(1)
            .execute()
        )
        if res.data:
            r = res.data[0]
            return {
                "target": int(r["target"]),
                "period_start": r.get("period_start"),
                "period_end": r.get("period_end"),
            }
        return None

    def record_event(self, venue: str, staff: str, category: str, delta: int):
        self.client.table("events").insert(
            {
                "venue": venue,
                "staff": staff,
                "category": category,
                "delta": int(delta),
                "created_at": _now_utc_iso(),
            }
        ).execute()

    def _venue_events(self, venue: str, staff: str = None):
        q = (
            self.client.table("events")
            .select("staff,category,delta")
            .eq("venue", venue)
        )
        if staff is not None:
            q = q.eq("staff", staff)
        res = q.execute()
        return [(r["staff"], r["category"], r["delta"]) for r in (res.data or [])]

    def get_venue_total(self, venue: str) -> int:
        return sum(r[2] for r in self._venue_events(venue))

    def get_my_counts(self, venue: str, staff: str) -> dict:
        counts = {c: 0 for c in CATEGORIES}
        for _staff, category, delta in self._venue_events(venue, staff):
            if category in counts:
                counts[category] += int(delta)
        return counts

    def get_breakdown(self, venue: str) -> list:
        return _aggregate_breakdown(self._venue_events(venue))

    def get_venues(self) -> list:
        res = self.client.table("venues").select("venue").order("venue").execute()
        return [r["venue"] for r in (res.data or [])]


def _aggregate_breakdown(rows) -> list:
    """(staff, category, delta) の行リストをスタッフ別内訳に集約する。

    戻り値: [{"担当者": 名前, "機種変更": n, ..., "合計": n}, ...]（合計降順）
    """
    table = {}
    for staff, category, delta in rows:
        if staff not in table:
            table[staff] = {c: 0 for c in CATEGORIES}
        if category in table[staff]:
            table[staff][category] += int(delta)

    result = []
    for staff, counts in table.items():
        row = {"担当者": staff}
        row.update(counts)
        row["合計"] = sum(counts.values())
        result.append(row)

    result.sort(key=lambda r: r["合計"], reverse=True)
    return result


# --- バックエンドのシングルトン管理 -----------------------------------------

_SUPABASE_BACKEND = None


def _get_supabase_backend():
    """Supabaseバックエンドを1度だけ生成して使い回す。"""
    global _SUPABASE_BACKEND
    if _SUPABASE_BACKEND is None:
        conf = _secrets_section("supabase")
        _SUPABASE_BACKEND = SupabaseBackend(conf["url"], conf["key"])
    return _SUPABASE_BACKEND


def get_backend():
    """現在のモードに応じたバックエンドを返す。"""
    if get_backend_mode() == "supabase":
        return _get_supabase_backend()
    # SQLiteは生成が軽いので都度作る（DB_PATH差し替えにも追従できる）
    return SQLiteBackend(DB_PATH)


# --- UI側から呼ぶ薄いラッパー（呼び名は従来どおり据え置き） -----------------

def init_db():
    get_backend()  # 生成時にテーブル自動作成（SQLite）


def set_venue(venue: str, target: int, period_start: str, period_end: str):
    get_backend().set_venue(venue, int(target), period_start, period_end)


def get_venue_meta(venue: str):
    return get_backend().get_venue_meta(venue)


def record_event(venue: str, staff: str, category: str, delta: int):
    get_backend().record_event(venue, staff, category, int(delta))
    # 将来のNotion同期フックはここではなく、レート制限を避けるため
    # 「集約→定期同期」方式にした（sync_breakdown_to_notion を参照）。


def get_venue_total(venue: str) -> int:
    return get_backend().get_venue_total(venue)


def get_my_counts(venue: str, staff: str) -> dict:
    return get_backend().get_my_counts(venue, staff)


def get_breakdown(venue: str) -> list:
    return get_backend().get_breakdown(venue)


def get_venues() -> list:
    return get_backend().get_venues()


# ===========================================================================
# Notion同期（管理者ダッシュボード向け）
#   設計方針:
#     - 「+1」タップ1件ずつは送らない（Notion APIは約3req/秒制限のため）
#     - 代わりに「会場×スタッフ＝1行」に集約し、合計値を定期upsertする
#     - これでNotionは常に最新の内訳スナップショットを保持し、行も膨らまない
#   ※ライブ疎通テストはNotionトークン投入後（S2）に実施する
# ===========================================================================

# Notion側DBに用意するプロパティ（管理者が作成）:
#   会場        : タイトル or テキスト
#   担当者      : テキスト
#   機種変更/MNP/新規契約/LTV商材 : 数値
#   合計        : 数値
#   更新時刻    : テキスト or 日付
# 会場×担当者の一意キーとして「会場 + " / " + 担当者」を _key プロパティ(テキスト)に持たせ、
# それで既存ページを検索してupsertする。

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def notion_configured() -> bool:
    return _secrets_section("notion") is not None


def _notion_headers():
    conf = _secrets_section("notion")
    return {
        "Authorization": f"Bearer {conf['token']}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _notion_date(period_start: str, period_end: str) -> dict:
    """開始〜終了をNotionの日付プロパティ形式に変換する（単日なら開始のみ）。"""
    if not period_start:
        return {"date": None}
    d = {"start": period_start}
    if period_end and period_end != period_start:
        d["end"] = period_end
    return {"date": d}


def _notion_props(venue, totals, total_all, staff_count, key, target, period_start, period_end) -> dict:
    """会場合計をNotionのプロパティ形式に変換する。"""
    def num(v):
        return {"number": int(v)}

    def txt(v):
        return {"rich_text": [{"text": {"content": str(v)}}]}

    return {
        "会場": {"title": [{"text": {"content": venue}}]},
        "担当者": txt(f"{staff_count}名"),  # 会場に立っているスタッフ人数
        "イベント期間": _notion_date(period_start, period_end),
        "期間目標値": num(target),  # 新規＋MNPの期間目標
        "機種変更": num(totals["機種変更"]),
        "MNP": num(totals["MNP"]),
        "新規契約": num(totals["新規契約"]),
        "LTV商材": num(totals["LTV商材"]),
        "合計": num(total_all),
        "更新時刻": txt(_now_local_iso()),
        "_key": txt(key),
    }


def sync_breakdown_to_notion(venue: str) -> int:
    """会場ごとに1行へ集約してNotionへ同期する（会場名＝一意キー）。

    全スタッフ分を会場単位で合算し、1ページに upsert（あれば更新・無ければ作成）。
    戻り値: 同期した会場数（0 or 1）。未設定時は 0（no-op）。
    """
    if not notion_configured():
        print("[notion] 未設定のためスキップ（ローカル検証モード）")
        return 0

    import requests  # 遅延import

    conf = _secrets_section("notion")
    db_id = conf["database_id"]
    headers = _notion_headers()
    breakdown = get_breakdown(venue)
    if not breakdown:
        return 0

    # 全スタッフ分を会場単位で合算
    totals = {c: 0 for c in CATEGORIES}
    for row in breakdown:
        for c in CATEGORIES:
            totals[c] += row[c]
    total_all = sum(totals.values())
    staff_count = len(breakdown)
    key = venue  # 会場名そのものを一意キーに

    meta = get_venue_meta(venue) or {}
    target = meta.get("target", 0) or 0
    period_start = meta.get("period_start")
    period_end = meta.get("period_end")

    # 既存ページ検索（_key＝会場名で一意化）
    query = requests.post(
        f"{NOTION_API}/databases/{db_id}/query",
        headers=headers,
        json={"filter": {"property": "_key", "rich_text": {"equals": key}}},
        timeout=15,
    )
    query.raise_for_status()
    results = query.json().get("results", [])
    props = _notion_props(
        venue, totals, total_all, staff_count, key, target, period_start, period_end
    )

    if results:  # 更新
        page_id = results[0]["id"]
        resp = requests.patch(
            f"{NOTION_API}/pages/{page_id}",
            headers=headers,
            json={"properties": props},
            timeout=15,
        )
    else:  # 新規作成
        resp = requests.post(
            f"{NOTION_API}/pages",
            headers=headers,
            json={"parent": {"database_id": db_id}, "properties": props},
            timeout=15,
        )
    resp.raise_for_status()
    return 1


# ---------------------------------------------------------------------------
# 画面自動更新（他スタッフの入力を定期反映）
# ---------------------------------------------------------------------------

def do_autorefresh(interval_ms: int, key: str):
    """画面を定期的に再実行して最新データを取り込む。"""
    try:
        from streamlit_autorefresh import st_autorefresh

        st_autorefresh(interval=interval_ms, key=key)
    except Exception:
        components.html(
            f"""
            <script>
                setTimeout(function() {{
                    window.parent.location.reload();
                }}, {interval_ms});
            </script>
            """,
            height=0,
        )


# ---------------------------------------------------------------------------
# UI: スタイル（大きなボタン・モバイル最適化）
# ---------------------------------------------------------------------------

def inject_css():
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.2rem; padding-bottom: 3rem; }
        div.stButton > button[kind="primary"] {
            height: 5.2rem;
            font-size: 1.35rem;
            font-weight: 700;
            border-radius: 16px;
            width: 100%;
        }
        div.stButton > button[kind="secondary"] {
            height: 2.0rem;
            font-size: 0.85rem;
            border-radius: 10px;
            width: 100%;
            opacity: 0.75;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# 画面0: ログイン（会場共通PIN）
# ---------------------------------------------------------------------------

def render_login():
    st.title("🔒 ログイン")
    st.caption("会場スタッフ用の合言葉（PIN）を入力してください。")
    with st.form("login_form"):
        pin = st.text_input("合言葉 / PIN", type="password")
        ok = st.form_submit_button("入る", type="primary", use_container_width=True)
    if ok:
        if check_pin(pin):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("合言葉が違います。")


# ---------------------------------------------------------------------------
# 画面1: 初期設定（セッション開始）
# ---------------------------------------------------------------------------

def render_setup():
    st.title("📲 実績入力アプリ")
    mode = get_backend_mode()
    st.caption(
        "イベント現場のリアルタイム実績共有"
        + ("　｜　☁ Supabase接続中" if mode == "supabase" else "　｜　💾 ローカル検証モード")
    )

    st.subheader("セッションを開始")

    # 既存の会場があればプルダウンで選べる（表記ゆれ・入力ミスによる会場分裂を防ぐ）
    existing = get_venues()
    NEW_LABEL = "＋ 新しい会場を入力"
    if existing:
        choice = st.selectbox("イベント会場名", [NEW_LABEL] + existing)
        if choice == NEW_LABEL:
            venue = st.text_input("新しい会場名", placeholder="例：高松ゆめタウン特設ブース")
        else:
            venue = choice
    else:
        venue = st.text_input("イベント会場名", placeholder="例：高松ゆめタウン特設ブース")

    staff = st.text_input("担当者名（あなたの名前）", placeholder="例：並木")

    # 既存会場を選んだ場合は、その目標・期間を初期値に（誤って上書きしないため）
    today = date.today()
    default_target, default_start, default_end = 30, today, today
    if venue and venue in existing:
        meta = get_venue_meta(venue)
        if meta:
            default_target = meta["target"] or 30
            if meta.get("period_start"):
                try:
                    default_start = date.fromisoformat(meta["period_start"])
                except ValueError:
                    pass
            if meta.get("period_end"):
                try:
                    default_end = date.fromisoformat(meta["period_end"])
                except ValueError:
                    pass

    c1, c2 = st.columns(2)
    period_start = c1.date_input("イベント開始日", value=default_start, format="YYYY/MM/DD")
    period_end = c2.date_input("イベント終了日", value=default_end, format="YYYY/MM/DD")

    target = st.number_input(
        "期間目標値（新規＋MNPの合計目標件数）",
        min_value=1,
        max_value=100000,
        value=int(default_target),
        step=1,
        help="この派遣期間トータルの「新規＋MNP」の目標件数です。会場のスタッフで共有され、あとから変更もできます。",
    )

    if st.button("この会場で開始する", type="primary", use_container_width=True):
        if not venue or not venue.strip() or not staff.strip():
            st.error("会場名と担当者名を入力してください。")
            return
        if period_end < period_start:
            st.error("終了日は開始日以降にしてください。")
            return

        venue = venue.strip()
        staff = staff.strip()
        set_venue(venue, int(target), period_start.isoformat(), period_end.isoformat())

        st.session_state["venue"] = venue
        st.session_state["staff"] = staff
        st.session_state["configured"] = True
        st.rerun()


# ---------------------------------------------------------------------------
# 画面2: メイン（入力 + ダッシュボードが1画面で完結）
# ---------------------------------------------------------------------------

def render_main():
    venue = st.session_state["venue"]
    staff = st.session_state["staff"]

    do_autorefresh(AUTO_REFRESH_MS, key="dashboard_refresh")

    meta = get_venue_meta(venue) or {}
    target = meta.get("target", 0) or 0
    period_start = meta.get("period_start")
    period_end = meta.get("period_end")
    period_label = ""
    if period_start:
        period_label = period_start.replace("-", "/")
        if period_end and period_end != period_start:
            period_label += "〜" + period_end.replace("-", "/")

    # ===== ヘッダー =====
    top_l, top_r = st.columns([3, 1])
    with top_l:
        st.subheader(f"🏬 {venue}")
        cap = f"担当：{staff}"
        if period_label:
            cap += f"　｜　📅 {period_label}"
        cap += f"　｜　{AUTO_REFRESH_MS // 1000}秒ごとに自動更新"
        st.caption(cap)
    with top_r:
        if st.button("設定変更", use_container_width=True):
            st.session_state["configured"] = False
            st.rerun()

    # ===== リアルタイムダッシュボード（新規＋MNP を目標に）=====
    breakdown = get_breakdown(venue)
    nm_total = sum(r["MNP"] + r["新規契約"] for r in breakdown)  # 新規＋MNP
    all_total = get_venue_total(venue)  # 全商材合計（参考）
    rate = (nm_total / target * 100) if target > 0 else 0.0
    remaining = max(target - nm_total, 0)

    st.markdown("### 📊 会場全体の進捗（新規＋MNP）")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("新規＋MNP", f"{nm_total} 件")
    m2.metric("期間目標", f"{target} 件")
    m3.metric("達成率", f"{rate:.0f}%")
    m4.metric("残り", f"{remaining} 件")

    st.progress(min(nm_total / target, 1.0) if target > 0 else 0.0)
    st.caption(f"※全商材合計（機種変更・LTV含む）：{all_total} 件")
    if target > 0 and nm_total >= target:
        st.success("🎉 期間目標達成！ナイスファイト！")

    st.divider()

    # ===== 実績入力（自分の入力） =====
    st.markdown(f"### ➕ 実績を入力（{staff} さん）")
    my_counts = get_my_counts(venue, staff)

    grid = [CATEGORIES[i:i + 2] for i in range(0, len(CATEGORIES), 2)]
    for row in grid:
        cols = st.columns(len(row))
        for col, category in zip(cols, row):
            with col:
                icon = CATEGORY_ICONS.get(category, "")
                current = my_counts.get(category, 0)
                if st.button(
                    f"{icon} {category}\n＋1（現在 {current}）",
                    key=f"plus_{category}",
                    type="primary",
                    use_container_width=True,
                ):
                    record_event(venue, staff, category, +1)
                    st.rerun()
                if st.button(
                    "−1 修正",
                    key=f"minus_{category}",
                    type="secondary",
                    use_container_width=True,
                ):
                    if current > 0:
                        record_event(venue, staff, category, -1)
                    st.rerun()

    my_total = sum(my_counts.values())
    st.caption(f"あなたの合計：{my_total} 件")

    st.divider()

    # ===== スタッフ別の内訳一覧 =====
    st.markdown("### 👥 スタッフ別の内訳")
    breakdown = get_breakdown(venue)
    if not breakdown:
        st.info("まだ実績がありません。最初の1件を入力してみましょう。")
    else:
        import pandas as pd

        df = pd.DataFrame(breakdown)
        col_order = ["担当者"] + CATEGORIES + ["合計"]
        df = df[[c for c in col_order if c in df.columns]]
        st.dataframe(df, use_container_width=True, hide_index=True)

    # ===== 管理者用：Notion同期 =====
    if notion_configured():
        st.divider()
        with st.expander("🗂 管理者用：Notionへ同期"):
            st.caption("会場×スタッフの合計をNotionへ集約送信します（報告用）。")
            if st.button("今すぐNotionに同期する"):
                try:
                    n = sync_breakdown_to_notion(venue)
                    st.success(f"Notionへ {n} 名分を同期しました。")
                except Exception as e:
                    st.error(f"同期に失敗しました: {e}")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="実績入力アプリ",
        page_icon="📲",
        layout="centered",
        initial_sidebar_state="collapsed",
    )
    inject_css()
    init_db()

    # 公開URLの野良アクセス対策：PIN設定時はログインを要求
    if auth_required() and not st.session_state.get("authed"):
        render_login()
        return

    if st.session_state.get("configured"):
        render_main()
    else:
        render_setup()


if __name__ == "__main__":
    main()
