"""
検証用の新しい標本を作る（DBは読み取りのみ）

プロンプト改善に使ったラベル済みの記事を除いた建設記事から、無作為な順に現在のプロンプトで処理し、
採用・除外がそれぞれ --per-class 件ずつ集まるまで続ける。改善に使っていない記事で精度を確かめるための標本。

出力（--out 配下の日時フォルダ）:
  - test_labels.xlsx : ラベル付け用シート（列の「LLM」は現在のプロンプトの出力）
  - raw.jsonl        : LLMの生出力（eval_construction_prompt.py --raw で採点に使う）
  - meta.json        : 母集団の件数と、処理した記事に占める採用の割合（重み付けの推定に使う）

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor_construction sh -c \
    "pip install -q openpyxl && python make_test_set.py --exclude-csv eval_results/labels/labels_20261006_173604.csv"
"""

import argparse
import csv
import json
import os
from datetime import datetime

from sqlalchemy import text

from database import SessionLocal
from eval_construction_prompt import predict_from_result
from llm_processor import PROMPT_VERSIONS, TARGETS, _build_prompt, _OllamaBackend, _parse_json
from make_label_sheet import translate_titles, write_sheet

POOL_SQL = """
SELECT id, target, title, url, source_domain, publish_date, body, left(coalesce(body, ''), 400) AS body_head
FROM articles
WHERE target LIKE 'large_scale%' AND publish_date >= :since AND NOT (id = ANY(:exclude))
ORDER BY md5(id::text || :seed)
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="検証用の新しい標本を作る")
    parser.add_argument("--exclude-csv", required=True, help="改善に使ったラベル標本のCSV（ここに含まれる記事は除く）")
    parser.add_argument("--per-class", type=int, default=25, help="採用・除外それぞれの件数")
    parser.add_argument("--since", default="2026-08-25", help="対象とする記事の観測日の下限")
    parser.add_argument("--seed", default="test-2026-10-07", help="標本を決める文字列")
    parser.add_argument("--max-process", type=int, default=400, help="処理する記事数の上限")
    parser.add_argument("--out", default="eval_results/test_set", help="出力先ディレクトリの親")
    args = parser.parse_args()

    with open(args.exclude_csv, encoding="utf-8-sig") as f:
        exclude = [int(r["id"]) for r in csv.DictReader(f)]
    session = SessionLocal()
    try:
        pool = [dict(r) for r in session.execute(text(POOL_SQL), {"since": args.since, "exclude": exclude,
                                                                   "seed": args.seed}).mappings().all()]
    finally:
        session.close()
    print(f"母集団 {len(pool)} 件（改善に使った {len(exclude)} 件を除く）", flush=True)

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    backend = _OllamaBackend()
    labels = {k: v["label"] for k, v in TARGETS.items()}

    kept, excluded, processed = [], [], 0
    with open(os.path.join(out_dir, "raw.jsonl"), "w", encoding="utf-8") as f:
        for a in pool:
            if (len(kept) >= args.per_class and len(excluded) >= args.per_class) or processed >= args.max_process:
                break
            prompt = _build_prompt(labels[a["target"]], a["title"] or "", a["source_domain"] or "",
                                   a["publish_date"].strftime("%Y-%m-%d"), a["body"] or "", True)
            result = _parse_json(backend.call(prompt)) or {"excluded": True}
            processed += 1
            f.write(json.dumps({"id": a["id"], "result": result}, ensure_ascii=False) + "\n")
            pred = predict_from_result(result, a)
            row = {**a, "excluded": pred["excluded"], "reason": pred["reason"], "status": None,
                   "building_name": pred["name"], "city": pred["city"],
                   "event_date": datetime.fromisoformat(pred["event_date"]).date() if pred["event_date"] else None,
                   "evidence": result.get("project_evidence")}
            # predict_from_result の状態は日本語なので、シート用に英語のコードを入れ直す
            row["status"] = result.get("construction_status")
            (excluded if pred["excluded"] else kept).append(row)
            if processed % 20 == 0:
                print(f"処理 {processed} 件（採用 {len(kept)} / 除外 {len(excluded)}）", flush=True)

    # 処理した全記事に占める採用の割合（母集団での採用率の推定）。標本は先頭から per-class 件ずつ使う
    kept_rate = len(kept) / processed if processed else 0
    sample = excluded[:args.per_class] + kept[:args.per_class]
    meta = {
        "prompt_version": PROMPT_VERSIONS["construction"],
        "pool": len(pool),
        "processed": processed,
        "kept_in_processed": len(kept),
        "kept_rate": round(kept_rate, 4),
        "kept_pop_estimate": round(len(pool) * kept_rate),
        "excluded_pop_estimate": round(len(pool) * (1 - kept_rate)),
        "sample_ids": [r["id"] for r in sample],
        "seed": args.seed,
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    translations = translate_titles({r["id"]: r["title"] or "" for r in sample})
    write_sheet(sample, translations, os.path.join(out_dir, "test_labels.xlsx"))
    print(json.dumps({k: v for k, v in meta.items() if k != "sample_ids"}, ensure_ascii=False))
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
