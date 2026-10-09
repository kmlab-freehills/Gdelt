"""
需要プロンプトの改善を、ラベル済みの記事で確かめる（DBは読み取りのみ）

make_commodity_label_sheet.py の標本（LLMの判定で層別した無作為標本）に、現在の需要プロンプトを
その場で通し（DBには書かない）、人手ラベルと比べる。標本を選んだ時の判定（保存済みの結果）の層の
母数で各記事を重み付けするので、プロンプトを変えた後の判定でも母集団での精度・再現率を推定できる。
因果（③〜⑤）は保存済みの結果について付けたラベルなので採点しない（中身は raw.jsonl で確かめる）。

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor sh -c \
    "pip install -q openpyxl && python eval_demand_prompt.py --dir eval_results/commodity_labels/20261007_133137"
"""

import argparse
import json
import os
from datetime import datetime

from sqlalchemy import text

from database import SessionLocal
from llm_processor import (PROMPT_VERSIONS, TARGETS, _apply_demand_rules, _as_bool, _build_prompt, _OllamaBackend,
                           _parse_json, _verify_demand_evidence)
from make_commodity_label_sheet import TONE_JA
from score_labels import cell, read_rows, wilson

RELEVANCE_COL = "①需要に関わる事実がある記事か"

ARTICLES_SQL = """
SELECT id, target, title, source_domain, publish_date, body, llm_analysis
FROM articles WHERE id = ANY(:ids)
"""


def predict(result: dict, article: dict) -> dict:
    """本番（run_all）と同じ後処理を通した判定。result は書き換わる。"""
    _apply_demand_rules(result)
    rating, evidence = _verify_demand_evidence(result, f"{article['title'] or ''}\n{article['body'] or ''}")
    excluded = _as_bool(result.get("excluded"))
    return {"excluded": excluded, "rating": None if excluded else rating, "tone": result.get("tone"),
            "evidence": evidence, "by_rule": result.get("excluded_by_rule")}


def score(items: list[dict], key: str, weights: dict[int, float]) -> dict:
    """items の各要素: human_relevant, pred（予測）, id。key で使う予測を選ぶ。"""
    kept = [r for r in items if not r[key]["excluded"]]
    excluded = [r for r in items if r[key]["excluded"]]
    w = lambda rs: sum(weights[r["id"]] for r in rs)  # noqa: E731
    tp = [r for r in kept if r["human_relevant"]]
    fn = [r for r in excluded if r["human_relevant"]]
    rated = [r for r in tp if r["human_rating"]]
    toned = [r for r in tp if r["human_tone"]]
    return {
        "kept": len(kept),
        "precision": wilson(len(tp), len(kept)),
        "precision_weighted_pct": round(100 * w(tp) / w(kept), 1) if kept else None,
        "excluded_correct": wilson(len(excluded) - len(fn), len(excluded)),
        "recall_weighted_pct": round(100 * w(tp) / (w(tp) + w(fn)), 1) if tp or fn else None,
        "direction": wilson(sum(1 for r in toned if TONE_JA.get(r[key]["tone"]) == r["human_tone"]), len(toned)),
        "rating_match": wilson(sum(1 for r in rated if f"★{r[key]['rating']}" == r["human_rating"]), len(rated)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="需要プロンプトの改善をラベル済みの記事で確かめる")
    parser.add_argument("--dir", required=True, help="make_commodity_label_sheet.py の出力フォルダ")
    parser.add_argument("--labels", default="commodity_labels_filled.xlsx", help="記入済みシートのファイル名")
    parser.add_argument("--out", default="eval_results/demand_prompt_eval", help="出力先ディレクトリの親")
    args = parser.parse_args()

    with open(os.path.join(args.dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    rows = [r for r in read_rows(os.path.join(args.dir, args.labels))
            if cell(r, "記事ID") and cell(r, RELEVANCE_COL) in ("はい", "いいえ")]
    ids = [int(float(cell(r, "記事ID"))) for r in rows]
    # 重み = 標本を選んだ層の母数 / その層の標本数
    stratum_of = {i: name for name, members in meta["sample"].items() for i in members}
    weights = {i: meta["population"][stratum_of[i]] / len(meta["sample"][stratum_of[i]]) for i in ids}

    session = SessionLocal()
    try:
        articles = {r["id"]: dict(r) for r in session.execute(text(ARTICLES_SQL), {"ids": ids}).mappings().all()}
    finally:
        session.close()

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    backend = _OllamaBackend()
    labels = {k: v["label"] for k, v in TARGETS.items()}
    items = []
    with open(os.path.join(out_dir, "raw.jsonl"), "w", encoding="utf-8") as f:
        for n, (row, article_id) in enumerate(zip(rows, ids), 1):
            a = articles[article_id]
            prompt = _build_prompt(labels[a["target"]], a["title"] or "", a["source_domain"] or "",
                                   a["publish_date"].strftime("%Y-%m-%d"), a["body"] or "", False)
            result = _parse_json(backend.call(prompt)) or {"excluded": True}
            f.write(json.dumps({"id": article_id, "result": result}, ensure_ascii=False) + "\n")
            stored = a["llm_analysis"] or {}
            items.append({
                "id": article_id, "target": a["target"],
                "human_relevant": cell(row, RELEVANCE_COL) == "はい",
                "human_tone": cell(row, "②正しい方向"), "human_rating": cell(row, "⑥あなたが付ける★"),
                "stored": {"excluded": bool(stored.get("excluded")), "rating": stored.get("rating"),
                           "tone": stored.get("tone")},
                "new": predict(result, a),
                "new_causal": result.get("causal"), "new_inferred": result.get("causal_inferred"),
            })
            print(f"処理 {n}/{len(rows)}", flush=True)

    summary = {
        "prompt_version": PROMPT_VERSIONS["demand"],
        "labels_dir": args.dir,
        "n": len(items),
        "stored": score(items, "stored", weights),
        "new": score(items, "new", weights),
        "changed": [
            {"id": r["id"], "target": r["target"], "human": "はい" if r["human_relevant"] else "いいえ",
             "human_tone": r["human_tone"], "human_rating": r["human_rating"],
             "stored": f"{'除外' if r['stored']['excluded'] else '★' + str(r['stored']['rating'])} {r['stored']['tone']}",
             "new": f"{'除外' if r['new']['excluded'] else '★' + str(r['new']['rating'])} {r['new']['tone']}"}
            for r in items
            if (r["stored"]["excluded"], r["stored"]["rating"], r["stored"]["tone"])
            != (r["new"]["excluded"], r["new"]["rating"], r["new"]["tone"])
        ],
        "causal_filled_new": {k: sum(1 for r in items if not r["new"]["excluded"] and (r["new_causal"] or {}).get(k))
                              for k in ("trigger", "mechanism", "effect")},
        "inferred_filled_new": sum(1 for r in items if not r["new"]["excluded"]
                                   and any((r["new_inferred"] or {}).values())),
        "kept_new": sum(1 for r in items if not r["new"]["excluded"]),
        "excluded_by_rule_new": [r["id"] for r in items if r["new"]["by_rule"]],
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
