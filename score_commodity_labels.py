"""
コモディティの人手ラベルを採点する（DBは読み取りのみ）

make_commodity_label_sheet.py で作ったシートに記入したものを読み、LLMの判定と比べる。
層（除外／★2／★3）ごとの割合を出し、meta.json の母数で重み付けして全体を推定する。
「判定不可」など①が空欄の行は採点から除く。

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor sh -c \
    "pip install -q openpyxl && python score_commodity_labels.py --dir eval_results/commodity_labels/20261007_133137"
"""

import argparse
import json
import os
from collections import Counter

from score_labels import cell, read_rows, wilson

KEPT_STRATA = ("★2", "★3")


def main() -> None:
    parser = argparse.ArgumentParser(description="コモディティの人手ラベルを採点する")
    parser.add_argument("--dir", required=True, help="make_commodity_label_sheet.py の出力フォルダ")
    parser.add_argument("--labels", default="commodity_labels_filled.xlsx", help="記入済みシートのファイル名")
    args = parser.parse_args()

    with open(os.path.join(args.dir, "meta.json"), encoding="utf-8") as f:
        population = json.load(f)["population"]
    rows = [r for r in read_rows(os.path.join(args.dir, args.labels)) if cell(r, "記事ID")]
    labeled = [r for r in rows if cell(r, "①需要に関わる事実がある記事か") in ("はい", "いいえ")]
    relevant = lambda r: cell(r, "①需要に関わる事実がある記事か") == "はい"  # noqa: E731

    by_stratum = {}
    for name in ("除外",) + KEPT_STRATA:
        items = [r for r in labeled if cell(r, "LLMの判定") == name]
        k = sum(1 for r in items if (not relevant(r)) == (name == "除外"))
        by_stratum[name] = wilson(k, len(items))

    # 採用全体の精度・再現率は層の母数で重み付けする
    p2, p3, pe = (by_stratum[s]["pct"] / 100 for s in ("★2", "★3", "除外"))
    true_kept = population["★2"] * p2 + population["★3"] * p3
    missed = population["除外"] * (1 - pe)
    weighted = {
        "precision_kept_pct": round(100 * true_kept / (population["★2"] + population["★3"]), 1),
        "recall_pct": round(100 * true_kept / (true_kept + missed), 1),
    }

    kept_relevant = [r for r in labeled if cell(r, "LLMの判定") in KEPT_STRATA and relevant(r)]
    direction = wilson(sum(1 for r in kept_relevant if cell(r, "LLM：方向") == cell(r, "②正しい方向")),
                       sum(1 for r in kept_relevant if cell(r, "②正しい方向")))
    direction_errors = [(cell(r, "記事ID"), cell(r, "品目"), cell(r, "LLM：方向"), cell(r, "②正しい方向"))
                        for r in kept_relevant if cell(r, "②正しい方向") and cell(r, "LLM：方向") != cell(r, "②正しい方向")]

    # 因果は3項目とも記入された行だけで採点する。「記事に記載なし」はLLMも空欄だったので正解に数える
    causal_cols = ("③きっかけ", "④仕組み", "⑤影響")
    causal_rows = [r for r in kept_relevant if all(cell(r, c) for c in causal_cols)]
    ok = lambda r, c: cell(r, c) in ("正しい", "記事に記載なし")  # noqa: E731
    causal = {c: wilson(sum(1 for r in causal_rows if ok(r, c)), len(causal_rows)) for c in causal_cols}
    causal["3項目すべて"] = wilson(sum(1 for r in causal_rows if all(ok(r, c) for c in causal_cols)), len(causal_rows))

    rated = [r for r in kept_relevant if cell(r, "⑥あなたが付ける★")]
    rating_match = wilson(sum(1 for r in rated if cell(r, "⑥あなたが付ける★") == cell(r, "LLMの判定")), len(rated))
    rating_confusion = Counter(f"LLM{cell(r, 'LLMの判定')}→人{cell(r, '⑥あなたが付ける★')}" for r in rated)
    star3 = [r for r in labeled if cell(r, "LLMの判定") == "★3"]
    star3_confirmed = wilson(sum(1 for r in star3 if relevant(r) and cell(r, "⑥あなたが付ける★") == "★3"), len(star3))

    by_target = {}
    for r in labeled:
        if cell(r, "LLMの判定") in KEPT_STRATA:
            t = by_target.setdefault(cell(r, "品目"), [0, 0])
            t[0] += relevant(r)
            t[1] += 1

    result = {
        "population": population,
        "rows": len(rows),
        "labeled": len(labeled),
        "excluded_correct": by_stratum["除外"],
        "precision_rating2": by_stratum["★2"],
        "precision_rating3": by_stratum["★3"],
        "weighted": weighted,
        "direction": direction,
        "direction_errors": direction_errors,
        "causal": causal,
        "causal_rows": len(causal_rows),
        "rating_match": rating_match,
        "rating_confusion": dict(rating_confusion),
        "star3_confirmed_of_all_star3": star3_confirmed,
        "kept_relevant_by_target": {t: f"{a}/{n}" for t, (a, n) in by_target.items()},
    }
    out = os.path.join(args.dir, "score.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"出力先: {out}")


if __name__ == "__main__":
    main()
