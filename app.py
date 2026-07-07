"""
イベント現場 リアルタイム実績入力アプリ
=====================================================

携帯販売のイベント現場で、スタッフがスマホからワンタップで獲得実績を入力し、
会場全体の目標達成率（新規＋MNP）をリアルタイム共有するアプリ。

■ データ単位（B案）
    「イベント（会場 × 開催期間）」を1単位とする。
    同じ会場でも期間が違えば別イベントとして履歴が残る（月別・期間別の振り返り可）。
    内部キー event_key = "会場|開始日|終了日"。

■ アーキテクチャ
    スマホ（Streamlit）→ Supabase（リアルタイムDB）→ Notion（管理者ダッシュボード）
    - secretsに[supabase]があれば本番、無ければローカルSQLite（自動切替）

起動:
    pip install -r requirements.txt
    streamlit run app.py
"""

import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

# Streamlitはアプリ実行時のみ必要。Notion自動同期スクリプト（CI）では未使用なので
# import できなくても動くようにガードする。
try:
    import streamlit as st
    import streamlit.components.v1 as components
except ImportError:  # pragma: no cover
    st = None
    components = None

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# 固定項目（KPI「新規＋MNP」の対象）。これ以外はすべて自由項目。
FIXED_CATEGORIES = ["MNP", "新規契約"]
FIXED_ICONS = {"MNP": "🔁", "新規契約": "✨"}
FIXED_SLUG = {"MNP": "mnp", "新規契約": "shinki"}
CUSTOM_ICON = "🏷"
# 自由項目ボタンの色（項目ごとに巡回して色分け）
CUSTOM_PALETTE = [
    ("#F59E0B", "#D9860A"), ("#06B6D4", "#0894AE"), ("#EC4899", "#C93080"),
    ("#14B8A6", "#0F9488"), ("#F97316", "#D75E0C"), ("#A855F7", "#8B39D4"),
    ("#84CC16", "#69A310"),
]

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "event_sales.db")
AUTO_REFRESH_MS = 5000


# ---------------------------------------------------------------------------
# 設定（secrets）・共通ヘルパー
# ---------------------------------------------------------------------------

def _secrets_section(name: str):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return None


def get_backend_mode() -> str:
    forced = os.environ.get("EVENT_APP_BACKEND")
    if forced in ("sqlite", "supabase"):
        return forced
    return "supabase" if _secrets_section("supabase") else "sqlite"


def auth_required() -> bool:
    return _secrets_section("auth") is not None


def check_pin(entered: str) -> bool:
    conf = _secrets_section("auth")
    if not conf:
        return True
    return str(entered).strip() == str(conf.get("pin", "")).strip()


def get_role(entered):
    """合言葉から権限を判定。'admin' / 'staff' / None（不一致）。

    - [auth]なし（ローカル）→ 全機能(admin)
    - admin_pin一致 → admin
    - pin一致 → admin_pin未設定なら後方互換でadmin、設定済みならstaff
    """
    conf = _secrets_section("auth")
    if not conf:
        return "admin"
    e = str(entered).strip()
    admin_pin = str(conf.get("admin_pin", "")).strip()
    staff_pin = str(conf.get("pin", "")).strip()
    if admin_pin and e == admin_pin:
        return "admin"
    if staff_pin and e == staff_pin:
        return "staff" if admin_pin else "admin"
    return None


def is_admin() -> bool:
    return st.session_state.get("role", "admin") == "admin"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_local_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def make_event_key(venue: str, period_start: str, period_end: str) -> str:
    """イベントの一意キー（会場×期間）。"""
    return f"{venue}|{period_start}|{period_end}"


def fmt_period(period_start: str, period_end: str) -> str:
    """'2026-07-13'〜'2026-07-15' を '2026/07/13〜2026/07/15' に整形（単日は片方のみ）。"""
    if not period_start:
        return ""
    s = period_start.replace("-", "/")
    if period_end and period_end != period_start:
        s += "〜" + period_end.replace("-", "/")
    return s


def fmt_jst(ts) -> str:
    """created_at（ローカル/UTC iso）を 'MM/DD HH:MM'（JST）に整形。"""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone(timedelta(hours=9)))
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return str(ts)


# ===========================================================================
# データ層（バックエンド）
#   config表（venues）: event_key(PK), venue, target, period_start, period_end
#   tap表  （events） : id, event_key, venue, staff, category, delta, created_at
# ===========================================================================

class SQLiteBackend:
    """ローカル検証用。"""

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
                    event_key    TEXT PRIMARY KEY,
                    venue        TEXT NOT NULL,
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
                    event_key  TEXT    NOT NULL,
                    venue      TEXT    NOT NULL,
                    staff      TEXT    NOT NULL,
                    category   TEXT    NOT NULL,
                    delta      INTEGER NOT NULL,
                    created_at TEXT    NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_key ON events(event_key)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key  TEXT NOT NULL,
                    staff      TEXT NOT NULL,
                    text       TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_key ON notes(event_key)")

    def set_event(self, event_key, venue, target, period_start, period_end):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO venues (event_key, venue, target, period_start, period_end, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    venue = excluded.venue,
                    target = excluded.target,
                    period_start = excluded.period_start,
                    period_end = excluded.period_end,
                    updated_at = excluded.updated_at
                """,
                (event_key, venue, int(target), period_start, period_end, _now_local_iso()),
            )

    def get_event_meta(self, event_key):
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT venue, target, period_start, period_end FROM venues WHERE event_key = ?",
                (event_key,),
            ).fetchone()
        if not row:
            return None
        return {"venue": row[0], "target": int(row[1]), "period_start": row[2], "period_end": row[3]}

    def list_events(self):
        with closing(self._conn()) as conn:
            rows = conn.execute(
                """
                SELECT event_key, venue, period_start, period_end, target
                FROM venues ORDER BY period_start DESC, venue
                """
            ).fetchall()
        return [
            {"event_key": r[0], "venue": r[1], "period_start": r[2], "period_end": r[3], "target": int(r[4])}
            for r in rows
        ]

    def record_event(self, event_key, venue, staff, category, delta):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO events (event_key, venue, staff, category, delta, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_key, venue, staff, category, int(delta), _now_local_iso()),
            )

    def _event_rows(self, event_key, staff=None):
        sql = "SELECT staff, category, delta FROM events WHERE event_key = ?"
        params = [event_key]
        if staff is not None:
            sql += " AND staff = ?"
            params.append(staff)
        with closing(self._conn()) as conn:
            return conn.execute(sql, params).fetchall()

    def get_total(self, event_key):
        return sum(r[2] for r in self._event_rows(event_key))

    def get_my_counts(self, event_key, staff):
        counts = {}
        for _s, category, delta in self._event_rows(event_key, staff):
            counts[category] = counts.get(category, 0) + int(delta)
        return counts

    def get_category_totals(self, event_key):
        totals = {}
        for _s, category, delta in self._event_rows(event_key):
            totals[category] = totals.get(category, 0) + int(delta)
        return totals

    def list_venue_categories(self, venue):
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT category FROM events WHERE venue = ?", (venue,)
            ).fetchall()
        return [r[0] for r in rows]

    def list_venue_staff(self, venue):
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT staff FROM events WHERE venue = ?", (venue,)
            ).fetchall()
        return [r[0] for r in rows]

    def add_note(self, event_key, staff, text):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO notes (event_key, staff, text, created_at) VALUES (?, ?, ?, ?)",
                (event_key, staff, text, _now_local_iso()),
            )

    def list_notes(self, event_key, limit=50):
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT staff, text, created_at FROM notes WHERE event_key = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (event_key, limit),
            ).fetchall()
        return [{"staff": r[0], "text": r[1], "created_at": r[2]} for r in rows]

    def get_hourly_kpi(self, event_key):
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT created_at, category, delta FROM events WHERE event_key = ?",
                (event_key,),
            ).fetchall()
        return _aggregate_hourly(rows)

    def list_event_records(self, event_key, limit=1000):
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT created_at, staff, category, delta FROM events "
                "WHERE event_key = ? ORDER BY created_at DESC LIMIT ?",
                (event_key, limit),
            ).fetchall()
        return [
            {"created_at": r[0], "staff": r[1], "category": r[2], "delta": r[3]}
            for r in rows
        ]

    def get_breakdown(self, event_key):
        return _aggregate_breakdown(self._event_rows(event_key))


class SupabaseBackend:
    """本番用（Supabase/Postgres）。集計はPython側で合算。"""

    def __init__(self, url: str, key: str):
        from supabase import create_client
        self.client = create_client(url, key)

    def set_event(self, event_key, venue, target, period_start, period_end):
        self.client.table("venues").upsert(
            {
                "event_key": event_key,
                "venue": venue,
                "target": int(target),
                "period_start": period_start,
                "period_end": period_end,
                "updated_at": _now_utc_iso(),
            }
        ).execute()

    def get_event_meta(self, event_key):
        res = (
            self.client.table("venues")
            .select("venue,target,period_start,period_end")
            .eq("event_key", event_key)
            .limit(1)
            .execute()
        )
        if res.data:
            r = res.data[0]
            return {
                "venue": r["venue"],
                "target": int(r["target"]),
                "period_start": r.get("period_start"),
                "period_end": r.get("period_end"),
            }
        return None

    def list_events(self):
        res = (
            self.client.table("venues")
            .select("event_key,venue,period_start,period_end,target")
            .order("period_start", desc=True)
            .execute()
        )
        return [
            {
                "event_key": r["event_key"],
                "venue": r["venue"],
                "period_start": r.get("period_start"),
                "period_end": r.get("period_end"),
                "target": int(r["target"]),
            }
            for r in (res.data or [])
        ]

    def record_event(self, event_key, venue, staff, category, delta):
        self.client.table("events").insert(
            {
                "event_key": event_key,
                "venue": venue,
                "staff": staff,
                "category": category,
                "delta": int(delta),
                "created_at": _now_utc_iso(),
            }
        ).execute()

    def _event_rows(self, event_key, staff=None):
        q = self.client.table("events").select("staff,category,delta").eq("event_key", event_key)
        if staff is not None:
            q = q.eq("staff", staff)
        res = q.execute()
        return [(r["staff"], r["category"], r["delta"]) for r in (res.data or [])]

    def get_total(self, event_key):
        return sum(r[2] for r in self._event_rows(event_key))

    def get_my_counts(self, event_key, staff):
        counts = {}
        for _s, category, delta in self._event_rows(event_key, staff):
            counts[category] = counts.get(category, 0) + int(delta)
        return counts

    def get_category_totals(self, event_key):
        totals = {}
        for _s, category, delta in self._event_rows(event_key):
            totals[category] = totals.get(category, 0) + int(delta)
        return totals

    def list_venue_categories(self, venue):
        res = self.client.table("events").select("category").eq("venue", venue).execute()
        seen = []
        for r in (res.data or []):
            if r["category"] not in seen:
                seen.append(r["category"])
        return seen

    def list_venue_staff(self, venue):
        res = self.client.table("events").select("staff").eq("venue", venue).execute()
        seen = []
        for r in (res.data or []):
            if r["staff"] not in seen:
                seen.append(r["staff"])
        return seen

    def add_note(self, event_key, staff, text):
        self.client.table("notes").insert(
            {"event_key": event_key, "staff": staff, "text": text, "created_at": _now_utc_iso()}
        ).execute()

    def list_notes(self, event_key, limit=50):
        # notesテーブル未作成でもアプリが落ちないよう防御
        try:
            res = (
                self.client.table("notes")
                .select("staff,text,created_at")
                .eq("event_key", event_key)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return [
                {"staff": r["staff"], "text": r["text"], "created_at": r.get("created_at")}
                for r in (res.data or [])
            ]
        except Exception:
            return []

    def get_hourly_kpi(self, event_key):
        res = (
            self.client.table("events")
            .select("created_at,category,delta")
            .eq("event_key", event_key)
            .execute()
        )
        return _aggregate_hourly(
            [(r["created_at"], r["category"], r["delta"]) for r in (res.data or [])]
        )

    def list_event_records(self, event_key, limit=1000):
        res = (
            self.client.table("events")
            .select("created_at,staff,category,delta")
            .eq("event_key", event_key)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [
            {
                "created_at": r.get("created_at"),
                "staff": r["staff"],
                "category": r["category"],
                "delta": r["delta"],
            }
            for r in (res.data or [])
        ]

    def get_breakdown(self, event_key):
        return _aggregate_breakdown(self._event_rows(event_key))


def _aggregate_hourly(rows) -> dict:
    """(created_at, category, delta) から時間帯別の新規＋MNP件数を集計。

    Supabaseの時刻はUTCなのでJST(+9)に変換してから時を取る。
    戻り値: {hour(0-23): 件数}。
    """
    hours = {}
    for ts, category, delta in rows:
        if category not in FIXED_CATEGORIES:  # KPI（新規＋MNP）のみ
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone(timedelta(hours=9)))
            h = dt.hour
        except Exception:
            continue
        hours[h] = hours.get(h, 0) + int(delta)
    return hours


def _aggregate_breakdown(rows) -> list:
    """(staff, category, delta) をスタッフ別内訳に集約（任意カテゴリ対応・合計降順）。"""
    table = {}
    for staff, category, delta in rows:
        table.setdefault(staff, {})
        table[staff][category] = table[staff].get(category, 0) + int(delta)
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
    global _SUPABASE_BACKEND
    if _SUPABASE_BACKEND is None:
        conf = _secrets_section("supabase")
        _SUPABASE_BACKEND = SupabaseBackend(conf["url"], conf["key"])
    return _SUPABASE_BACKEND


def get_backend():
    if get_backend_mode() == "supabase":
        return _get_supabase_backend()
    return SQLiteBackend(DB_PATH)


# --- UI側から呼ぶ薄いラッパー ----------------------------------------------

def init_db():
    get_backend()


def set_event(event_key, venue, target, period_start, period_end):
    get_backend().set_event(event_key, venue, int(target), period_start, period_end)


def get_event_meta(event_key):
    return get_backend().get_event_meta(event_key)


def list_events():
    return get_backend().list_events()


def record_event(event_key, venue, staff, category, delta):
    get_backend().record_event(event_key, venue, staff, category, int(delta))


def get_total(event_key):
    return get_backend().get_total(event_key)


def get_my_counts(event_key, staff):
    return get_backend().get_my_counts(event_key, staff)


def get_category_totals(event_key):
    return get_backend().get_category_totals(event_key)


def list_venue_categories(venue):
    return get_backend().list_venue_categories(venue)


def list_venue_staff(venue):
    return get_backend().list_venue_staff(venue)


def add_note(event_key, staff, text):
    get_backend().add_note(event_key, staff, text)


def list_notes(event_key, limit=50):
    return get_backend().list_notes(event_key, limit)


def get_hourly_kpi(event_key):
    return get_backend().get_hourly_kpi(event_key)


def list_event_records(event_key, limit=1000):
    return get_backend().list_event_records(event_key, limit)


def get_breakdown(event_key):
    return get_backend().get_breakdown(event_key)


# ===========================================================================
# Notion同期（管理者ダッシュボード向け）
#   1イベント（会場×期間）＝1行。event_key（非表示の_key列）でupsert。
#   連打は送らず、管理者の手動ボタン or 締めで同期する想定（レート制限回避）。
# ===========================================================================

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


def _notion_date(period_start, period_end):
    if not period_start:
        return {"date": None}
    d = {"start": period_start}
    if period_end and period_end != period_start:
        d["end"] = period_end
    return {"date": d}


def _notion_props(venue, totals, staff_count, target, period_start, period_end, event_key, memo=""):
    def num(v):
        return {"number": int(v)}

    def txt(v):
        return {"rich_text": [{"text": {"content": str(v)[:1900]}}]}

    # 固定以外（自由項目）を「機種変更×5, クレカ×3」形式の内訳テキストに集約
    customs = {c: v for c, v in totals.items() if c not in FIXED_CATEGORIES and v}
    naiyaku = ", ".join(f"{c}×{v}" for c, v in sorted(customs.items(), key=lambda x: -x[1]))
    # 合計は固定KPI（MNP＋新規契約）のみ。自由項目は含めない
    kpi_total = sum(totals.get(c, 0) for c in FIXED_CATEGORIES)

    month = (period_start or "")[:7]  # 'YYYY-MM'（月グループ化・フィルター用）
    return {
        "会場": {"title": [{"text": {"content": venue}}]},
        "担当者": txt(f"{staff_count}名"),
        "イベント期間": txt(fmt_period(period_start, period_end)),  # 〇/〇〜〇/〇 表記
        "月": txt(month),
        "期間目標値": num(target),
        "MNP": num(totals.get("MNP", 0)),
        "新規契約": num(totals.get("新規契約", 0)),
        "内訳": txt(naiyaku),
        "合計": num(kpi_total),
        "メモ": txt(memo),
        "更新時刻": txt(_now_local_iso()),
        "_key": txt(event_key),  # 会場×期間の一意キー（非表示推奨）
    }


def sync_event_to_notion(event_key: str) -> int:
    """1イベント（会場×期間）を1行へ集約してNotionへupsert。未設定時は0。"""
    if not notion_configured():
        print("[notion] 未設定のためスキップ（ローカル検証モード）")
        return 0

    import requests

    conf = _secrets_section("notion")
    db_id = conf["database_id"]
    headers = _notion_headers()

    meta = get_event_meta(event_key)
    if not meta:
        return 0

    totals = get_category_totals(event_key)
    staff_count = len(get_breakdown(event_key))

    # 共有メモを新しい順に集約（Notion「メモ」列へ）
    notes = list_notes(event_key)
    memo = "\n".join(f"・{n['staff']}：{n['text']}" for n in notes[:15])

    props = _notion_props(
        meta["venue"], totals, staff_count,
        meta["target"], meta["period_start"], meta["period_end"], event_key, memo,
    )

    query = requests.post(
        f"{NOTION_API}/databases/{db_id}/query",
        headers=headers,
        json={"filter": {"property": "_key", "rich_text": {"equals": event_key}}},
        timeout=15,
    )
    query.raise_for_status()
    results = query.json().get("results", [])

    if results:
        resp = requests.patch(
            f"{NOTION_API}/pages/{results[0]['id']}",
            headers=headers, json={"properties": props}, timeout=15,
        )
    else:
        resp = requests.post(
            f"{NOTION_API}/pages",
            headers=headers,
            json={"parent": {"database_id": db_id}, "properties": props},
            timeout=15,
        )
    resp.raise_for_status()
    return 1


# ===========================================================================
# AI機能（Claude API）— 日報自動生成・メモ要約
#   secretsに [ai] api_key があれば有効。無ければボタン非表示（no-op）。
#   モデルは claude-opus-4-8。課金が発生するためOwner承認のうえキーを設定する。
# ===========================================================================

AI_MODEL = "claude-opus-4-8"


def ai_configured() -> bool:
    return _secrets_section("ai") is not None


def _ai_client():
    import anthropic  # 遅延import

    conf = _secrets_section("ai")
    return anthropic.Anthropic(api_key=conf["api_key"])


def _ai_generate(prompt: str, max_tokens: int = 1500) -> str:
    client = _ai_client()
    resp = client.messages.create(
        model=AI_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _event_context_text(event_key: str) -> str:
    """AIに渡すイベントの実績サマリー文字列を組み立てる。"""
    meta = get_event_meta(event_key) or {}
    totals = get_category_totals(event_key)
    nm = sum(totals.get(c, 0) for c in FIXED_CATEGORIES)
    customs = {c: v for c, v in totals.items() if c not in FIXED_CATEGORIES and v}
    breakdown = get_breakdown(event_key)
    lines = [
        f"会場: {meta.get('venue', '')}",
        f"期間: {fmt_period(meta.get('period_start'), meta.get('period_end'))}",
        f"期間目標(新規+MNP): {meta.get('target', 0)}件",
        f"新規+MNP 実績: {nm}件（MNP {totals.get('MNP', 0)} / 新規契約 {totals.get('新規契約', 0)}）",
        f"その他内訳: {', '.join(f'{c}×{v}' for c, v in customs.items()) or 'なし'}",
        "スタッフ別: " + "／".join(
            f"{r['担当者']}(新規+MNP {r.get('MNP', 0) + r.get('新規契約', 0)})" for r in breakdown
        ),
    ]
    return "\n".join(lines)


def generate_daily_report(event_key: str) -> str:
    """数字＋共有メモから、店長向けの日報テキストを生成する。"""
    ctx = _event_context_text(event_key)
    notes = list_notes(event_key)
    memo = "\n".join(f"・{n['staff']}：{n['text']}" for n in notes[:20]) or "（メモなし）"
    prompt = (
        "あなたは携帯販売イベントの現場マネージャーです。以下の実績データと現場メモから、"
        "本部提出用の日報を日本語で作成してください。事実にない数字は創作しないこと。"
        "誇張・断定的な表現は避け、簡潔に。構成は【本日の実績サマリー】【所感・気づき】"
        "【明日への申し送り】の3見出し。日報本文のみを出力（前置き不要）。\n\n"
        f"# 実績データ\n{ctx}\n\n# 現場メモ\n{memo}"
    )
    return _ai_generate(prompt, max_tokens=1500)


def summarize_notes(event_key: str) -> str:
    """共有メモを「今日の会場傾向」に要約する。"""
    notes = list_notes(event_key)
    if not notes:
        return "（メモがありません）"
    memo = "\n".join(f"・{n['staff']}：{n['text']}" for n in notes[:40])
    prompt = (
        "以下は携帯販売イベント現場でスタッフが共有したメモです。会場の傾向・客層・"
        "気づきを日本語で3〜5行に要約してください。事実にない内容は加えないこと。"
        "要約本文のみを出力。\n\n" + memo
    )
    return _ai_generate(prompt, max_tokens=600)


# ---------------------------------------------------------------------------
# 画面自動更新
# ---------------------------------------------------------------------------

def do_autorefresh(interval_ms: int, key: str):
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=interval_ms, key=key)
    except Exception:
        components.html(
            f"<script>setTimeout(function(){{window.parent.location.reload();}}, {interval_ms});</script>",
            height=0,
        )


# ---------------------------------------------------------------------------
# スタイル
# ---------------------------------------------------------------------------

def inject_css():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        :root {
            --card: #161C2B;
            --card-border: rgba(255,255,255,0.07);
            --accent: #4F8DFD;
            --muted: #9AA6BF;
            --c-kishu: #3B82F6;   /* 機種変更 */
            --c-mnp:   #8B5CF6;   /* MNP */
            --c-shinki:#10B981;   /* 新規契約 */
            --c-ltv:   #F59E0B;   /* LTV商材 */
        }

        /* 全体のフォント・背景 */
        html, body, [class*="css"], .stApp { font-family: 'Inter', system-ui, sans-serif; }
        .stApp { background: linear-gradient(180deg, #0E1320 0%, #0B0F1A 100%); }
        .block-container { max-width: 680px; padding-top: 1.1rem; padding-bottom: 3.5rem; }

        /* Streamlitの余計なクロームを隠してスッキリ */
        #MainMenu, footer, [data-testid="stDecoration"] { display: none; }
        [data-testid="stHeader"] { background: transparent; }

        /* 見出し */
        h1, h2, h3 { font-weight: 800; letter-spacing: -0.01em; }
        .stMarkdown h3 {
            position: relative; padding-left: 0.7rem; margin-top: 0.4rem;
            font-size: 1.05rem; color: #EDF1F8;
        }
        .stMarkdown h3::before {
            content: ""; position: absolute; left: 0; top: 0.18em; bottom: 0.18em;
            width: 4px; border-radius: 4px;
            background: linear-gradient(180deg, #22C55E, #10B981);
        }

        /* メトリクスをカード化 */
        [data-testid="stMetric"] {
            background: var(--card); border: 1px solid var(--card-border);
            border-radius: 16px; padding: 14px 14px 10px;
            box-shadow: 0 4px 14px rgba(0,0,0,0.25);
        }
        [data-testid="stMetricLabel"] { opacity: 0.65; font-size: 0.78rem; font-weight: 600; }
        [data-testid="stMetricValue"] { font-size: 1.55rem; font-weight: 800; letter-spacing: -0.02em; }

        /* プログレスバー（塗りはテーマのprimaryColor=グリーンが自動で入る） */
        [data-testid="stProgress"] [role="progressbar"] {
            height: 12px; border-radius: 999px; background: rgba(255,255,255,0.08);
        }

        /* ボタン共通 */
        div.stButton > button {
            border: none; transition: transform .06s ease, box-shadow .2s ease, filter .2s ease;
        }
        div.stButton > button:active { transform: translateY(1px) scale(0.995); }

        /* ＋1 の大ボタン（primary） */
        /* 枠（ボタンの箱）＝固定。文字サイズは下の --btn-font だけで調整する */
        div.stButton > button[kind="primary"] {
            height: 5rem; padding: 0.5rem 0.6rem;
            font-weight: 800; border-radius: 16px; width: 100%; color: #fff;
            white-space: pre-line; line-height: 1.1;
            background: linear-gradient(180deg, #4F8DFD 0%, #3D6FE0 100%);
            box-shadow: 0 8px 20px rgba(79,141,253,0.28);
        }
        /* ★文字サイズはここだけ変える（枠は上で固定）★ */
        div.stButton > button[kind="primary"],
        div.stButton > button[kind="primary"] p,
        div.stButton > button[kind="primary"] div { font-size: 1.4rem !important; font-weight: 800 !important; }
        div.stButton > button[kind="primary"]:hover { filter: brightness(1.07); }

        /* −1 修正（secondary） */
        div.stButton > button[kind="secondary"] {
            height: 2.1rem; font-size: 0.82rem; border-radius: 10px; width: 100%;
            color: var(--muted); background: rgba(255,255,255,0.04);
            border: 1px solid var(--card-border);
        }
        div.stButton > button[kind="secondary"]:hover { color: #fff; background: rgba(255,255,255,0.09); }

        /* ボタン色分け：固定=MNP紫/新規緑、自由項目=アンバー（keyのst-key-で狙い撃ち） */
        .st-key-plus_mnp    button[kind="primary"] { background: linear-gradient(180deg,#8B5CF6,#7248D9) !important; box-shadow:0 8px 20px rgba(139,92,246,0.30) !important; }
        .st-key-plus_shinki button[kind="primary"] { background: linear-gradient(180deg,#10B981,#0C9A6C) !important; box-shadow:0 8px 20px rgba(16,185,129,0.30) !important; }
        /* 自由項目ボタンの色は render_main で項目ごとに動的注入（plus_c0, plus_c1...） */

        /* ボタン下の獲得数表示（自分／会場） */
        .catcnt { text-align:center; font-size:0.85rem; color:var(--muted); margin:4px 0 2px; }
        .catcnt b { font-size:1.35rem; color:#fff; font-weight:800; margin:0 3px; }
        .catcnt .sep { opacity:0.4; margin:0 6px; }

        /* スタッフ別ランキング */
        .rankrow { display:flex; align-items:center; gap:10px; padding:10px 12px; margin-bottom:6px;
            background:var(--card); border:1px solid var(--card-border); border-radius:12px; }
        .rankrow .rk { font-size:1.15rem; min-width:2.4rem; text-align:center; }
        .rankrow .rn { font-weight:700; flex:1; }
        .rankrow .rv { color:var(--muted); font-size:0.85rem; white-space:nowrap; }
        .rankrow .rv b { color:#22C55E; font-size:1.2rem; margin:0 2px; }

        /* 共有メモ */
        .memorow { padding:9px 12px; margin-bottom:6px; background:var(--card);
            border:1px solid var(--card-border); border-radius:12px; }
        .memorow .mt { color:var(--muted); font-size:0.72rem; margin-right:8px; }
        .memorow .ms { color:#4F8DFD; font-weight:700; font-size:0.85rem; margin-right:8px; }
        .memorow .mx { color:#E7ECF3; }

        /* 入力・セレクト・expander */
        [data-testid="stExpander"] { border: 1px solid var(--card-border); border-radius: 14px; background: var(--card); }
        [data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; }
        .stTextInput input, .stNumberInput input, [data-baseweb="select"] > div {
            border-radius: 10px !important;
        }
        hr { margin: 1.1rem 0; border-color: var(--card-border); }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# 画面0: ログイン
# ---------------------------------------------------------------------------

def render_login():
    st.title("🔒 ログイン")
    st.caption("会場スタッフ用の合言葉（PIN）を入力してください。")
    with st.form("login_form"):
        pin = st.text_input("合言葉 / PIN", type="password")
        ok = st.form_submit_button("入る", type="primary", use_container_width=True)
    if ok:
        role = get_role(pin)
        if role:
            st.session_state["authed"] = True
            st.session_state["role"] = role
            st.rerun()
        else:
            st.error("合言葉が違います。")


# ---------------------------------------------------------------------------
# 画面1: 初期設定（イベント選択 or 新規）
# ---------------------------------------------------------------------------

def render_setup():
    st.title("📲 実績入力アプリ")
    mode = get_backend_mode()
    st.caption(
        "イベント現場のリアルタイム実績共有"
        + ("　｜　☁ Supabase接続中" if mode == "supabase" else "　｜　💾 ローカル検証モード")
    )
    st.subheader("セッションを開始")

    events = list_events()

    # ===== 全会場ダッシュボード（管理者のみ・開催中は一覧／終了は会場ごとに履歴） =====
    if events and is_admin():
        import pandas as pd
        today_jst = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        ongoing = [e for e in events if not e.get("period_end") or e["period_end"] >= today_jst]
        ended = [e for e in events if e.get("period_end") and e["period_end"] < today_jst]

        def _summary_row(e, with_venue=True):
            t = get_category_totals(e["event_key"])
            nm = sum(t.get(c, 0) for c in FIXED_CATEGORIES)
            tgt = e["target"] or 0
            row = {"期間": fmt_period(e["period_start"], e["period_end"]),
                   "新規＋MNP": nm, "目標": tgt,
                   "達成率": f"{(nm / tgt * 100):.0f}%" if tgt else "—"}
            if with_venue:
                return {"会場": e["venue"], **row}
            return row

        with st.expander("📊 全会場ダッシュボード（開催中）", expanded=True):
            if ongoing:
                st.caption("「選択」を押すとそのイベントで開始できます。")
                for e in ongoing:
                    t = get_category_totals(e["event_key"])
                    nm = sum(t.get(c, 0) for c in FIXED_CATEGORIES)
                    tgt = e["target"] or 0
                    period = fmt_period(e["period_start"], e["period_end"])
                    c1, c2, c3 = st.columns([4, 3, 1.6])
                    c1.markdown(
                        f"**🏬 {e['venue']}**<br>"
                        f"<span style='color:#9AA6BF;font-size:0.78rem'>{period}</span>",
                        unsafe_allow_html=True,
                    )
                    c2.markdown(
                        f"新規＋MNP **{nm}** / {tgt}"
                        + (f"（{nm / tgt * 100:.0f}%）" if tgt else "")
                    )
                    if c3.button("選択", key=f"pick_{e['event_key']}", use_container_width=True):
                        st.session_state["event_choice"] = f"{e['venue']}（{period}）"
                        st.rerun()
            else:
                st.caption("開催中のイベントはありません。")
            st.caption(f"（JST {today_jst} 基準）")

        # 終了イベント：1つのプルダウンにまとめ、中で会場ごとに開閉（数字履歴・新しい順）
        if ended:
            by_venue = {}
            for e in ended:
                by_venue.setdefault(e["venue"], []).append(e)
            venues_sorted = sorted(
                by_venue.items(),
                key=lambda kv: max((e.get("period_start") or "") for e in kv[1]),
                reverse=True,
            )
            with st.expander(f"🏁 終了した会場（{len(venues_sorted)}会場）", expanded=False):
                for venue, evs in venues_sorted:
                    if st.checkbox(f"🏬 {venue}（{len(evs)}件）", key=f"ended_{venue}"):
                        evs_sorted = sorted(
                            evs, key=lambda x: (x.get("period_start") or ""), reverse=True
                        )
                        st.dataframe(
                            pd.DataFrame([_summary_row(e, with_venue=False) for e in evs_sorted]),
                            use_container_width=True, hide_index=True,
                        )

    NEW_LABEL = "＋ 新しいイベント"
    # 続きから候補：会場ごとに最新イベント1件だけ（list_eventsは開始日の新しい順）
    latest_by_venue = {}
    for e in events:
        latest_by_venue.setdefault(e["venue"], e)
    dropdown_events = list(latest_by_venue.values())
    labels = [NEW_LABEL] + [
        f"{e['venue']}（{fmt_period(e['period_start'], e['period_end'])}）" for e in dropdown_events
    ]
    # ダッシュボードの「選択」で入った値が古い場合は無効化（labelsに無ければ既定へ）
    if st.session_state.get("event_choice") not in labels:
        st.session_state.pop("event_choice", None)
    choice = st.selectbox("イベントを選択（続きから／新規）", labels, key="event_choice")

    today = date.today()
    if choice == NEW_LABEL:
        venue = st.text_input("会場名", placeholder="例：高松ゆめタウン特設ブース")
        c1, c2 = st.columns(2)
        period_start = c1.date_input("開始日", value=today, format="YYYY/MM/DD")
        period_end = c2.date_input("終了日", value=today, format="YYYY/MM/DD")
        target = st.number_input(
            "期間目標値（新規＋MNPの合計目標件数）",
            min_value=1, max_value=100000, value=30, step=1,
            help="この派遣期間トータルの「新規＋MNP」の目標件数です。",
        )
    else:
        e = dropdown_events[labels.index(choice) - 1]
        venue = e["venue"]
        period_start = date.fromisoformat(e["period_start"]) if e["period_start"] else today
        period_end = date.fromisoformat(e["period_end"]) if e["period_end"] else today
        target = e["target"]
        st.info(
            f"会場：{venue}\n\n期間：{fmt_period(e['period_start'], e['period_end'])}"
            f"\n\n期間目標値（新規＋MNP）：{target} 件"
        )

    # 担当者名：その会場で過去に入力があればプルダウン（表記ゆれ防止）
    existing_staff = list_venue_staff(venue) if venue and venue.strip() else []
    if existing_staff:
        NEW_STAFF = "＋ 新しい担当者"
        s_choice = st.selectbox("担当者名（あなたの名前）", [NEW_STAFF] + existing_staff)
        staff = st.text_input("新しい担当者名", placeholder="例：並木") if s_choice == NEW_STAFF else s_choice
    else:
        staff = st.text_input("担当者名（あなたの名前）", placeholder="例：並木")

    if st.button("このイベントで開始する", type="primary", use_container_width=True):
        if not venue or not venue.strip() or not staff.strip():
            st.error("会場名と担当者名を入力してください。")
            return
        if period_end < period_start:
            st.error("終了日は開始日以降にしてください。")
            return

        venue = venue.strip()
        staff = staff.strip()
        ps, pe = period_start.isoformat(), period_end.isoformat()
        event_key = make_event_key(venue, ps, pe)
        set_event(event_key, venue, int(target), ps, pe)

        st.session_state["event_key"] = event_key
        st.session_state["venue"] = venue
        st.session_state["staff"] = staff
        st.session_state["configured"] = True
        st.rerun()


# ---------------------------------------------------------------------------
# 画面2: メイン（入力 + ダッシュボード）
# ---------------------------------------------------------------------------

def render_main():
    event_key = st.session_state["event_key"]
    venue = st.session_state["venue"]
    staff = st.session_state["staff"]

    adding = st.session_state.get("adding_item", False)
    memo_mode = st.session_state.get("memo_mode", False)
    if not (adding or memo_mode):  # 入力・メモ記入中は自動更新を止める（フォーカス喪失防止）
        do_autorefresh(AUTO_REFRESH_MS, key="dashboard_refresh")

    meta = get_event_meta(event_key) or {}
    target = meta.get("target", 0) or 0
    period_label = fmt_period(meta.get("period_start"), meta.get("period_end"))

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

    # ===== ダッシュボード（新規＋MNP を目標に）=====
    breakdown = get_breakdown(event_key)
    totals = get_category_totals(event_key)
    nm_total = sum(totals.get(c, 0) for c in FIXED_CATEGORIES)  # 新規＋MNP
    all_total = sum(totals.values())
    rate = (nm_total / target * 100) if target > 0 else 0.0
    remaining = max(target - nm_total, 0)

    st.markdown("### 📊 会場全体の進捗（新規＋MNP）")
    r1 = st.columns(2)
    r1[0].metric("新規＋MNP", f"{nm_total} 件")
    r1[1].metric("期間目標", f"{target} 件")
    r2 = st.columns(2)
    r2[0].metric("達成率", f"{rate:.0f}%")
    r2[1].metric("残り", f"{remaining} 件")

    st.progress(min(nm_total / target, 1.0) if target > 0 else 0.0)
    st.caption(f"※全項目合計（自由項目含む）：{all_total} 件")
    if target > 0 and nm_total >= target:
        st.success("🎉 期間目標達成！ナイスファイト！")

    st.divider()

    # ===== 実績入力 =====
    st.markdown(f"### ➕ 実績を入力（{staff} さん）")
    my_counts = get_my_counts(event_key, staff)

    # 表示する項目 = 固定(MNP/新規) ＋ この会場で過去に使われた自由項目 ＋ 今セッションで追加した項目
    venue_cats = [c for c in list_venue_categories(venue) if c not in FIXED_CATEGORIES]
    session_new = st.session_state.get("new_items", [])
    custom_items = []
    for c in venue_cats + session_new:
        if c not in custom_items:
            custom_items.append(c)
    items = FIXED_CATEGORIES + custom_items

    # ボタンkey用のスラッグ（固定は専用色、自由項目は plus_c<n> で色を巡回）
    key_map, ci = {}, 0
    for c in items:
        if c in FIXED_SLUG:
            key_map[c] = FIXED_SLUG[c]
        else:
            key_map[c] = f"c{ci}"
            ci += 1

    # 自由項目ボタンを項目ごとに色分け（動的CSS注入）
    if custom_items:
        rules = ""
        for i in range(len(custom_items)):
            a, b = CUSTOM_PALETTE[i % len(CUSTOM_PALETTE)]
            rules += (
                f'.st-key-plus_c{i} button[kind="primary"]{{'
                f'background:linear-gradient(180deg,{a},{b})!important;'
                f'box-shadow:0 8px 20px {a}55!important;}}'
            )
        st.markdown(f"<style>{rules}</style>", unsafe_allow_html=True)

    st.caption("ボタンをタップで ＋1／下の「−1 修正」で取り消し。数字は「自分／会場全体」")
    grid = [items[i:i + 2] for i in range(0, len(items), 2)]
    for row in grid:
        cols = st.columns(len(row))
        for col, category in zip(cols, row):
            with col:
                icon = FIXED_ICONS.get(category, CUSTOM_ICON)
                slug = key_map[category]
                mine = my_counts.get(category, 0)
                vtot = totals.get(category, 0)
                if st.button(
                    f"{icon} {category}",
                    key=f"plus_{slug}", type="primary", use_container_width=True,
                ):
                    record_event(event_key, venue, staff, category, +1)
                    st.rerun()
                st.markdown(
                    f"<div class='catcnt'>自分 <b>{mine}</b><span class='sep'>|</span>会場 <b>{vtot}</b></div>",
                    unsafe_allow_html=True,
                )
                if st.button(
                    "−1 修正", key=f"minus_{slug}",
                    type="secondary", use_container_width=True,
                ):
                    if mine > 0:
                        record_event(event_key, venue, staff, category, -1)
                    st.rerun()

    st.caption(f"あなたの合計：{sum(my_counts.values())} 件")

    # ===== 自由項目の追加（追加中は自動更新を停止）=====
    if not adding:
        if st.button("＋ 項目を追加（自由入力）"):
            st.session_state["adding_item"] = True
            st.rerun()
    else:
        st.markdown("**＋ 項目を追加**")
        st.caption("MNP・新規契約以外の項目を追加できます（例：機種変更、クレカ、でんき）。同じ会場で追加した項目は次回から自動表示。追加中は自動更新を止めています。")
        new_name = st.text_input("項目名", key="new_item_input", placeholder="例：クレカ")
        a1, a2 = st.columns(2)
        if a1.button("追加する", type="primary", use_container_width=True):
            name = (new_name or "").strip()
            if not name:
                st.error("項目名を入力してください。")
            elif name in items or name in ("合計", "担当者"):
                st.warning("その項目はすでに使われています。")
            else:
                st.session_state.setdefault("new_items", []).append(name)
                st.session_state["adding_item"] = False
                st.rerun()
        if a2.button("キャンセル", use_container_width=True):
            st.session_state["adding_item"] = False
            st.rerun()

    st.divider()

    # ===== スタッフ別ランキング（新規＋MNP）=====
    st.markdown("### 🏆 スタッフ別ランキング（新規＋MNP）")
    if not breakdown:
        st.info("まだ実績がありません。最初の1件を入力してみましょう。")
    else:
        ranked = []
        for r in breakdown:
            nm = r.get("MNP", 0) + r.get("新規契約", 0)
            ranked.append((r["担当者"], nm, r["合計"]))
        ranked.sort(key=lambda x: (-x[1], -x[2]))
        medals = ["🥇", "🥈", "🥉"]
        for idx, (name, nm, tot) in enumerate(ranked):
            badge = medals[idx] if idx < 3 else f"{idx + 1}位"
            st.markdown(
                f"<div class='rankrow'><span class='rk'>{badge}</span>"
                f"<span class='rn'>{name}</span>"
                f"<span class='rv'>新規＋MNP <b>{nm}</b> ／ 全{tot}件</span></div>",
                unsafe_allow_html=True,
            )
        with st.expander("📋 項目別の内訳を見る"):
            import pandas as pd
            df = pd.DataFrame(breakdown).fillna(0)
            for c in items:  # 未入力の項目も列として0で表示
                if c not in df.columns:
                    df[c] = 0
            col_order = ["担当者"] + items + ["合計"]
            df = df[[c for c in col_order if c in df.columns]]
            for c in items + ["合計"]:
                if c in df.columns:
                    df[c] = df[c].astype(int)
            st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()

    # ===== 共有メモ（会場の雰囲気・客層・気づき）=====
    st.markdown("### 📝 共有メモ")
    if not memo_mode:
        if st.button("＋ メモを書く"):
            st.session_state["memo_mode"] = True
            st.rerun()
    else:
        st.caption("会場の雰囲気・お客様の層・気づいたことなど。記入中は自動更新を止めています。")
        memo_text = st.text_area("メモ", key="memo_input", height=100,
                                 placeholder="例：夕方から家族連れが増加。学割訴求が刺さる。")
        b1, b2 = st.columns(2)
        if b1.button("投稿する", type="primary", use_container_width=True):
            t = (memo_text or "").strip()
            if not t:
                st.error("メモを入力してください。")
            else:
                try:
                    add_note(event_key, staff, t)
                    st.session_state["memo_mode"] = False
                    st.rerun()
                except Exception:
                    st.error("メモの保存に失敗しました（Supabaseにnotesテーブルが未作成の可能性）。管理者にご連絡ください。")
        if b2.button("キャンセル", use_container_width=True):
            st.session_state["memo_mode"] = False
            st.rerun()

    notes = list_notes(event_key)
    if not notes:
        st.caption("まだメモはありません。")
    else:
        for n in notes[:30]:
            ts = (n.get("created_at") or "").replace("T", " ")[5:16]  # MM-DD HH:MM
            st.markdown(
                f"<div class='memorow'><span class='mt'>{ts}</span>"
                f"<span class='ms'>{n['staff']}</span>"
                f"<span class='mx'>{n['text']}</span></div>",
                unsafe_allow_html=True,
            )

    # ここから下は管理者のみ（現場スタッフには非表示）
    if not is_admin():
        return

    # ===== 管理者メニュー（分析・AI） =====
    st.divider()
    with st.expander("🛠 管理者メニュー（分析・AI）"):
        # 時間帯別グラフ（新規＋MNP）
        st.markdown("**⏰ 時間帯別の獲得（新規＋MNP）**")
        hourly = get_hourly_kpi(event_key)
        if hourly:
            import pandas as pd
            hs = sorted(hourly)
            rng = range(hs[0], hs[-1] + 1)
            df_h = pd.DataFrame(
                {"新規＋MNP": [hourly.get(h, 0) for h in rng]},
                index=[f"{h}時" for h in rng],
            )
            st.bar_chart(df_h)
        else:
            st.caption("まだ時間帯データがありません。")

        # AI日報・メモ要約
        st.divider()
        if ai_configured():
            c_ai1, c_ai2 = st.columns(2)
            if c_ai1.button("📝 AI日報を生成", use_container_width=True):
                with st.spinner("AIが日報を作成中…"):
                    try:
                        st.session_state["ai_report"] = generate_daily_report(event_key)
                    except Exception as e:
                        st.session_state["ai_report"] = f"生成に失敗しました: {e}"
                st.rerun()
            if c_ai2.button("🧠 メモをAI要約", use_container_width=True):
                with st.spinner("AIが要約中…"):
                    try:
                        st.session_state["ai_summary"] = summarize_notes(event_key)
                    except Exception as e:
                        st.session_state["ai_summary"] = f"要約に失敗しました: {e}"
                st.rerun()
            if st.session_state.get("ai_summary"):
                st.markdown("**🧠 メモ要約**")
                st.info(st.session_state["ai_summary"])
            if st.session_state.get("ai_report"):
                st.markdown("**📝 AI日報**")
                st.text_area("日報（コピーして提出）", st.session_state["ai_report"], height=300)
        else:
            st.caption("AI日報・メモ要約は、AnthropicのAPIキー設定後に有効になります（課金・管理者設定）。")

    # ===== 管理者用：Notion同期 =====
    if notion_configured():
        st.divider()
        with st.expander("🗂 管理者用：Notionへ同期"):
            st.caption("このイベント（会場×期間）の合計をNotionへ送信します（報告用）。")
            if st.button("今すぐNotionに同期する"):
                try:
                    n = sync_event_to_notion(event_key)
                    st.success(f"Notionへ {n} 件同期しました。")
                except Exception as e:
                    st.error(f"同期に失敗しました: {e}")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="実績入力アプリ", page_icon="📲",
        layout="centered", initial_sidebar_state="collapsed",
    )
    inject_css()
    init_db()

    if auth_required() and not st.session_state.get("authed"):
        render_login()
        return

    if st.session_state.get("configured"):
        render_main()
    else:
        render_setup()


if __name__ == "__main__":
    main()
