-- Phase 2.5 マイグレーション
-- 既存DBに対して実行する。新規DBには不要（init_db()が正しいスキーマを作成する）。
--
-- 実行方法:
--   docker exec gdelt_postgres psql -U gdelt_user -d gdelt_db -f /migration.sql
-- または:
--   docker exec -i gdelt_postgres psql -U gdelt_user -d gdelt_db < migration.sql

BEGIN;

-- 新カラムを追加（既に存在する場合はスキップ）
ALTER TABLE articles ADD COLUMN IF NOT EXISTS target VARCHAR;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS collection_mode VARCHAR DEFAULT 'analyze';
ALTER TABLE articles ADD COLUMN IF NOT EXISTS event_date DATE;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS title VARCHAR;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS source_domain VARCHAR;

-- 既存データを移行
UPDATE articles SET target = task_name         WHERE target IS NULL AND task_name IS NOT NULL;
UPDATE articles SET fetched_at = publish_date  WHERE fetched_at IS NULL;
UPDATE articles SET title = raw_data->>'title' WHERE title IS NULL;
UPDATE articles SET source_domain = raw_data->>'domain' WHERE source_domain IS NULL;

-- NOT NULL 制約を設定（データ移行後）
ALTER TABLE articles ALTER COLUMN target SET NOT NULL;
ALTER TABLE articles ALTER COLUMN collection_mode SET NOT NULL;
ALTER TABLE articles ALTER COLUMN fetched_at SET NOT NULL;

-- 旧カラムを削除
ALTER TABLE articles DROP COLUMN IF EXISTS task_name;

-- インデックスを作成
CREATE INDEX IF NOT EXISTS ix_articles_target        ON articles(target);
CREATE INDEX IF NOT EXISTS ix_articles_event_date    ON articles(event_date);
CREATE INDEX IF NOT EXISTS ix_articles_source_domain ON articles(source_domain);

-- 既存のLLM分析結果をリセット（新スキーマ: tone/causal/event_date/why_notable 追加のため）
-- ※ 再処理にGemini APIコストがかかる場合はコメントアウトして既存データを保持することも可
UPDATE articles
SET is_llm_processed = false, llm_analysis = null
WHERE is_llm_processed = true;

COMMIT;
