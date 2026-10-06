"""
人手ラベル付け用CSVの出力（DBは読み取りのみ）

最新プロンプト（construction.project_evidence を持つ世代）で処理した建設記事から、
採用・除外を同数ずつ無作為抽出し、人手で正解を記入するためのCSVを出力する。
抽出はidのハッシュ順で決定論的に行うため、同じseed・同じDBなら同じ標本になる。

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor_construction \
      python export_labels.py --kept 50 --excluded 50
"""

import argparse
import csv
import os
from datetime import datetime

from sqlalchemy import text

from database import SessionLocal

BASE_WHERE = """
    target LIKE 'large_scale%'
    AND is_llm_processed
    AND llm_analysis->'construction' ? 'project_evidence'
"""

SELECT_SQL = """
SELECT id, url, title, publish_date, source_domain, left(coalesce(body, ''), 300) AS body_head,
       (llm_analysis->>'excluded')::boolean AS excluded,
       llm_analysis->>'rating'            AS rating,
       llm_analysis->>'reason'            AS reason,
       llm_analysis->>'event_date_basis'  AS basis,
       event_date,
       llm_analysis->'construction'->>'status'           AS status,
       llm_analysis->'construction'->>'project_type'     AS project_type,
       llm_analysis->'construction'->>'building_name'    AS building_name,
       llm_analysis->'construction'->'location'->>'city' AS city,
       llm_analysis->'construction'->>'project_evidence' AS evidence,
       (llm_analysis->'construction'->>'evidence_found')::boolean AS evidence_found
FROM articles
WHERE {base} AND (llm_analysis->>'excluded')::boolean = :excluded
ORDER BY md5(id::text || :seed)
LIMIT :limit
"""

FIELDS = [
    # 記事そのもの（人が読んで判断するための情報）
    "id", "publish_date", "source_domain", "title", "url", "body_head",
    # LLMの出力（答え合わせの対象）
    "llm_excluded", "llm_rating", "llm_status", "llm_project_type",
    "llm_building_name", "llm_city", "llm_event_date", "llm_event_date_basis",
    "llm_evidence", "llm_evidence_found", "llm_reason",
    # 人が記入する列
    "human_excluded", "human_status", "human_building_name", "human_city",
    "human_event_date", "human_source_type", "human_note",
]


def _fetch(session, excluded: bool, limit: int, seed: str) -> list[dict]:
    sql = text(SELECT_SQL.format(base=BASE_WHERE))
    rows = session.execute(sql, {"excluded": excluded, "limit": limit, "seed": seed}).mappings().all()
    return [dict(r) for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="人手ラベル用CSVの出力（DB読み取りのみ）")
    parser.add_argument("--kept", type=int, default=50, help="採用（excluded=false）からの抽出件数")
    parser.add_argument("--excluded", type=int, default=50, help="除外（excluded=true）からの抽出件数")
    parser.add_argument("--seed", default="2026-10-06", help="標本を決める文字列。変えると別の標本になる")
    parser.add_argument("--out", default="eval_results/labels", help="出力先ディレクトリ")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        rows = _fetch(session, False, args.kept, args.seed) + _fetch(session, True, args.excluded, args.seed)
    finally:
        session.close()

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"labels_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

    with open(path, "w", encoding="utf-8-sig", newline="") as f:  # Excelで文字化けしないようBOM付き
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "id": r["id"],
                "publish_date": r["publish_date"].strftime("%Y-%m-%d") if r["publish_date"] else "",
                "source_domain": r["source_domain"],
                "title": r["title"],
                "url": r["url"],
                "body_head": (r["body_head"] or "").replace("\n", " "),
                "llm_excluded": r["excluded"],
                "llm_rating": r["rating"],
                "llm_status": r["status"],
                "llm_project_type": r["project_type"],
                "llm_building_name": r["building_name"],
                "llm_city": r["city"],
                "llm_event_date": r["event_date"],
                "llm_event_date_basis": r["basis"],
                "llm_evidence": r["evidence"],
                "llm_evidence_found": r["evidence_found"],
                "llm_reason": r["reason"],
            })

    print(f"{len(rows)} 件を出力しました: {path}")
    print("採用 {} 件 / 除外 {} 件（seed={}）".format(args.kept, args.excluded, args.seed))


if __name__ == "__main__":
    main()
