"""
LLMバックエンド比較スクリプト（Gemini vs Ollama）

DB内の記事をサンプリングし、同一プロンプトで両バックエンドを実行して結果を比較する。
DBへの書き込みは一切行わない。結果は --out ディレクトリに保存する。
  - results.jsonl        : 記事ごとの両バックエンドの生出力
  - summary.json         : 一致率などの集計
  - labels_template.csv  : 人手ラベル付け用テンプレート（human_* 列を記入）

実行例（プロジェクトをマウントして一時コンテナで実行、結果はホストに残る）:
  docker compose run --rm -v "$PWD:/app" llm_processor_construction \
      python compare_llm.py --targets large_scale_construction,large_scale_construction_ja --limit 50
"""

import argparse
import csv
import json
import os
import time
from datetime import date, datetime, timedelta

from database import Article, SessionLocal
from llm_processor import (
    CONSTRUCTION_TARGETS,
    GEMINI_MODEL,
    OLLAMA_MODEL,
    _build_prompt,
    _GeminiBackend,
    _OllamaBackend,
    _parse_json,
)
from ngrams_fetcher import TARGETS

EVENT_DATE_MIN = date(2000, 1, 1)  # これより前のevent_dateは異常値とみなす


def _load_articles(targets: set[str] | None, limit: int) -> list[Article]:
    session = SessionLocal()
    try:
        query = session.query(Article)
        if targets:
            query = query.filter(Article.target.in_(targets))
        return query.order_by(Article.id.desc()).limit(limit).all()
    finally:
        session.close()


def _run_backend(backend, prompt: str, interval: float) -> tuple[dict | None, float, str | None]:
    start = time.time()
    try:
        raw = backend.call(prompt)
        result = _parse_json(raw)
        error = None if result else "JSONパース失敗"
    except Exception as e:
        result, error = None, str(e)[:200]
    elapsed = time.time() - start
    if interval:
        time.sleep(interval)
    return result, elapsed, error


def _event_date_anomaly(value, publish_date: datetime | None) -> bool:
    """event_dateが2000年より前、または記事公開日の翌日より後なら異常とみなす。"""
    if not value:
        return False
    try:
        d = date.fromisoformat(value)
    except (ValueError, TypeError):
        return True
    if d < EVENT_DATE_MIN:
        return True
    if publish_date and d > (publish_date + timedelta(days=1)).date():
        return True
    return False


def _rate(pairs: list[tuple]) -> dict:
    n = len(pairs)
    agree = sum(1 for a, b in pairs if a == b)
    return {"n": n, "agree": agree, "rate": round(agree / n, 3) if n else None}


def _summarize(records: list[dict], names: list[str]) -> dict:
    summary: dict = {"n_articles": len(records), "backends": {}}

    for name in names:
        rs = [r[name] for r in records]
        ok = [x for x in rs if x["result"]]
        dates = [(x["result"].get("event_date"), r["publish_date"]) for x, r in zip(rs, records) if x["result"]]
        summary["backends"][name] = {
            "model": rs[0]["model"] if rs else None,
            "failed": len(rs) - len(ok),
            "avg_seconds": round(sum(x["seconds"] for x in rs) / len(rs), 2) if rs else None,
            "excluded_rate": round(sum(1 for x in ok if x["result"].get("excluded")) / len(ok), 3) if ok else None,
            "event_date_null": sum(1 for d, _ in dates if not d),
            "event_date_anomaly": sum(
                1 for d, p in dates
                if _event_date_anomaly(d, datetime.fromisoformat(p) if p else None)
            ),
            "articles_with_milestones": sum(1 for x in ok if x["result"].get("milestones")),
            "event_date_basis": {
                b: sum(1 for x in ok if x["result"].get("event_date_basis") == b)
                for b in ("explicit", "relative", "publication", None)
            },
        }

    if len(names) == 2:
        a, b = names
        both = [r for r in records if r[a]["result"] and r[b]["result"]]

        def get(r, n, k):
            return r[n]["result"].get(k)

        agreement = {
            "excluded": _rate([(bool(get(r, a, "excluded")), bool(get(r, b, "excluded"))) for r in both]),
            "rating": _rate([(get(r, a, "rating"), get(r, b, "rating")) for r in both]),
            "tone": _rate([(get(r, a, "tone"), get(r, b, "tone")) for r in both]),
            "event_date": _rate([(get(r, a, "event_date"), get(r, b, "event_date")) for r in both]),
        }
        cons = [r for r in both if r["target"] in CONSTRUCTION_TARGETS]
        if cons:
            agreement["construction_status"] = _rate(
                [(get(r, a, "construction_status"), get(r, b, "construction_status")) for r in cons]
            )
        diffs = [
            abs(float(get(r, a, "tone_score")) - float(get(r, b, "tone_score")))
            for r in both
            if isinstance(get(r, a, "tone_score"), (int, float)) and isinstance(get(r, b, "tone_score"), (int, float))
        ]
        agreement["tone_score_mae"] = round(sum(diffs) / len(diffs), 2) if diffs else None
        summary["agreement"] = agreement

    return summary


def _write_label_template(path: str, records: list[dict], names: list[str]) -> None:
    fields = ["id", "target", "title", "url", "publish_date"]
    for name in names:
        fields += [f"{name}_excluded", f"{name}_rating", f"{name}_tone", f"{name}_event_date", f"{name}_status"]
    fields += ["human_excluded", "human_rating", "human_tone", "human_event_date", "human_status", "memo"]

    with open(path, "w", encoding="utf-8-sig", newline="") as f:  # Excelで文字化けしないようBOM付き
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in records:
            row = {k: r[k] for k in ["id", "target", "title", "url", "publish_date"]}
            for name in names:
                res = r[name]["result"] or {}
                row[f"{name}_excluded"] = res.get("excluded")
                row[f"{name}_rating"] = res.get("rating")
                row[f"{name}_tone"] = res.get("tone")
                row[f"{name}_event_date"] = res.get("event_date")
                row[f"{name}_status"] = res.get("construction_status")
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini vs Ollama 比較（DBは読み取りのみ）")
    parser.add_argument("--targets", default="", help="対象ターゲット（カンマ区切り）。未指定なら全ターゲット")
    parser.add_argument("--limit", type=int, default=50, help="比較する記事数（新しい順）")
    parser.add_argument("--backends", default="gemini,ollama", help="実行するバックエンド（カンマ区切り）")
    parser.add_argument("--out", default="eval_results", help="出力先ディレクトリの親")
    args = parser.parse_args()

    targets = {t.strip() for t in args.targets.split(",") if t.strip()} or None
    names = [n.strip() for n in args.backends.split(",") if n.strip()]
    factories = {
        "gemini": (_GeminiBackend, GEMINI_MODEL, _GeminiBackend.REQUEST_INTERVAL),
        "ollama": (_OllamaBackend, OLLAMA_MODEL, 0),
    }
    backends = {n: factories[n][0]() for n in names}

    articles = _load_articles(targets, args.limit)
    print(f"{len(articles)} 件を比較します（backends={names}）")

    out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    target_labels = {k: v["label"] for k, v in TARGETS.items()}

    records = []
    with open(os.path.join(out_dir, "results.jsonl"), "w", encoding="utf-8") as f:
        for i, row in enumerate(articles, 1):
            prompt = _build_prompt(
                target_labels.get(row.target, row.target),
                row.title or row.raw_data.get("title", ""),
                row.source_domain or row.raw_data.get("domain", ""),
                row.publish_date.strftime("%Y-%m-%d") if row.publish_date else "",
                row.body or "",
                row.target in CONSTRUCTION_TARGETS,
            )
            rec = {
                "id": row.id,
                "target": row.target,
                "title": row.title,
                "url": row.url,
                "publish_date": row.publish_date.isoformat() if row.publish_date else None,
            }
            for name in names:
                result, seconds, error = _run_backend(backends[name], prompt, factories[name][2])
                rec[name] = {"model": factories[name][1], "result": result, "seconds": round(seconds, 2), "error": error}
            records.append(rec)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{i}/{len(articles)}] id={row.id} " + " ".join(
                f"{n}={(rec[n]['result'] or {}).get('tone')}/{(rec[n]['result'] or {}).get('construction_status')}"
                for n in names
            ))

    summary = _summarize(records, names)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    _write_label_template(os.path.join(out_dir, "labels_template.csv"), records, names)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"出力先: {out_dir}")


if __name__ == "__main__":
    main()
