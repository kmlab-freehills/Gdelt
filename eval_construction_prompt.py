"""
建設プロンプトの評価（人手ラベルを正解とする。DBは読み取りのみ）

人手でラベル付けしたシートの記事について、現在の建設プロンプトでLLMを実行し（--stored ならDBに
保存済みの結果を使い）、採用判定・状態・案件名・都市・日付を正解と照合する。
プロンプトを改訂するたびに同じ物差しで比べるためのスクリプト。

正解の作り方（シートの記入から）:
  - 建設案件か: ①（はい/いいえ）
  - 状態: ②
  - 案件名・都市・日付: ③〜⑤が「正しい」なら当時のLLMの値、「誤り」なら右隣の正しい値、
    「記事に記載なし」なら「なし」。誤りなのに正しい値が空の行は、その項目の採点から除く

標本は採用・除外から同数ずつ抽出しているため、適合率・再現率は母集団の件数で重み付けした値も出す。

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor_construction sh -c \
    "pip install -q openpyxl && python eval_construction_prompt.py --labels eval_results/labels/labels_filled_final.xlsx"
  # DBに保存済みの結果（ベースライン）で採点
  ... python eval_construction_prompt.py --labels ... --stored
"""

import argparse
import json
import os
import re
from datetime import date, datetime

from sqlalchemy import text

from database import SessionLocal
from llm_processor import (
    PROMPT_VERSIONS,
    TARGETS,
    _apply_subject_rule,
    _as_bool,
    _build_construction_info,
    _build_prompt,
    _null_if_blank,
    _OllamaBackend,
    _parse_json,
    _sanitize_dates,
)
from score_labels import STATUS_JA, cell, read_rows, wilson

DEFAULT_KEPT_POP = 1389
DEFAULT_EXCLUDED_POP = 1813

SQL = """
SELECT id, target, title, source_domain, publish_date, body, event_date, llm_analysis
FROM articles WHERE id = ANY(:ids)
"""


def _norm(value) -> str:
    """照合用に小文字化し、先頭の冠詞と記号・空白を除く。"""
    if not value:
        return ""
    text_ = re.sub(r"^\s*(a|an|the)\s+", "", str(value).lower())
    return re.sub(r"\W+", "", text_)


def _within_one_day(gold: str, pred: str | None) -> bool:
    if not pred:
        return False
    return abs((date.fromisoformat(gold) - date.fromisoformat(pred)).days) <= 1


def _same_text(a, b) -> bool:
    """表記ゆれを許して一致とみなす（片方がもう片方を含めば一致）。都市名の照合に使う。"""
    a, b = _norm(a), _norm(b)
    return bool(a) and bool(b) and (a in b or b in a)


def _contains_gold(gold, pred) -> bool:
    """予測が正解の名前を含んでいれば一致とみなす（予測が短すぎる「Dakhla」等は不一致）。案件名の照合に使う。"""
    gold, pred = _norm(gold), _norm(pred)
    return bool(gold) and bool(pred) and gold in pred


def _parse_date(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", str(value))
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def build_gold(row: dict) -> dict | None:
    relevant = cell(row, "①建設案件の記事か")
    if relevant not in ("はい", "いいえ"):
        return None

    def field(mark_col: str, llm_col: str, fix_col: str, is_date: bool = False):
        mark = cell(row, mark_col)
        if mark == "正しい":
            value = cell(row, llm_col)
            return ("value", _parse_date(value) if is_date else value)
        if mark == "誤り":
            fix = row.get(fix_col)
            fix = _parse_date(fix) if is_date else cell(row, fix_col)
            return ("value", fix) if fix else None  # 正しい値が未記入なら採点しない
        if mark == "記事に記載なし":
            return ("none", None)
        return None

    return {
        "id": int(float(cell(row, "記事ID"))),
        "stratum": cell(row, "LLMの判定"),  # 標本抽出時のLLMの判定（重み付けに使う）
        "relevant": relevant == "はい",
        "status": cell(row, "②正しい状態") or None,
        "name": field("③案件名", "LLM：案件名", "正しい案件名（誤りの場合）"),
        "city": field("④都市", "LLM：都市", "正しい都市（誤りの場合）"),
        "date": field("⑤日付", "LLM：日付", "正しい日付（誤りの場合）", is_date=True),
        "title": cell(row, "見出し（原文）"),
    }


def predict_from_result(result: dict, article: dict) -> dict:
    """LLMの生出力を、本番と同じ後処理を通して評価用の形にする。"""
    result = _apply_subject_rule(result)
    excluded = _as_bool(result.get("excluded"))
    event_date, _, basis, milestones = _sanitize_dates(result, article["publish_date"], True)
    info = _build_construction_info(result, milestones, f"{article['title'] or ''}\n{article['body'] or ''}")
    rating = result.get("rating")
    try:
        rating = int(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating = None
    return {
        "excluded": excluded,
        "rating": None if excluded else rating,
        "status": STATUS_JA.get(info["status"]) if not excluded else None,
        "name": info["building_name"],
        "city": (info["location"] or {}).get("city"),
        "event_date": event_date.isoformat() if event_date else None,
        "basis": basis,
        "subject": result.get("main_subject"),
        "reason": result.get("reason"),
    }


def predict_from_stored(article: dict) -> dict:
    a = article["llm_analysis"] or {}
    c = a.get("construction") or {}
    excluded = bool(a.get("excluded"))
    rating = a.get("rating")
    return {
        "excluded": excluded,
        "rating": None if excluded else (int(rating) if rating is not None else None),
        "status": STATUS_JA.get(c.get("status")) if not excluded else None,
        "name": _null_if_blank(c.get("building_name")),
        "city": _null_if_blank((c.get("location") or {}).get("city")),
        "event_date": article["event_date"].isoformat() if article["event_date"] else None,
        "basis": a.get("event_date_basis"),
        "subject": c.get("main_subject"),
        "reason": a.get("reason"),
    }


def score(golds: list[dict], preds: dict[int, dict], kept_pop: int, excluded_pop: int, min_rating: int = 1) -> dict:
    n_kept_stratum = sum(1 for g in golds if g["stratum"] == "採用")
    n_excl_stratum = sum(1 for g in golds if g["stratum"] == "除外")
    weight = {"採用": kept_pop / n_kept_stratum, "除外": excluded_pop / n_excl_stratum}

    def is_kept(p):
        return not p["excluded"] and (p["rating"] or 0) >= min_rating

    tp = fp = fn = tn = 0
    wtp = wfp = wfn = 0.0
    fp_list, fn_list = [], []
    for g in golds:
        p = preds[g["id"]]
        w = weight[g["stratum"]]
        if is_kept(p) and g["relevant"]:
            tp += 1
            wtp += w
        elif is_kept(p):
            fp += 1
            wfp += w
            fp_list.append({"id": g["id"], "subject": p["subject"], "rating": p["rating"], "title": g["title"][:70]})
        elif g["relevant"]:
            fn += 1
            wfn += w
            fn_list.append({"id": g["id"], "subject": p["subject"], "title": g["title"][:70], "reason": (p["reason"] or "")[:80]})
        else:
            tn += 1

    hits = [g for g in golds if g["relevant"] and is_kept(preds[g["id"]])]

    def field_acc(key: str, compare) -> tuple[dict, list]:
        ok, n, errors = 0, 0, []
        for g in hits:
            gold = g[key]
            if gold is None:
                continue
            n += 1
            good = compare(gold, preds[g["id"]])
            ok += good
            if not good:
                errors.append({"id": g["id"], "gold": gold[1], "pred": preds[g["id"]].get(key if key != "date" else "event_date")})
        return wilson(ok, n), errors

    status_rows = [g for g in hits if g["status"]]
    status_errors = [{"id": g["id"], "gold": g["status"], "pred": preds[g["id"]]["status"]}
                     for g in status_rows if preds[g["id"]]["status"] != g["status"]]
    name_acc, name_err = field_acc("name", lambda gold, p: (gold[0] == "none" and not p["name"]) or (gold[0] == "value" and _contains_gold(gold[1], p["name"])))
    city_acc, city_err = field_acc("city", lambda gold, p: (gold[0] == "none" and not p["city"]) or (gold[0] == "value" and _same_text(gold[1], p["city"])))
    date_acc, date_err = field_acc("date", lambda gold, p: (gold[0] == "none" and (not p["event_date"] or p["basis"] == "publication"))
                                   or (gold[0] == "value" and gold[1] == p["event_date"]))
    # GDELTの観測日(UTC)と記事の現地日付のずれを許した指標（先行性の分析では1日の差はほぼ影響しない）
    date_acc_1d, _ = field_acc("date", lambda gold, p: (gold[0] == "none" and (not p["event_date"] or p["basis"] == "publication"))
                               or (gold[0] == "value" and _within_one_day(gold[1], p["event_date"])))

    return {
        "min_rating": min_rating,
        "confusion": {"TP": tp, "FP": fp, "FN": fn, "TN": tn},
        "precision_raw": wilson(tp, tp + fp),
        "precision_weighted_pct": round(100 * wtp / (wtp + wfp), 1) if wtp + wfp else None,
        "recall_weighted_pct": round(100 * wtp / (wtp + wfn), 1) if wtp + wfn else None,
        "status": wilson(len(status_rows) - len(status_errors), len(status_rows)),
        "name": name_acc,
        "city": city_acc,
        "date": date_acc,
        "date_within_1day": date_acc_1d,
        "errors": {"FP": fp_list, "FN": fn_list, "status": status_errors, "name": name_err, "city": city_err, "date": date_err},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="建設プロンプトの評価（人手ラベルを正解とする）")
    parser.add_argument("--labels", required=True, help="記入済みのラベルシート（xlsx/CSV）")
    parser.add_argument("--stored", action="store_true", help="LLMを実行せず、DBに保存済みの結果で採点する")
    parser.add_argument("--raw", help="LLMを実行せず、以前の評価の raw.jsonl（LLMの生出力）に今の後処理を適用して採点する")
    parser.add_argument("--kept-pop", type=int, default=DEFAULT_KEPT_POP)
    parser.add_argument("--excluded-pop", type=int, default=DEFAULT_EXCLUDED_POP)
    parser.add_argument("--out", default="eval_results/prompt_eval", help="出力先ディレクトリの親")
    args = parser.parse_args()

    golds = [g for g in (build_gold(r) for r in read_rows(args.labels)) if g]
    session = SessionLocal()
    try:
        articles = {r["id"]: dict(r) for r in session.execute(text(SQL), {"ids": [g["id"] for g in golds]}).mappings().all()}
    finally:
        session.close()

    suffix = "_stored" if args.stored else ("_rescored" if args.raw else "")
    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S") + suffix)
    os.makedirs(out_dir, exist_ok=True)

    preds = {}
    if args.stored:
        label = "stored"
        preds = {g["id"]: predict_from_stored(articles[g["id"]]) for g in golds}
    elif args.raw:
        label = f"rescored:{args.raw}"
        with open(args.raw, encoding="utf-8") as f:
            raw = {r["id"]: r["result"] for r in map(json.loads, f)}
        preds = {g["id"]: predict_from_result(raw[g["id"]], articles[g["id"]]) for g in golds}
    else:
        label = PROMPT_VERSIONS["construction"]
        backend = _OllamaBackend()
        labels = {k: v["label"] for k, v in TARGETS.items()}
        with open(os.path.join(out_dir, "raw.jsonl"), "w", encoding="utf-8") as f:
            for i, g in enumerate(golds, 1):
                a = articles[g["id"]]
                prompt = _build_prompt(labels[a["target"]], a["title"] or "", a["source_domain"] or "",
                                       a["publish_date"].strftime("%Y-%m-%d"), a["body"] or "", True)
                result = _parse_json(backend.call(prompt)) or {"excluded": True}
                preds[g["id"]] = predict_from_result(result, a)
                f.write(json.dumps({"id": g["id"], "result": result}, ensure_ascii=False) + "\n")
                if i % 20 == 0:
                    print(f"{i}/{len(golds)}", flush=True)

    summary = {
        "label": label,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n": len(golds),
        "all_kept": score(golds, preds, args.kept_pop, args.excluded_pop, min_rating=1),
        "kept_rating2plus": score(golds, preds, args.kept_pop, args.excluded_pop, min_rating=2),
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "preds.json"), "w", encoding="utf-8") as f:
        json.dump(preds, f, ensure_ascii=False, indent=2)

    for key in ("all_kept", "kept_rating2plus"):
        s = summary[key]
        print(f"[{label} / {key}] confusion={s['confusion']} precision={s['precision_raw']['pct']}% "
              f"(weighted {s['precision_weighted_pct']}%) recall_w={s['recall_weighted_pct']}% "
              f"status={s['status']['pct']}% name={s['name']['pct']}% city={s['city']['pct']}% date={s['date']['pct']}% "
              f"(±1day {s['date_within_1day']['pct']}%)")
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
