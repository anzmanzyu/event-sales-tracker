-- =====================================================================
-- イベント実績アプリ / Supabase スキーマ
-- Supabaseダッシュボード → SQL Editor に貼り付けて実行してください。
-- =====================================================================

-- 会場ごとの目標・イベント期間
--   target       : 期間目標値（新規＋MNPの合計目標件数）
--   period_start : イベント開始日 'YYYY-MM-DD'
--   period_end   : イベント終了日 'YYYY-MM-DD'
create table if not exists public.venues (
    venue        text primary key,
    target       integer not null default 0,
    period_start text,
    period_end   text,
    updated_at   timestamptz not null default now()
);

-- 既存DBに後から列を足す場合（マイグレーション）
alter table public.venues add column if not exists period_start text;
alter table public.venues add column if not exists period_end text;

-- 実績イベント（1タップ = 1行。+1 / -1 を delta で表現）
create table if not exists public.events (
    id         bigint generated always as identity primary key,
    venue      text    not null,
    staff      text    not null,
    category   text    not null,
    delta      integer not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_events_venue on public.events (venue);
create index if not exists idx_events_venue_staff on public.events (venue, staff);

-- ---------------------------------------------------------------------
-- RLS（行レベルセキュリティ）について
--   このアプリは anon key で読み書きします。まずは検証を優先し、
--   下記いずれかを選択してください。
--
--   【簡易】RLSを無効化（検証・小規模向け。誰でも読み書き可）
--     alter table public.venues disable row level security;
--     alter table public.events disable row level security;
--
--   【推奨・本番】RLSを有効化し、anonに必要な操作だけ許可
--     alter table public.venues enable row level security;
--     alter table public.events enable row level security;
--     create policy "anon read venues"  on public.venues for select to anon using (true);
--     create policy "anon upsert venues" on public.venues for insert to anon with check (true);
--     create policy "anon update venues" on public.venues for update to anon using (true) with check (true);
--     create policy "anon read events"   on public.events for select to anon using (true);
--     create policy "anon insert events" on public.events for insert to anon with check (true);
--   ※ events は insert のみ許可（改ざん・削除を防ぐ）。運用に合わせて調整可。
-- ---------------------------------------------------------------------
