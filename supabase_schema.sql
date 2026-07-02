-- =====================================================================
-- イベント実績アプリ / Supabase スキーマ（B案：イベント＝会場×期間 単位）
-- Supabaseダッシュボード → SQL Editor に貼り付けて実行してください。
--
-- ⚠️ 旧スキーマ（会場単位）から移行する場合は、下の drop 2行で作り直します。
--    テストデータは消えます（本番データがある場合は事前にバックアップを）。
-- =====================================================================

drop table if exists public.events;
drop table if exists public.venues;

-- イベント設定（会場×期間ごとに1行）
--   event_key    : "会場|開始日|終了日" の一意キー
--   target       : 期間目標値（新規＋MNPの合計目標件数）
--   period_start / period_end : 'YYYY-MM-DD'
create table public.venues (
    event_key    text primary key,
    venue        text not null,
    target       integer not null default 0,
    period_start text,
    period_end   text,
    updated_at   timestamptz not null default now()
);

-- 実績イベント（1タップ = 1行。+1 / -1 を delta で表現）
create table public.events (
    id         bigint generated always as identity primary key,
    event_key  text    not null,
    venue      text    not null,
    staff      text    not null,
    category   text    not null,
    delta      integer not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_events_key on public.events (event_key);
create index if not exists idx_events_key_staff on public.events (event_key, staff);

-- RLS（anon で読み書き。events は削除不可＝改ざん防止）
alter table public.venues enable row level security;
alter table public.events enable row level security;

create policy "anon read venues"   on public.venues for select to anon using (true);
create policy "anon upsert venues" on public.venues for insert to anon with check (true);
create policy "anon update venues" on public.venues for update to anon using (true) with check (true);
create policy "anon read events"   on public.events for select to anon using (true);
create policy "anon insert events" on public.events for insert to anon with check (true);
