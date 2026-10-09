"""
人手ラベルの採点（DBは読み取りのみ）

make_label_sheet.py で作ったシート（Googleスプレッドシート等で記入し、xlsxかCSVで保存したもの）を読み、
LLMの判断の正解率を計算する。入力欄が空の行は集計から除く（途中まででも採点できる）。

標本は「採用」「除外」から同数ずつ抽出しているため、全体の正解率と再現率は
母集団の件数（--kept-pop / --excluded-pop）で重み付けして推定する。
比率の95%信頼区間はWilson法で計算する。

実行例（xlsxを読む場合は openpyxl が必要）:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor_construction sh -c \
    "pip install -q openpyxl && python score_labels.py --file eval_results/labels/labels_filled.xlsx"
"""

import argparse
import csv
import json
import math
import os
from datetime import datetime

from sqlalchemy import text

from database import SessionLocal

# 標本抽出時点（2026-10-06 17:36）の最新プロンプト世代の件数
DEFAULT_KEPT_POP = 1389
DEFAULT_EXCLUDED_POP = 1813

STATUS_JA = {
    "planned": "計画", "groundbreaking": "着工", "under_construction": "施工中",
    "topped_out": "上棟", "completed": "竣工", "halted": "中断",
    "resumed": "再開", "cancelled": "中止", "unknown": "不明",
}
SOURCE_JA = {
    "original_reporting": "一次報道", "syndicated": "転載", "opinion": "論説",
    "aggregation": "まとめ", "press_release": "PR", "other": "その他",
}


def wilson(k: int, n: int, z: float = 1.96) -> dict:
    """比率 k/n とWilsonの95%信頼区間（%）。"""
    if n == 0:
        return {"k": 0, "n": 0, "pct": None, "ci95": None}
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {"k": k, "n": n, "pct": round(100 * p, 1),
            "ci95": [round(100 * max(0.0, center - half), 1), round(100 * min(1.0, center + half), 1)]}


def read_rows(path: str) -> list[dict]:
    if path.lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb["ラベル付け"] if "ラベル付け" in wb.sheetnames else wb.worksheets[-1]
        values = list(ws.iter_rows(values_only=True))
        header = [str(h).strip() if h is not None else "" for h in values[0]]
        return [dict(zip(header, row)) for row in values[1:] if any(v not in (None, "") for v in row)]
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def cell(row: dict, key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def load_llm_meta(ids: list[int]) -> dict[int, dict]:
    """シートに載っていないLLMの出力（ソースの種類、日付の根拠）をDBから取る。"""
    session = SessionLocal()
    try:
        rows = session.execute(
            text("SELECT id, llm_analysis->'source_assessment'->>'source_type' AS source_type, "
                 "llm_analysis->>'event_date_basis' AS basis FROM articles WHERE id = ANY(:ids)"),
            {"ids": ids},
        ).mappings().all()
    finally:
        session.close()
    return {r["id"]: dict(r) for r in rows}


def article_id(row: dict) -> int:
    return int(float(cell(row, "記事ID")))


def date_check(rows: list[dict], meta: dict[int, dict]) -> dict:
    """日付は、LLMが付けた根拠ごとに分けて採点する。
      - 明記・相対表現から計算（explicit/relative）: 「正しい」なら正解
      - 公開日で代用（publication）: 記事に日付が無い（「記事に記載なし」）か「正しい」なら妥当な代用。
        「誤り」なら、記事に書かれていた日付を取りこぼした
      - 日付なし（null）: 「記事に記載なし」なら正解
    """
    stated, fallback, none_ = [], [], []
    for r in rows:
        mark = cell(r, "⑤日付")
        if not mark:
            continue
        basis = (meta.get(article_id(r)) or {}).get("basis")
        if not cell(r, "LLM：日付"):
            none_.append(mark == "記事に記載なし")
        elif basis == "publication":
            fallback.append(mark in ("記事に記載なし", "正しい"))
        else:
            stated.append(mark == "正しい")
    all_ = stated + fallback + none_
    return {
        "明記された日付の正答率": wilson(sum(stated), len(stated)),
        "公開日での代用が妥当だった割合": wilson(sum(fallback), len(fallback)),
        "日付なしが正しかった割合": wilson(sum(none_), len(none_)),
        "日付の扱いが妥当だった割合(全体)": wilson(sum(all_), len(all_)),
    }


def field_check(rows: list[dict], check_col: str, llm_col: str) -> tuple[dict, list]:
    """③〜⑤の採点。正しい=正解、記事に記載なし=LLMも空なら正解（値を作っていたら誤り）、誤り=誤り。"""
    correct, total, errors = 0, 0, []
    for r in rows:
        mark = cell(r, check_col)
        if not mark:
            continue
        total += 1
        llm_value = cell(r, llm_col)
        ok = mark == "正しい" or (mark == "記事に記載なし" and not llm_value)
        correct += ok
        if not ok:
            errors.append({"記事ID": cell(r, "記事ID"), "LLM": llm_value, "判定": mark})
    return wilson(correct, total), errors


def main() -> None:
    parser = argparse.ArgumentParser(description="人手ラベルの採点")
    parser.add_argument("--file", required=True, help="記入済みのシート（xlsx または CSV）")
    parser.add_argument("--kept-pop", type=int, default=DEFAULT_KEPT_POP, help="母集団の採用件数")
    parser.add_argument("--excluded-pop", type=int, default=DEFAULT_EXCLUDED_POP, help="母集団の除外件数")
    parser.add_argument("--out", default="eval_results/scores", help="出力先ディレクトリの親")
    args = parser.parse_args()

    rows = [r for r in read_rows(args.file) if cell(r, "①建設案件の記事か") in ("はい", "いいえ")]
    llm_kept = [r for r in rows if cell(r, "LLMの判定") == "採用"]
    llm_excl = [r for r in rows if cell(r, "LLMの判定") == "除外"]

    # 1. 採用・除外の判定
    precision = wilson(sum(1 for r in llm_kept if cell(r, "①建設案件の記事か") == "はい"), len(llm_kept))
    npv = wilson(sum(1 for r in llm_excl if cell(r, "①建設案件の記事か") == "いいえ"), len(llm_excl))
    estimate = {}
    if precision["n"] and npv["n"]:
        p, q = precision["k"] / precision["n"], npv["k"] / npv["n"]
        true_pos = args.kept_pop * p
        false_neg = args.excluded_pop * (1 - q)
        estimate = {
            "accuracy_pct": round(100 * (args.kept_pop * p + args.excluded_pop * q) / (args.kept_pop + args.excluded_pop), 1),
            "recall_pct": round(100 * true_pos / (true_pos + false_neg), 1) if true_pos + false_neg else None,
            "note": "母集団の件数で重み付けした推定値（点推定）",
        }

    # 2. 採用かつ建設案件の記事について、各項目の正答率
    true_kept = [r for r in llm_kept if cell(r, "①建設案件の記事か") == "はい"]
    status_rows = [r for r in true_kept if cell(r, "②正しい状態")]
    status = wilson(sum(1 for r in status_rows if cell(r, "LLM：状態") == cell(r, "②正しい状態")), len(status_rows))
    status_errors = [{"記事ID": cell(r, "記事ID"), "LLM": cell(r, "LLM：状態"), "正解": cell(r, "②正しい状態")}
                     for r in status_rows if cell(r, "LLM：状態") != cell(r, "②正しい状態")]
    name, name_errors = field_check(true_kept, "③案件名", "LLM：案件名")
    city, city_errors = field_check(true_kept, "④都市", "LLM：都市")
    meta = load_llm_meta([article_id(r) for r in llm_kept]) if llm_kept else {}
    dates = date_check(true_kept, meta)
    date_errors = [{"記事ID": cell(r, "記事ID"), "LLM": cell(r, "LLM：日付"),
                    "根拠": (meta.get(article_id(r)) or {}).get("basis"), "判定": cell(r, "⑤日付")}
                   for r in true_kept if cell(r, "⑤日付") == "誤り"]

    # 3. ソースの種類（LLMの判定は採用記事にのみ存在）
    source_rows = [r for r in llm_kept if cell(r, "⑥ソースの種類")]

    def source_match(r: dict) -> bool:
        return SOURCE_JA.get((meta.get(article_id(r)) or {}).get("source_type")) == cell(r, "⑥ソースの種類")

    unsure = [r for r in source_rows if "迷" in cell(r, "メモ")]
    sure = [r for r in source_rows if r not in unsure]
    source_all = wilson(sum(1 for r in source_rows if source_match(r)), len(source_rows))
    source_sure = wilson(sum(1 for r in sure if source_match(r)), len(sure))
    source_errors = [{"記事ID": cell(r, "記事ID"),
                      "LLM": SOURCE_JA.get((meta.get(article_id(r)) or {}).get("source_type")),
                      "正解": cell(r, "⑥ソースの種類")} for r in source_rows if not source_match(r)]

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "file": args.file,
        "labeled": {"採用": len(llm_kept), "除外": len(llm_excl)},
        "population": {"採用": args.kept_pop, "除外": args.excluded_pop},
        "1_採用判定の適合率(採用のうち本当に建設案件)": precision,
        "1_除外判定の正しさ(除外のうち本当に無関係)": npv,
        "1_全体の推定": estimate,
        "2_状態の正答率": status,
        "2_案件名の正答率": name,
        "2_都市の正答率": city,
        "2_日付": dates,
        "3_ソース種類の一致率": source_all,
        "3_ソース種類の一致率(迷った記事を除く)": source_sure,
        "errors": {"状態": status_errors, "案件名": name_errors, "都市": city_errors,
                   "日付": date_errors, "ソース種類": source_errors},
        "unsure_source_rows": len(unsure),
    }

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
