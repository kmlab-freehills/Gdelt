"""
コモディティの検証用の新しい標本を作る（DBは読み取りのみ）

改善に使ったラベル標本（--exclude-meta の記事）を除いた狭いクエリの記事を無作為な順に現在の需要プロンプトで
処理し（DBには書かない）、判定（除外／★2／★3）ごとに指定件数が集まるまで続ける。
採点役が評価対象のLLMの判定を見ずに採点できるよう、判定を含まない記事一覧を別ファイルに、順番を混ぜて出す。

出力（--out 配下の日時フォルダ）:
  - raw.jsonl         : LLMの生出力（採点が終わるまで見ない）
  - meta.json         : 母集団の件数、処理した記事に占める各判定の割合、標本の記事ID（層別）
  - judge_input.jsonl : 採点役に渡す記事（id・品目・見出し・URL・本文。判定は含まない）

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor sh -c \
    "pip install -q openpyxl && python make_commodity_test_set.py \
       --exclude-meta eval_results/commodity_labels/20261007_133137/meta.json"
"""

import argparse
import hashlib
import json
import os
import time
from datetime import datetime

from sqlalchemy import text

from database import SessionLocal
from eval_demand_prompt import predict
from llm_processor import PROMPT_VERSIONS, TARGETS, _build_prompt, _OllamaBackend, _parse_json
from make_commodity_label_sheet import COMMODITIES

POOL_SQL = """
SELECT id, target, title, url, source_domain, publish_date, body
FROM articles
WHERE collection_mode = 'analyze' AND is_llm_processed AND target = ANY(:targets) AND NOT (id = ANY(:exclude))
ORDER BY md5(id::text || :seed)
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="コモディティの検証用の新しい標本を作る")
    parser.add_argument("--exclude-meta", required=True, help="改善に使ったラベル標本の meta.json（その記事は除く）")
    parser.add_argument("--excluded", type=int, default=15, help="除外の層の件数")
    parser.add_argument("--rating2", type=int, default=15, help="★2の層の件数")
    parser.add_argument("--rating3", type=int, default=10, help="★3の層の件数")
    parser.add_argument("--seed", default="commodity-test-2026-10-07", help="標本を決める文字列")
    parser.add_argument("--max-process", type=int, default=400, help="処理する記事数の上限")
    parser.add_argument("--out", default="eval_results/commodity_test", help="出力先ディレクトリの親")
    parser.add_argument("--resume", help="途中で止まった出力フォルダ（その raw.jsonl の結果を使い、続きから処理する）")
    args = parser.parse_args()

    with open(args.exclude_meta, encoding="utf-8") as f:
        exclude = [i for ids in json.load(f)["sample"].values() for i in ids]
    session = SessionLocal()
    try:
        pool = [dict(r) for r in session.execute(text(POOL_SQL), {"targets": COMMODITIES, "exclude": exclude,
                                                                   "seed": args.seed}).mappings().all()]
    finally:
        session.close()
    print(f"母集団 {len(pool)} 件（改善に使った {len(exclude)} 件を除く）", flush=True)

    quotas = {"除外": args.excluded, "★2": args.rating2, "★3": args.rating3}
    cached = {}
    if args.resume:
        out_dir = args.resume
        with open(os.path.join(out_dir, "raw.jsonl"), encoding="utf-8") as f:
            cached = {d["id"]: d["result"] for d in map(json.loads, f)}
        print(f"再開: 処理済み {len(cached)} 件を使う", flush=True)
    else:
        out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(out_dir, exist_ok=True)
    backend = _OllamaBackend()
    labels = {k: v["label"] for k, v in TARGETS.items()}
    seen = {"除外": [], "★1": [], "★2": [], "★3": []}
    processed = 0
    with open(os.path.join(out_dir, "raw.jsonl"), "a" if args.resume else "w", encoding="utf-8") as f:
        for a in pool:
            if all(len(seen[k]) >= q for k, q in quotas.items()) or processed >= args.max_process:
                break
            if a["id"] in cached:
                result = cached[a["id"]]
            else:
                prompt = _build_prompt(labels[a["target"]], a["title"] or "", a["source_domain"] or "",
                                       a["publish_date"].strftime("%Y-%m-%d"), a["body"] or "", False)
                result = None
                for attempt in range(3):  # Ollama の一時的なエラー（500など）は待って再試行する
                    try:
                        result = _parse_json(backend.call(prompt)) or {"excluded": True}
                        break
                    except Exception as e:
                        print(f"LLM呼び出し失敗 id={a['id']}（{attempt + 1}回目）: {e}", flush=True)
                        time.sleep(30)
                if result is None:
                    continue
                f.write(json.dumps({"id": a["id"], "result": result}, ensure_ascii=False) + "\n")
                f.flush()
            processed += 1
            pred = predict(dict(result), a)
            stratum = "除外" if pred["excluded"] else f"★{pred['rating']}"
            seen.setdefault(stratum, []).append(a)
            if processed % 20 == 0:
                print(f"処理 {processed} 件（" + "、".join(f"{k} {len(v)}" for k, v in seen.items()) + "）", flush=True)

    sample = {k: [a["id"] for a in seen.get(k, [])[:q]] for k, q in quotas.items()}
    meta = {
        "prompt_version": PROMPT_VERSIONS["demand"],
        "seed": args.seed,
        "pool": len(pool),
        "processed": processed,
        "processed_by_stratum": {k: len(v) for k, v in seen.items()},
        # 母集団での各判定の件数の推定（処理した記事に占める割合 × 母集団）
        "population": {k: round(len(pool) * len(v) / processed) for k, v in seen.items()} if processed else {},
        "sample": sample,
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 採点役に渡す一覧。層が分からないよう、記事IDのハッシュ順に並べる
    by_id = {a["id"]: a for v in seen.values() for a in v}
    ids = sorted((i for v in sample.values() for i in v), key=lambda i: hashlib.md5(f"judge{i}".encode()).hexdigest())
    with open(os.path.join(out_dir, "judge_input.jsonl"), "w", encoding="utf-8") as f:
        for i in ids:
            a = by_id[i]
            f.write(json.dumps({"id": i, "target": a["target"], "label": labels[a["target"]], "title": a["title"],
                                "url": a["url"], "domain": a["source_domain"],
                                "date": a["publish_date"].strftime("%Y-%m-%d"), "body": a["body"] or ""},
                               ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in meta.items() if k != "sample"}, ensure_ascii=False))
    print(f"標本 {len(ids)} 件 → 出力先: {out_dir}")


if __name__ == "__main__":
    main()
