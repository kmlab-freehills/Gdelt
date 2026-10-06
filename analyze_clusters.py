"""
重複・報道の広がりの集計（DBは読み取りのみ）

最新プロンプト世代で「採用」された建設記事を、案件単位・出来事単位にまとめて集計する。
  - 案件キー: 案件名を正規化したもの（無ければ見出しを正規化したもの）
  - 出来事:   同じ案件の記事を日付順に並べ、GAP_DAYS日以上空いたら別の出来事とみなす
出力（--out 配下の日時フォルダ）:
  - summary.json : 卒論・ゼミ用の集計値
  - events.csv   : 出来事ごとの明細（記事数・媒体数・初報/ピーク/最終報の日付など）

実行例:
  docker compose run --rm --no-deps -v "$PWD:/app" llm_processor_construction python analyze_clusters.py
"""

import argparse
import csv
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import text

from database import SessionLocal

SQL = """
SELECT id, publish_date, source_domain, title,
       llm_analysis->'construction'->>'building_name' AS building_name,
       llm_analysis->'construction'->>'status'        AS status
FROM articles
WHERE target LIKE 'large_scale%'
  AND is_llm_processed
  AND llm_analysis->'construction' ? 'project_evidence'
  AND NOT (llm_analysis->>'excluded')::boolean
"""


def _normalize(value: str | None) -> str:
    """比較用に小文字化し、先頭の the と記号・空白を除去する（CJK文字は残る）。"""
    if not value:
        return ""
    value = re.sub(r"^\s*the\s+", "", value.lower())
    return re.sub(r"\W+", "", value)


# 「co.uk」のように2階層で1つの公開接尾辞になるもの（登録ドメインは末尾3ラベル）
_SECOND_LEVEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "org.au", "co.nz", "co.za",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "co.in", "com.cn", "com.hk",
    "com.my", "com.sg", "com.br", "com.pk", "com.ng", "co.ke", "com.tr", "com.mx",
}


def _registrable_domain(domain: str | None) -> str:
    """650keni.iheart.com → iheart.com のように、系列局のサブドメインを1媒体にまとめる。"""
    if not domain:
        return ""
    labels = domain.lower().split(":")[0].strip(".").split(".")  # "asiaone.com:443" のポート番号を除く
    if len(labels) >= 3 and ".".join(labels[-2:]) in _SECOND_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _base_title(title: str | None) -> str:
    """見出し末尾の「 | 媒体名」「 - 媒体名」を除いて正規化する（転載の判定用）。"""
    if not title:
        return ""
    return _normalize(re.split(r"\s+[|｜]\s+|\s+[-–—]\s+", title)[0])


def _percentiles(values: list[int]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "mean": round(statistics.mean(ordered), 2),
        "median": statistics.median(ordered),
        "p75": ordered[int(len(ordered) * 0.75) - 1] if len(ordered) >= 4 else ordered[-1],
        "p90": ordered[int(len(ordered) * 0.9) - 1] if len(ordered) >= 10 else ordered[-1],
        "max": ordered[-1],
    }


def _bucket(n: int) -> str:
    if n == 1:
        return "1"
    if n == 2:
        return "2"
    if n <= 4:
        return "3-4"
    if n <= 9:
        return "5-9"
    return "10+"


def group_events(rows: list[dict], gap_days: int) -> list[tuple[str, list[dict]]]:
    """記事を (案件キー, その出来事の記事一覧) のリストにまとめる。"""
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = _normalize(r["building_name"]) or "title:" + _normalize(r["title"])
        by_key[key].append(r)

    grouped = []
    for key, items in by_key.items():
        items.sort(key=lambda r: r["publish_date"])
        current: list[dict] = []
        for r in items:
            if current and (r["publish_date"].date() - current[-1]["publish_date"].date()).days >= gap_days:
                grouped.append((key, current))
                current = []
            current.append(r)
        grouped.append((key, current))
    return grouped


def build_events(rows: list[dict], gap_days: int) -> list[dict]:
    return [_summarize_event(key, items) for key, items in group_events(rows, gap_days)]


def _summarize_event(key: str, items: list[dict]) -> dict:
    days = Counter(r["publish_date"].date() for r in items)
    first, last = min(days), max(days)
    peak = min(d for d, c in days.items() if c == max(days.values()))  # 同数なら早い日
    return {
        "project_key": key,
        "building_name": next((r["building_name"] for r in items if r["building_name"]), None),
        "n_articles": len(items),
        "n_hostnames": len({r["source_domain"] for r in items if r["source_domain"]}),
        # 系列局のサブドメインをまとめた実質的な媒体数（裏取りの指標はこちらを使う）
        "n_domains": len({_registrable_domain(r["source_domain"]) for r in items if r["source_domain"]}),
        # 見出しが異なる記事の数（転載ではない独立した報道の目安）
        "n_distinct_titles": len({_base_title(r["title"]) for r in items}),
        "first_date": first.isoformat(),
        "peak_date": peak.isoformat(),
        "last_date": last.isoformat(),
        "days_first_to_peak": (peak - first).days,
        "days_first_to_last": (last - first).days,
        "statuses": ",".join(sorted({r["status"] for r in items if r["status"]})),
        "example_title": items[0]["title"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="重複・報道の広がりの集計（DB読み取りのみ）")
    parser.add_argument("--gap-days", type=int, default=7, help="この日数以上空いたら別の出来事とみなす")
    parser.add_argument("--out", default="eval_results/clusters", help="出力先ディレクトリの親")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        rows = [dict(r) for r in session.execute(text(SQL)).mappings().all()]
    finally:
        session.close()
    for r in rows:  # 旧データに残る文字列の "null" を空として扱う
        if (r["building_name"] or "").strip().lower() in ("", "null", "none", "n/a", "unknown"):
            r["building_name"] = None

    # 見出しの重複（転載）の割合。末尾の媒体名を除いて比較する
    title_counts = Counter(_base_title(r["title"]) for r in rows)
    dup_articles = sum(c for c in title_counts.values() if c > 1)

    events = build_events(rows, args.gap_days)
    multi_domain = [e for e in events if e["n_domains"] >= 2]
    # 独立した裏取り: 2媒体以上 かつ 見出しが2種類以上（同一記事の転載だけではない）
    independent = [e for e in events if e["n_domains"] >= 2 and e["n_distinct_titles"] >= 2]
    syndicated_only = [e for e in events if e["n_hostnames"] >= 2 and e["n_distinct_titles"] == 1]
    spread = [e for e in events if e["days_first_to_last"] >= 1]
    projects = Counter(e["project_key"] for e in events)

    dates = [r["publish_date"].date() for r in rows]
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"from": min(dates).isoformat(), "to": max(dates).isoformat(),
                   "days": (max(dates) - min(dates)).days + 1},
        "gap_days": args.gap_days,
        "articles": len(rows),
        "articles_title_duplicated": dup_articles,
        "articles_title_duplicated_pct": round(100 * dup_articles / len(rows), 1),
        "projects": len(projects),
        "events": len(events),
        "projects_with_multiple_events": sum(1 for c in projects.values() if c > 1),
        "events_per_day": round(len(events) / ((max(dates) - min(dates)).days + 1), 2),
        "articles_per_event": _percentiles([e["n_articles"] for e in events]),
        "domains_per_event": _percentiles([e["n_domains"] for e in events]),
        "domains_per_event_buckets": dict(sorted(Counter(_bucket(e["n_domains"]) for e in events).items())),
        "events_corroborated_2plus_domains": len(multi_domain),
        "events_corroborated_2plus_domains_pct": round(100 * len(multi_domain) / len(events), 1),
        "events_independently_reported": len(independent),
        "events_independently_reported_pct": round(100 * len(independent) / len(events), 1),
        "events_syndication_only": len(syndicated_only),
        "articles_in_syndication_only_events": sum(e["n_articles"] for e in syndicated_only),
        # 報道の広がり（2日以上にわたって報じられた出来事のみ）
        "spread_events": len(spread),
        "days_first_to_peak_spread": _percentiles([e["days_first_to_peak"] for e in spread]),
        "days_first_to_last_spread": _percentiles([e["days_first_to_last"] for e in spread]),
        "peak_on_first_day_pct_spread": round(
            100 * sum(1 for e in spread if e["days_first_to_peak"] == 0) / len(spread), 1) if spread else None,
        "distinct_domains": len({_registrable_domain(r["source_domain"]) for r in rows if r["source_domain"]}),
        "top_domains": Counter(_registrable_domain(r["source_domain"]) for r in rows if r["source_domain"]).most_common(30),
        "top_events_by_domains": [
            {k: e[k] for k in ("building_name", "n_articles", "n_hostnames", "n_domains", "n_distinct_titles",
                               "first_date", "peak_date", "last_date")}
            for e in sorted(events, key=lambda e: (-e["n_domains"], -e["n_articles"]))[:10]
        ],
    }

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "events.csv"), "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(events[0].keys()))
        writer.writeheader()
        writer.writerows(sorted(events, key=lambda e: e["first_date"]))

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
