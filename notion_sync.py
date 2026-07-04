"""
Notion 自動同期スクリプト（GitHub Actions 用・スタンドアロン）

Supabase上の全イベントをNotionへ集約同期する。Streamlitは不要。
接続情報は環境変数から読む（GitHub Secretsで供給）:
    SUPABASE_URL / SUPABASE_KEY / NOTION_TOKEN / NOTION_DATABASE_ID

ローカル実行例:
    SUPABASE_URL=... SUPABASE_KEY=... NOTION_TOKEN=... NOTION_DATABASE_ID=... python notion_sync.py
"""

import os

# 本番バックエンドを強制（app import前に設定）
os.environ.setdefault("EVENT_APP_BACKEND", "supabase")

import app  # noqa: E402  (Streamlitが無くても import できるようガード済み)


def _env_secrets(name: str):
    """app._secrets_section を環境変数ベースに差し替える。"""
    if name == "supabase":
        return {"url": os.environ["SUPABASE_URL"], "key": os.environ["SUPABASE_KEY"]}
    if name == "notion":
        return {
            "token": os.environ["NOTION_TOKEN"],
            "database_id": os.environ["NOTION_DATABASE_ID"],
        }
    return None


def main():
    app._secrets_section = _env_secrets
    app._SUPABASE_BACKEND = None  # キャッシュをリセットして環境変数で再生成

    events = app.list_events()
    synced, errors = 0, 0
    for e in events:
        try:
            synced += app.sync_event_to_notion(e["event_key"])
        except Exception as ex:  # 1件失敗しても続行
            errors += 1
            print(f"[error] {e['event_key']}: {ex}")
    print(f"done: synced={synced} events, errors={errors}")


if __name__ == "__main__":
    main()
