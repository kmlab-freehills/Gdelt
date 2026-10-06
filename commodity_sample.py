"""
コモディティ記事の標本処理と集計

広いクエリ（monitor）は記事数が多く全件のLLM処理が現実的でないため、
  - 狭いクエリ（analyze）の記事は全件
  - 広いクエリ（monitor）の記事は品目ごとに --per-target 件を無作為抽出（idのハッシュ順で決定論的）
を標本とし、未処理のものだけをLLMで処理したうえで、品目×クエリ種別ごとに集計する。
標本の記事IDは出力先に保存する（同じ標本での再集計用）。

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor \
      python commodity_sample.py --from 2026-09-01 --per-target 50
  # LLM処理をせず集計だけ（標本ファイルを指定）
  ... python commodity_sample.py --ids-file eval_results/commodity/<日時>/sample_ids.json --summary-only
"""

import argparse
import json
import os
from collections import Counter
from datetime import datetime

from sqlalchemy import text

from database import SessionLocal
from llm_processor import _get_llm_backend, run_all

COMMODITIES = ["copper", "gold", "uranium", "rare earth", "natural gas", "silver"]

SAMPLE_SQL = """
SELECT id FROM articles
WHERE target = :target AND publish_date >= :since AND collection_mode = :mode
ORDER BY md5(id::text || :seed)
LIMIT :limit
"""

SUMMARY_SQL = """
SELECT target, collection_mode, is_llm_processed,
       (llm_analysis->>'excluded')::boolean AS excluded,
       llm_analysis->>'rating' AS rating,
       llm_analysis->>'tone'   AS tone,
       publish_date
FROM articles WHERE id = ANY(:ids)
"""


def select_sample(since: str, per_target: int, seed: str) -> list[int]:
    session = SessionLocal()
    try:
        ids = []
        for target in COMMODITIES:
            params = {"target": target, "since": since, "seed": seed}
            ids += [r[0] for r in session.execute(text(SAMPLE_SQL), {**params, "mode": "analyze", "limit": 100000})]
            ids += [r[0] for r in session.execute(text(SAMPLE_SQL), {**params, "mode": "monitor", "limit": per_target})]
        return ids
    finally:
        session.close()


def summarize(ids: list[int]) -> dict:
    session = SessionLocal()
    try:
        rows = [dict(r) for r in session.execute(text(SUMMARY_SQL), {"ids": ids}).mappings().all()]
    finally:
        session.close()

    def stats(items: list[dict]) -> dict:
        done = [r for r in items if r["is_llm_processed"]]
        kept = [r for r in done if not r["excluded"]]
        return {
            "n": len(items),
            "processed": len(done),
            "excluded_pct": round(100 * (len(done) - len(kept)) / len(done), 1) if done else None,
            "rating": dict(sorted(Counter(r["rating"] for r in kept).items())),
            "rating3_pct_of_kept": round(100 * sum(1 for r in kept if r["rating"] == "3") / len(kept), 1) if kept else None,
            "tone": dict(Counter(r["tone"] for r in kept).most_common()),
        }

    dates = [r["publish_date"] for r in rows]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"from": min(dates).isoformat(), "to": max(dates).isoformat()} if dates else None,
        "overall": stats(rows),
        "by_mode": {m: stats([r for r in rows if r["collection_mode"] == m]) for m in ("analyze", "monitor")},
        "by_target": {t: stats([r for r in rows if r["target"] == t]) for t in COMMODITIES},
        "by_target_mode": {
            f"{t}|{m}": stats([r for r in rows if r["target"] == t and r["collection_mode"] == m])
            for t in COMMODITIES for m in ("analyze", "monitor")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="コモディティ記事の標本処理と集計")
    parser.add_argument("--from", dest="since", default="2026-09-01", help="対象とする記事の観測日の下限")
    parser.add_argument("--per-target", type=int, default=50, help="広いクエリ(monitor)から品目ごとに抽出する件数")
    parser.add_argument("--seed", default="2026-10-06", help="標本を決める文字列")
    parser.add_argument("--ids-file", help="既存の標本ファイル（指定時は標本を選び直さない）")
    parser.add_argument("--summary-only", action="store_true", help="LLM処理をせず集計だけ行う")
    parser.add_argument("--out", default="eval_results/commodity", help="出力先ディレクトリの親")
    args = parser.parse_args()

    if args.ids_file:
        with open(args.ids_file, encoding="utf-8") as f:
            ids = json.load(f)["ids"]
    else:
        ids = select_sample(args.since, args.per_target, args.seed)
    print(f"標本 {len(ids)} 件", flush=True)

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "sample_ids.json"), "w", encoding="utf-8") as f:
        json.dump({"since": args.since, "per_target": args.per_target, "seed": args.seed, "ids": ids}, f)

    if not args.summary_only:
        backend = _get_llm_backend()
        if not backend:
            raise SystemExit("LLMバックエンドが利用できません")
        run_all(backend, None, ids)

    summary = summarize(ids)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
