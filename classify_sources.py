"""
記事ごとの取材性（ソースの性格）の判定

建設ターゲットで「採用」された記事（最新プロンプト世代）について、ローカルLLMに
一次報道か・転載か・論説か・まとめか・プレスリリースか、関係者の実名コメントがあるかを判定させ、
llm_analysis["source_assessment"] に追記する。既存の分析項目は変更しない。
未判定の記事だけを処理するので、途中で止めても再実行すれば続きから再開する。

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor_construction \
      python classify_sources.py --limit 2000
  # 書き込まずに結果だけ見る
  ... python classify_sources.py --limit 5 --dry-run
"""

import argparse
import json
import time

from sqlalchemy import text

from database import SessionLocal
from llm_processor import OLLAMA_MODEL, _as_bool, _OllamaBackend, _parse_json

SOURCE_VERSION = "source-v1"
SOURCE_TYPES = {"original_reporting", "syndicated", "opinion", "aggregation", "press_release", "other"}
BODY_CHARS = 2500

SELECT_SQL = """
SELECT id, title, source_domain, left(coalesce(body, ''), :body_chars) AS body
FROM articles
WHERE target LIKE 'large_scale%'
  AND is_llm_processed
  AND llm_analysis->'construction' ? 'project_evidence'
  AND NOT (llm_analysis->>'excluded')::boolean
  AND NOT llm_analysis ? 'source_assessment'
ORDER BY id
LIMIT :limit
"""

UPDATE_SQL = """
UPDATE articles
SET llm_analysis = jsonb_set(llm_analysis, '{source_assessment}', CAST(:value AS jsonb))
WHERE id = :id
"""


def build_prompt(title: str, domain: str, body: str) -> str:
    body_text = body.strip() if body else "(本文取得不可 - タイトルのみで判断)"
    return f"""You are assessing how a news article was produced, to judge the reliability of its source.

Article:
- Title: {title}
- Source domain: {domain}
- Body: {body_text}

Return ONLY valid JSON with this exact structure:
{{
  "source_type": <"original_reporting", "syndicated", "opinion", "aggregation", "press_release", or "other">,
  "has_named_source": <true or false>,
  "has_direct_quote": <true or false>,
  "cites_document": <true or false>
}}

source_type:
- original_reporting: the outlet's own reporting, with its own reporter gathering facts (interviews, site visits, records)
- syndicated: a wire-service or partner article republished as-is (e.g. "(AP)", "Reuters", "via", "This story originally appeared in")
- opinion: an opinion piece, column, editorial, or analysis expressing the writer's view
- aggregation: a roundup or rewrite of other outlets' reports without new reporting
- press_release: a company or government announcement republished with little or no editing
- other: anything else (listings, advertorials, event notices)
has_named_source: true if a named official, executive, or other identified person is cited
has_direct_quote: true if the article contains a direct quotation from a source
cites_document: true if the article cites a specific official document, filing, permit, or report
"""


def sanitize(result: dict) -> dict:
    source_type = result.get("source_type")
    return {
        "source_type":      source_type if source_type in SOURCE_TYPES else "other",
        "has_named_source": _as_bool(result.get("has_named_source")),
        "has_direct_quote": _as_bool(result.get("has_direct_quote")),
        "cites_document":   _as_bool(result.get("cites_document")),
        "model":            OLLAMA_MODEL,
        "version":          SOURCE_VERSION,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="記事ごとの取材性の判定（source_assessment を追記）")
    parser.add_argument("--limit", type=int, default=2000, help="今回処理する最大件数")
    parser.add_argument("--dry-run", action="store_true", help="DBに書き込まず結果を表示するだけ")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        rows = session.execute(text(SELECT_SQL), {"limit": args.limit, "body_chars": BODY_CHARS}).mappings().all()
    finally:
        session.close()
    print(f"未判定の記事 {len(rows)} 件を処理します（dry_run={args.dry_run}）", flush=True)

    backend = _OllamaBackend()
    done, failed, started = 0, 0, time.time()
    for i, row in enumerate(rows, 1):
        try:
            result = _parse_json(backend.call(build_prompt(row["title"] or "", row["source_domain"] or "", row["body"] or "")))
        except Exception as e:
            print(f"[{i}/{len(rows)}] id={row['id']} LLM呼び出し失敗: {e}", flush=True)
            failed += 1
            continue
        if not result:
            failed += 1
            continue

        value = sanitize(result)
        if args.dry_run:
            print(f"[{i}] id={row['id']} {row['source_domain']} | {(row['title'] or '')[:60]} -> {value}", flush=True)
        else:
            session = SessionLocal()
            try:
                session.execute(text(UPDATE_SQL), {"id": row["id"], "value": json.dumps(value)})
                session.commit()
            finally:
                session.close()
        done += 1
        if i % 50 == 0:
            print(f"[{i}/{len(rows)}] 完了 {done} 件, 失敗 {failed} 件, 経過 {time.time() - started:.0f}秒", flush=True)

    print(f"終了: 完了 {done} 件, 失敗 {failed} 件, 経過 {time.time() - started:.0f}秒")


if __name__ == "__main__":
    main()
