"""
コモディティの需要シグナルの因果を集計する（DBは読み取りのみ）

採用された記事（狭いクエリ、指定した版）の因果（きっかけ・仕組み・影響）と方向（価格・需要・供給）を、
品目ごとに次の観点で集計する。
  1. 検証できる因果の記録: 根拠の引用が記事に実在し、きっかけ・仕組み・影響がそろっている記事
  2. 因果の型: きっかけの種類 → 影響の種類 の組み合わせの件数（キーワードで分類、複数該当あり）
  3. 筋が通っているかの確認: きっかけの種類（文章から分類）と、LLMが別の欄に付けた需要・供給の向きが
     経済的に整合する割合（例: 供給障害 → 供給が締まる）
  4. 週ごとの推移
  5. 代表例（検証できる記録から、根拠の引用つき）

出力（--out 配下の日時フォルダ）: summary.json、examples.csv、weekly.csv

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor python causal_summary.py --version demand-v4
"""

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import text

from database import SessionLocal

COMMODITIES = ["copper", "gold", "natural gas", "rare earth", "uranium"]
COMMODITY_JA = {"copper": "銅", "gold": "金", "natural gas": "天然ガス", "rare earth": "レアアース", "uranium": "ウラン"}

# きっかけの種類（きっかけの文から判定。複数該当あり）
TRIGGER_THEMES = {
    "地政学・紛争": r"戦争|紛争|攻撃|ミサイル|イラン|イスラエル|ホルムズ|紅海|フーシ|ホウティ|中東|ウクライナ|ロシア|封鎖|地政学|緊張|武装|"
                  r"\bwar\b|conflict|Iran|Hormuz|attack",
    "輸出規制・制裁・関税": r"輸出規制|輸出禁止|輸出制限|規制|制裁|関税|許可|ライセンス|export (ban|control|restriction)|sanction|tariff",
    "供給障害・事故": r"停止|事故|洪水|浸水|崩落|ストライキ|停電|故障|閉鎖|中断|減産|操業|halt|suspend|outage|strike|flood|shut",
    "増産・新規供給": r"増産|生産(量)?(の)?(拡大|増加|増強)|生産能力(の)?(増|拡)|新(規|た)な?(鉱山|生産|供給|プロジェクト|施設|ターミナル)|"
                      r"稼働|操業(開始|を開始)|投資決定|開発(の)?承認|production (increase|expansion)|new mine",
    "投資・開発・買収": r"投資|プロジェクト|買収|合併|インフラ|パイプライン|掘削|探鉱|探査|開発|investment|acquisition",
    "技術・産業の需要": r"AI|データセンター|半導体|EV|電気自動車|電化|再生可能|再エネ|クリーン|低排出|送電|蓄電|原発|原子力|原子炉|"
                       r"防衛|data cent|electrif|reactor",
    "需要の動向（一般）": r"需要|消費|輸入(の)?(増|拡)|demand|consumption",
    "中央銀行・投資需要": r"中央銀行|中銀|外貨準備|ETF|投資家|安全資産|central bank",
    "金融・財政・マクロ": r"金利|利上げ|利下げ|FRB|Fed|ドル|インフレ|国債|利回り|景気|財政|債務|予算|rate|yield|inflation|dollar",
    "政策・政府": r"政府|政策|インセンティブ|法案|計画|戦略|推進|放出|自由化|government|policy",
    "サプライチェーン・依存": r"依存|サプライチェーン|供給網|supply chain",
    "天候・季節": r"冬|夏|寒波|猛暑|気温|エルニーニョ|干ばつ|天候|weather|winter",
    "在庫・備蓄": r"在庫|備蓄|貯蔵|stockpile|inventor|storage",
    "契約・調達": r"契約|調達|オフテイク|offtake|contract|procure",
    "価格の動き（説明的）": r"価格|相場|price",
}
# 影響の種類（仕組みと影響の文から判定）
EFFECT_THEMES = {
    "供給減・逼迫": r"供給.{0,10}(減|不足|逼迫|緊縮|制限|途絶|混乱|圧迫|低下)|不足|逼迫|shortage|tight",
    "供給増": r"供給.{0,10}(増|拡大|回復|過剰)|増産|surplus",
    "需要増": r"需要.{0,10}(増|拡大|高ま|急増|押し上げ|強)",
    "需要減": r"需要.{0,10}(減|低下|抑制|鈍化|落ち込|弱)",
    "価格上昇": r"(価格|価|相場).{0,10}(上昇|高騰|急騰|押し上げ|上が|最高)|値上がり|record high",
    "価格下落": r"(価格|価|相場).{0,10}(下落|低下|下が|急落)|値下がり",
    "代替・切替": r"代替|切り替|切替|石炭|転換|switch",
}
# 筋が通っているかの確認: きっかけの種類 → 期待される向き（direction の欄）
CONSISTENCY = [
    ("供給障害・事故", "supply", "tighter", "供給障害 → 供給が締まる"),
    ("輸出規制・制裁・関税", "supply", "tighter", "輸出規制・制裁 → 供給が締まる"),
    ("増産・新規供給", "supply", "looser", "増産・新規供給 → 供給が緩む"),
    ("技術・産業の需要", "demand", "increase", "技術・産業の需要 → 需要が増える"),
    ("中央銀行・投資需要", "demand", "increase", "中央銀行・投資需要 → 需要が増える"),
]
UNSTATED = re.compile(r"記載(されて)?(い)?ない|記されていない|不明|言及されていない|明記されていない")

SQL = """
SELECT id, target, publish_date, source_domain, title, url, llm_analysis
FROM articles
WHERE collection_mode = 'analyze' AND target = ANY(:targets) AND llm_analysis->>'prompt_version' = :version
"""


def themes(text_: str, table: dict) -> list[str]:
    found = [name for name, pattern in table.items() if re.search(pattern, text_ or "", re.I)]
    # 「鉱山の操業停止」のような障害の文は増産として数えない
    if "増産・新規供給" in found and "供給障害・事故" in found:
        found.remove("増産・新規供給")
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="コモディティの需要シグナルの因果を集計する")
    parser.add_argument("--version", default="demand-v4", help="集計する判定の版")
    parser.add_argument("--out", default="eval_results/causal_summary", help="出力先ディレクトリの親")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        rows = [dict(r) for r in session.execute(text(SQL), {"targets": COMMODITIES, "version": args.version}).mappings().all()]
    finally:
        session.close()

    records = []
    for r in rows:
        a = r["llm_analysis"] or {}
        if a.get("excluded"):
            continue
        causal = a.get("causal") or {}
        direction = a.get("direction") or {}
        evidence = a.get("evidence") or {}
        parts = [causal.get(k) for k in ("trigger", "mechanism", "effect")]
        verified = bool(evidence.get("causal_found")) and all(parts) and not any(UNSTATED.search(p or "") for p in parts)
        records.append({
            "id": r["id"], "target": r["target"], "date": r["publish_date"], "week": r["publish_date"].strftime("%G-W%V"),
            "domain": r["source_domain"], "title": r["title"], "url": r["url"],
            "tone": a.get("tone"), "rating": a.get("rating"), "direction": direction,
            "trigger": causal.get("trigger"), "mechanism": causal.get("mechanism"), "effect": causal.get("effect"),
            "quote": evidence.get("causal_evidence"), "verified": verified,
            "trigger_themes": themes(causal.get("trigger"), TRIGGER_THEMES),
            "effect_themes": themes(f"{causal.get('mechanism') or ''} {causal.get('effect') or ''}", EFFECT_THEMES),
        })

    summary = {"version": args.version, "generated_at": datetime.now().isoformat(timespec="seconds"),
               "processed": len(rows), "kept": len(records), "by_target": {}}
    examples, weekly = [], defaultdict(lambda: Counter())
    for target in COMMODITIES:
        recs = [x for x in records if x["target"] == target]
        ver = [x for x in recs if x["verified"]]
        trig = Counter(t for x in ver for t in x["trigger_themes"])
        eff = Counter(e for x in ver for e in x["effect_themes"])
        chains = Counter((t, e) for x in ver for t in x["trigger_themes"] for e in x["effect_themes"])
        consistency = {}
        for theme, field, expected, label in CONSISTENCY:
            hit = [x for x in ver if theme in x["trigger_themes"] and (x["direction"].get(field) or "none") != "none"]
            if hit:
                consistency[label] = {"k": sum(1 for x in hit if x["direction"].get(field) == expected), "n": len(hit)}
        theme_tone = {t: dict(Counter(x["tone"] for x in ver if t in x["trigger_themes"])) for t, _ in trig.most_common(6)}
        summary["by_target"][target] = {
            "processed": sum(1 for r in rows if r["target"] == target),
            "kept": len(recs),
            "verified": len(ver),
            "verified_pct_of_kept": round(100 * len(ver) / len(recs), 1) if recs else None,
            "trigger_classified_pct": round(100 * sum(1 for x in ver if x["trigger_themes"]) / len(ver), 1) if ver else None,
            "effect_classified_pct": round(100 * sum(1 for x in ver if x["effect_themes"]) / len(ver), 1) if ver else None,
            "tone": dict(Counter(x["tone"] for x in recs)),
            "rating3": sum(1 for x in recs if x["rating"] == 3),
            "trigger_themes": trig.most_common(),
            "effect_themes": eff.most_common(),
            "top_chains": [{"trigger": t, "effect": e, "n": n} for (t, e), n in chains.most_common(8)],
            "consistency": consistency,
            "tone_by_trigger_theme": theme_tone,
        }
        # 代表例: 上位の因果の型ごとに、★の高い順・新しい順で2件
        for (t, e), _ in chains.most_common(4):
            picks = sorted((x for x in ver if t in x["trigger_themes"] and e in x["effect_themes"]),
                           key=lambda x: (-(x["rating"] or 0), x["date"]), reverse=False)[:2]
            for x in picks:
                examples.append({"品目": COMMODITY_JA[target], "因果の型": f"{t} → {e}", "記事ID": x["id"],
                                 "観測日": x["date"].strftime("%Y-%m-%d"), "媒体": x["domain"], "見出し": x["title"],
                                 "方向": x["tone"], "★": x["rating"], "きっかけ": x["trigger"], "仕組み": x["mechanism"],
                                 "影響": x["effect"], "根拠の引用（記事に実在）": x["quote"], "URL": x["url"]})
        for x in recs:
            w = weekly[(target, x["week"])]
            w["採用"] += 1
            w["検証できる因果"] += x["verified"]
            w["強気"] += x["tone"] == "bullish"
            w["弱気"] += x["tone"] == "bearish"
            w["★3"] += x["rating"] == 3
            for t in x["trigger_themes"] if x["verified"] else []:
                w[t] += 1

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    if examples:
        with open(os.path.join(out_dir, "examples.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(examples[0]))
            w.writeheader()
            w.writerows(examples)
    cols = ["採用", "検証できる因果", "強気", "弱気", "★3", *TRIGGER_THEMES]
    with open(os.path.join(out_dir, "weekly.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["品目", "週", *cols])
        for (target, week), c in sorted(weekly.items()):
            w.writerow([COMMODITY_JA[target], week, *[c.get(k, 0) for k in cols]])
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
