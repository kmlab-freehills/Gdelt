"""
需要シグナルの判定を、別のLLM（Gemini）に採点させる（DBは読み取りのみ）

人手ラベルと同じ項目（①関係するか・②方向・⑥★、③〜⑤因果の正しさ）をGeminiに付けさせる。
人がラベル付けした時と同じく2段階で聞く:
  1回目: 記事だけを見せて①②⑥（評価対象のLLMの判定は見せない）
  2回目: 評価対象のLLMが書いた因果を見せて③〜⑤（1回目で関係ありとした記事のみ）
本文は評価対象のLLMと同じもの（DBに保存された、取得時に切り詰めた本文）を渡す。

このファイルを直接実行すると、人手ラベル済みの標本で採点役の一致率を測る:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor sh -c \
    "pip install -q openpyxl && python judge_demand.py --dir eval_results/commodity_labels/20261007_133137"
"""

import argparse
import json
import os
import time
from datetime import datetime

from sqlalchemy import text

from database import SessionLocal
from llm_processor import TARGETS, _GeminiBackend, _parse_json
from score_labels import cell, read_rows, wilson

JUDGE_INTERVAL_SECONDS = float(os.getenv("JUDGE_INTERVAL_SECONDS", "7"))  # 無料枠の毎分の上限に収める
CHECK_CHOICES = ("正しい", "誤り", "記事に記載なし")

ARTICLES_SQL = """
SELECT id, target, title, source_domain, publish_date, body, llm_analysis
FROM articles WHERE id = ANY(:ids)
"""


def _article_block(a: dict) -> str:
    body = (a["body"] or "").strip() or "(本文取得不可 - 見出しのみで判断)"
    return (f"- 見出し: {a['title'] or ''}\n- 媒体: {a['source_domain'] or ''}\n"
            f"- 観測日: {a['publish_date'].strftime('%Y-%m-%d')}\n- 本文: {body}")


def relevance_prompt(a: dict) -> str:
    label = TARGETS.get(a["target"], {}).get("label", a["target"])
    return f"""次のニュース記事を、【品目：{label}】の需要シグナルとして評価してください。判断の根拠になる文を、記事から原文のまま引用してください。記事に書かれていないことを推測で補わないでください。

記事:
{_article_block(a)}

①relevant（需要に関わる事実がある記事か）: "はい" / "いいえ"
  はい＝この品目の需要（または需給バランス）に影響する具体的な事実を報じている（新規受注、増産・減産、政策、輸出規制、消費統計など）
  いいえ＝価格の動きだけの市況、テクニカル分析、投資の勧め、一般論、この品目と関係ない話題（別の品目の話も含む）
②direction（①がはいの時）: "強気" / "弱気" / "中立"
  強気＝需要が増える、または供給が締まる・途絶える材料／弱気＝需要が減る、または供給がだぶつく材料／中立＝どちらとも言えない・混在
③rating（①がはいの時）: "★1" / "★2" / "★3"
  ★3＝記事自身が予想・コンセンサス・過去の記録と比べて明示している（「予想以上」「過去最高」「○年ぶり」など）、または突発的な供給・需要ショック（輸出禁止、工場閉鎖、新たな義務化など）をその規模とともに報じている。比較の記述がなければ★3にしない
  ★2＝需要に影響しうる新しい具体的事実（予想との比較はない）
  ★1＝既知の傾向の再確認、具体的な新事実のない論評

次のJSONだけを返してください（①がいいえの時、direction と rating は null）:
{{"relevant": "はい", "direction": "強気", "rating": "★2", "quote": "<根拠の原文の引用>", "note": "<迷う点があれば1文、なければ null>"}}"""


def causal_prompt(a: dict, causal: dict) -> str:
    label = TARGETS.get(a["target"], {}).get("label", a["target"])
    show = lambda v: v if v else "（空欄）"  # noqa: E731
    return f"""次のニュース記事について、別のシステムが【品目：{label}】の需給に関する因果を書きました。それぞれ記事の内容と合っているか判定し、根拠を原文のまま引用してください。

記事:
{_article_block(a)}

システムが書いた因果:
- きっかけ: {show(causal.get('trigger'))}
- 仕組み: {show(causal.get('mechanism'))}
- 影響: {show(causal.get('effect'))}

判定の基準:
  正しい＝記事に書かれている内容と合っている
  誤り＝記事の内容と違う、記事にない内容を足している、またはシステムが空欄なのに記事には書かれている
  記事に記載なし＝システムが空欄で、記事にも書かれていない

次のJSONだけを返してください:
{{"trigger": "正しい", "mechanism": "誤り", "effect": "正しい", "quote": "<根拠の原文の引用>", "note": "<誤りの理由を1文、なければ null>"}}"""


def _ask(backend, prompt: str) -> dict:
    for attempt in range(2):
        try:
            result = _parse_json(backend.call(prompt))
            if result:
                return result
        except Exception as e:
            print(f"採点役の呼び出し失敗: {e}", flush=True)
            time.sleep(30)
    return {}


def judge_article(backend, a: dict, causal: dict | None) -> dict:
    """1記事を採点する。causal は評価対象のLLMが書いた因果（除外された記事なら None）。"""
    first = _ask(backend, relevance_prompt(a))
    time.sleep(JUDGE_INTERVAL_SECONDS)
    out = {"relevant": first.get("relevant"), "direction": first.get("direction"), "rating": first.get("rating"),
           "quote": first.get("quote"), "note": first.get("note")}
    if causal is not None and out["relevant"] == "はい":
        second = _ask(backend, causal_prompt(a, causal))
        time.sleep(JUDGE_INTERVAL_SECONDS)
        out["causal_check"] = {k: second.get(k) for k in ("trigger", "mechanism", "effect")}
        out["causal_quote"] = second.get("quote")
        out["causal_note"] = second.get("note")
    return out


def load_articles(ids: list[int]) -> dict[int, dict]:
    session = SessionLocal()
    try:
        return {r["id"]: dict(r) for r in session.execute(text(ARTICLES_SQL), {"ids": ids}).mappings().all()}
    finally:
        session.close()


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohenのκ（偶然の一致を差し引いた一致度）。"""
    n = len(pairs)
    if n == 0:
        return None
    labels = {x for p in pairs for x in p}
    po = sum(1 for a, b in pairs if a == b) / n
    pe = sum((sum(1 for a, _ in pairs if a == k) / n) * (sum(1 for _, b in pairs if b == k) / n) for k in labels)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else None


def agreement(rows: list[dict], judged: dict[int, dict]) -> dict:
    """人手ラベル（シートの行）と採点役の一致率。"""
    rel = [(cell(r, "①需要に関わる事実がある記事か"), judged[r["_id"]].get("relevant")) for r in rows]
    both = [r for r in rows if cell(r, "①需要に関わる事実がある記事か") == "はい" and judged[r["_id"]].get("relevant") == "はい"]
    direction = [(cell(r, "②正しい方向"), judged[r["_id"]].get("direction")) for r in both if cell(r, "②正しい方向")]
    rating = [(cell(r, "⑥あなたが付ける★"), judged[r["_id"]].get("rating")) for r in both if cell(r, "⑥あなたが付ける★")]
    result = {
        "relevant": {**wilson(sum(1 for a, b in rel if a == b), len(rel)), "kappa": kappa(rel)},
        "relevant_disagree": [(r["_id"], cell(r, "①需要に関わる事実がある記事か"), judged[r["_id"]].get("relevant"))
                              for r in rows if cell(r, "①需要に関わる事実がある記事か") != judged[r["_id"]].get("relevant")],
        "direction": wilson(sum(1 for a, b in direction if a == b), len(direction)),
        "rating": wilson(sum(1 for a, b in rating if a == b), len(rating)),
    }
    for col, key in (("③きっかけ", "trigger"), ("④仕組み", "mechanism"), ("⑤影響", "effect")):
        pairs = [(cell(r, col), (judged[r["_id"]].get("causal_check") or {}).get(key))
                 for r in both if cell(r, col) and judged[r["_id"]].get("causal_check")]
        result[col] = wilson(sum(1 for a, b in pairs if a == b), len(pairs))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="人手ラベル済みの標本で採点役（Gemini）の一致率を測る")
    parser.add_argument("--dir", required=True, help="make_commodity_label_sheet.py の出力フォルダ")
    parser.add_argument("--labels", default="commodity_labels_filled.xlsx", help="記入済みシートのファイル名")
    parser.add_argument("--out", default="eval_results/judge_eval", help="出力先ディレクトリの親")
    args = parser.parse_args()

    rows = [r for r in read_rows(os.path.join(args.dir, args.labels))
            if cell(r, "記事ID") and cell(r, "①需要に関わる事実がある記事か") in ("はい", "いいえ")]
    for r in rows:
        r["_id"] = int(float(cell(r, "記事ID")))
    articles = load_articles([r["_id"] for r in rows])

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    backend = _GeminiBackend()
    judged = {}
    with open(os.path.join(out_dir, "judge.jsonl"), "w", encoding="utf-8") as f:
        for n, r in enumerate(rows, 1):
            a = articles[r["_id"]]
            stored = a["llm_analysis"] or {}
            # 人が③〜⑤を判定したのは保存済み（標本を選んだ時）の因果なので、同じものを見せる
            causal = None if stored.get("excluded") else (stored.get("causal") or {})
            judged[r["_id"]] = judge_article(backend, a, causal)
            f.write(json.dumps({"id": r["_id"], "judge": judged[r["_id"]]}, ensure_ascii=False) + "\n")
            print(f"採点 {n}/{len(rows)}", flush=True)

    summary = {"judge_model": backend.model_name, "labels_dir": args.dir, "n": len(rows),
               "agreement": agreement(rows, judged)}
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
