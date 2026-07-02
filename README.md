# イベント実績入力アプリ

携帯販売のイベント現場で、スタッフがスマホからワンタップで獲得実績を入力し、
会場全体の目標達成率をリアルタイム共有するアプリ。夜の集計業務を自動化する。

## 特徴

- 📱 **モバイルファースト**：接客の合間に片手でワンタップ入力（機種変更 / MNP / 新規契約 / LTV商材）
- 📊 **リアルタイム共有**：会場全体の進捗バー・達成率・スタッフ別内訳を全員のスマホで同期
- 🔒 **PINログイン**：会場共通の合言葉で野良アクセスを防止
- 🗂 **Notion自動集約**：スタッフ別合計を管理者用Notionへ集約（報告用ダッシュボード）
- 🔐 **個人情報を一切扱わない**：獲得「件数」のみをカウント（コンプラリスクなし）

## 構成

```
スマホ（Streamlit）→ Supabase（リアルタイムDB）→ Notion（管理者ダッシュボード）
```

- フロント：Python / Streamlit
- リアルタイムDB：Supabase (Postgres)
- 管理・報告：Notion API
- ホスティング：Streamlit Community Cloud

## セットアップ

1. 依存インストール
   ```
   pip install -r requirements.txt
   ```
2. 接続情報を設定（`.streamlit/secrets.toml.example` を `secrets.toml` にコピーして記入）
   - `[auth]` … 会場共通PIN
   - `[supabase]` … URL / anon key（未設定ならローカルSQLiteで動作）
   - `[notion]` … token / database_id（Notion同期を使う場合）
3. Supabaseに `supabase_schema.sql` を適用（SQL Editor）
4. Notionは `docs/notion_setup_guide.md` に従って準備
5. 起動
   ```
   streamlit run app.py
   ```

## 動作モード

| secrets | 動作 |
|---|---|
| `[supabase]` あり | 本番モード（Supabase共有DB） |
| `[supabase]` なし | ローカル検証モード（SQLite） |
| `[auth]` あり | ログイン画面を表示 |
| `[notion]` あり | 管理者用のNotion同期ボタンを表示 |

## ライセンス / 位置づけ

Namiki AI Agency（CTO: Daigoro）による内製プロトタイプ。
