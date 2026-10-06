"""
ソース信頼度の集計（DBは読み取りのみ）

建設ターゲットの最新プロンプト世代の記事について、次の3つを組み合わせて集計する。
  1. 媒体の区分（sources.yaml の tiers）
  2. 配信系列（sources.yaml の networks。系列内の複数ドメインは独立した裏取りとみなさない）
  3. 記事ごとの取材性（classify_sources.py が付ける llm_analysis["source_assessment"]）
出来事（analyze_clusters.py と同じまとめ方）ごとに信頼度を A〜D に区分する。
  A 裏取りあり        : 系列の異なる2媒体以上が、異なる見出しで報じた
  B 信頼できる単独報道: 通信社・業界専門・主要媒体の報道、または実名の情報源がある一次報道
  C 要注意            : プレスリリース・まとめ・ブログ等の記事しかない
  D その他            : 上記以外（区分未登録の媒体の、実名情報源のない記事のみ 等）

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor_construction python analyze_sources.py
"""

import argparse
import json
import os
from collections import Counter
from datetime import datetime

import yaml
from sqlalchemy import text

from analyze_clusters import _base_title, _registrable_domain, group_events
from database import SessionLocal

SQL = """
SELECT id, publish_date, source_domain, title,
       (llm_analysis->>'excluded')::boolean           AS excluded,
       llm_analysis->'construction'->>'building_name' AS building_name,
       llm_analysis->'construction'->>'status'        AS status,
       llm_analysis->'source_assessment'              AS assessment
FROM articles
WHERE target LIKE 'large_scale%'
  AND is_llm_processed
  AND llm_analysis->'construction' ? 'project_evidence'
"""

RELIABLE_TIERS = {"wire", "trade", "national"}
WEAK_TIERS = {"aggregator", "pr", "blog"}
WEAK_SOURCE_TYPES = {"press_release", "aggregation", "other"}


def load_sources(path: str) -> tuple[dict, dict]:
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    tier_of = {d: tier for tier, domains in config.get("tiers", {}).items() for d in domains}
    network_of = {d: net for net, domains in config.get("networks", {}).items() for d in domains}
    return tier_of, network_of


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def classify_event(items: list[dict]) -> str:
    outlets = {r["outlet"] for r in items}
    titles = {_base_title(r["title"]) for r in items}
    if len(outlets) >= 2 and len(titles) >= 2:
        return "A"
    for r in items:
        a = r["assessment"] or {}
        if r["tier"] in RELIABLE_TIERS and a.get("source_type") not in ("press_release", "aggregation"):
            return "B"
        if a.get("source_type") == "original_reporting" and a.get("has_named_source"):
            return "B"
    weak = all(
        r["tier"] in WEAK_TIERS or (r["assessment"] or {}).get("source_type") in WEAK_SOURCE_TYPES
        for r in items
    )
    return "C" if weak else "D"


def main() -> None:
    parser = argparse.ArgumentParser(description="ソース信頼度の集計（DB読み取りのみ）")
    parser.add_argument("--sources", default="sources.yaml", help="媒体の区分・系列の定義ファイル")
    parser.add_argument("--gap-days", type=int, default=7, help="この日数以上空いたら別の出来事とみなす")
    parser.add_argument("--out", default="eval_results/sources", help="出力先ディレクトリの親")
    args = parser.parse_args()

    tier_of, network_of = load_sources(args.sources)

    session = SessionLocal()
    try:
        rows = [dict(r) for r in session.execute(text(SQL)).mappings().all()]
    finally:
        session.close()

    for r in rows:
        domain = _registrable_domain(r["source_domain"])
        r["domain"] = domain
        r["tier"] = tier_of.get(domain, "unclassified")
        r["network"] = network_of.get(domain)
        r["outlet"] = r["network"] or domain  # 系列があれば系列単位で1媒体とみなす
        if (r["building_name"] or "").strip().lower() in ("", "null", "none", "n/a", "unknown"):
            r["building_name"] = None

    kept = [r for r in rows if not r["excluded"]]
    assessed = [r for r in kept if r["assessment"]]

    # 1. 媒体の区分
    tier_all = Counter(r["tier"] for r in rows)
    tier_kept = Counter(r["tier"] for r in kept)
    excluded_rate_by_tier = {
        t: _pct(sum(1 for r in rows if r["tier"] == t and r["excluded"]), n) for t, n in tier_all.items()
    }

    # 2. 記事ごとの取材性
    source_types = Counter(r["assessment"].get("source_type") for r in assessed)
    crosstab = Counter((r["tier"], r["assessment"].get("source_type")) for r in assessed)

    # 3. 出来事ごとの信頼度（analyze_clusters.py と同じまとめ方）
    event_grades = Counter(classify_event(items) for _, items in group_events(kept, args.gap_days))
    n_events = sum(event_grades.values())

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "articles_all": len(rows),
        "articles_kept": len(kept),
        "articles_kept_assessed": len(assessed),
        "tier_coverage_kept_pct": _pct(sum(n for t, n in tier_kept.items() if t != "unclassified"), len(kept)),
        "tier_kept": dict(tier_kept.most_common()),
        "tier_kept_pct": {t: _pct(n, len(kept)) for t, n in tier_kept.most_common()},
        "excluded_rate_by_tier_pct": excluded_rate_by_tier,
        "articles_kept_from_networks": sum(1 for r in kept if r["network"]),
        "articles_kept_from_networks_pct": _pct(sum(1 for r in kept if r["network"]), len(kept)),
        "network_kept": dict(Counter(r["network"] for r in kept if r["network"]).most_common()),
        "source_type_pct": {t: _pct(n, len(assessed)) for t, n in source_types.most_common()},
        "has_named_source_pct": _pct(sum(1 for r in assessed if r["assessment"].get("has_named_source")), len(assessed)),
        "has_direct_quote_pct": _pct(sum(1 for r in assessed if r["assessment"].get("has_direct_quote")), len(assessed)),
        "cites_document_pct": _pct(sum(1 for r in assessed if r["assessment"].get("cites_document")), len(assessed)),
        "tier_x_source_type": {f"{t}|{s}": n for (t, s), n in crosstab.most_common()},
        "events": n_events,
        "event_grade": dict(sorted(event_grades.items())),
        "event_grade_pct": {g: _pct(n, n_events) for g, n in sorted(event_grades.items())},
        "event_grade_A_or_B_pct": _pct(event_grades["A"] + event_grades["B"], n_events),
    }

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
