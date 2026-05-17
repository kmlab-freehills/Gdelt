#!/usr/bin/env python3
"""
需要シグナル収集スクリプト - GDELT DOC API版

Usage:
  python demand_fetcher.py                          # copper (デフォルト)
  python demand_fetcher.py --commodity gold uranium
  python demand_fetcher.py --commodity all --days 7
"""

import requests
import json
import argparse
import time
import re
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional

try:
    import trafilatura
    _TRAFILATURA_AVAILABLE = True
except ImportError:
    _TRAFILATURA_AVAILABLE = False

# ============================================================
# ノイズドメイン ブラックリスト
# sourcelang=eng 指定でも混入する非英語・低品質ドメインを除外
# ============================================================
DOMAIN_BLACKLIST: Set[str] = {
    # 中国系
    "cnfol.com", "eastmoney.com", "finance.sina.com.cn", "hexun.com",
    "stock.10jqka.com.cn", "cls.cn", "yicai.com", "caixin.com",
    # 韓国系
    "insight.co.kr", "hankyung.com", "mk.co.kr", "edaily.co.kr",
    # プレスリリース配信 (シンジケーション源としてノイズが多い)
    "prnewswire.com", "businesswire.com", "globenewswire.com",
    "accesswire.com", "einpresswire.com",
    # アグリゲーター系低品質
    "markets.businessinsider.com", "247wallst.com",
}

# ============================================================
# クエリ設計方針（3層構造）
#
# Layer 1 フレーズ層    : "X demand" など確実にヒットするアンカー（精度優先）
# Layer 2 類義語層      : consumption/offtake/procurement など表現の揺れをカバー（再現率優先）
# Layer 3 デルタ検知層  : "ahead of" / "unexpected" / "exceeds forecast" など
#                         コンセンサス超えを示す語をアンカーにする（サプライズ優先）
#
# マイナス検索で市況まとめ・株式デイリーを除外（GDELT構文: -word / -"phrase"）
# ============================================================

_NF = (  # 共通ノイズフィルタ（全クエリに付加）
    ' -"market report" -"daily report" -"trading update"'
    ' -"stock market" -"stock price" -"share price"'
    ' -"price today" -"market wrap" -"earnings report"'
)

COMMODITIES: Dict[str, Dict] = {
    "copper": {
        "label": "銅 (Copper)",
        "queries": [
            # --- Layer 1: フレーズ層 ---
            f'"copper demand" surge{_NF}',
            f'"copper demand" shortage{_NF}',
            # --- Layer 2: 類義語・業界用語層 ---
            # consumption/offtake/procurement で "demand" 以外の表現をカバー
            f'copper consumption surge{_NF}',
            f'"copper offtake" increase{_NF}',
            f'"copper procurement" shortage{_NF}',
            # 別称 "red metal" / 製品形態別
            f'"red metal" demand shortage{_NF}',
            f'"copper cathode" shortage{_NF}',
            # --- Layer 3: デルタ検知層 ---
            f'copper demand "ahead of forecast"{_NF}',
            f'copper demand "exceeds" forecast{_NF}',
            f'"unexpected" copper demand{_NF}',
            f'copper shortage "bottleneck"{_NF}',
        ],
    },
    "gold": {
        "label": "金 (Gold)",
        "queries": [
            # --- Layer 1 ---
            f'"gold demand" record{_NF}',
            f'"gold demand" surge{_NF}',
            # --- Layer 2 ---
            f'gold consumption "central bank"{_NF}',
            f'"gold buying" surge{_NF}',
            f'"bullion demand" surge{_NF}',
            f'"gold bullion" shortage{_NF}',
            # --- Layer 3 ---
            f'gold demand "ahead of forecast"{_NF}',
            f'"unexpected" gold demand{_NF}',
            f'"record gold" purchase{_NF}',
            f'gold "supply crunch"{_NF}',
        ],
    },
    "uranium": {
        "label": "ウラン (Uranium)",
        "queries": [
            # --- Layer 1 ---
            f'"uranium demand" nuclear{_NF}',
            f'"uranium demand" surge{_NF}',
            # --- Layer 2 ---
            f'uranium consumption reactor{_NF}',
            f'"uranium procurement" shortage{_NF}',
            f'"yellowcake" demand{_NF}',
            f'"U3O8" demand shortage{_NF}',
            # --- Layer 3 ---
            f'uranium demand "ahead of forecast"{_NF}',
            f'"unexpected" uranium demand{_NF}',
            f'uranium shortage "reactor"{_NF}',
            f'uranium "supply crunch"{_NF}',
        ],
    },
    "rare earth": {
        "label": "レアアース (Rare Earth)",
        "queries": [
            # --- Layer 1 ---
            f'"rare earth" demand surge{_NF}',
            f'"rare earth" shortage{_NF}',
            # --- Layer 2 ---
            # 汎用別称
            f'"critical minerals" shortage{_NF}',
            f'"strategic minerals" demand{_NF}',
            # 主要元素個別（磁石・電池用途で特に重要）
            f'neodymium shortage demand{_NF}',
            f'dysprosium shortage demand{_NF}',
            f'"rare earth" "export restriction"{_NF}',
            # --- Layer 3 ---
            f'"rare earth" shortage "ahead of"{_NF}',
            f'"unexpected" "rare earth" shortage{_NF}',
            f'"rare earth" "supply crunch" bottleneck{_NF}',
            f'"critical minerals" "bottleneck"{_NF}',
        ],
    },
    "natural gas": {
        "label": "天然ガス・LNG (Natural Gas / LNG)",
        "queries": [
            # --- Layer 1 ---
            f'"natural gas" demand surge{_NF}',
            f'"LNG demand" surge{_NF}',
            # --- Layer 2 ---
            f'"gas consumption" record{_NF}',
            f'"LNG imports" surge{_NF}',
            f'"pipeline gas" shortage{_NF}',
            f'LNG "spot demand" surge{_NF}',
            # --- Layer 3 ---
            f'"natural gas" demand "ahead of forecast"{_NF}',
            f'LNG demand "unexpected" surge{_NF}',
            f'"natural gas" "supply crunch"{_NF}',
            f'LNG "bottleneck" demand{_NF}',
        ],
    },
    "silver": {
        "label": "銀 (Silver)",
        "queries": [
            # --- Layer 1 ---
            f'"silver demand" surge{_NF}',
            f'"silver demand" shortage{_NF}',
            # --- Layer 2 ---
            f'silver consumption solar{_NF}',
            f'"silver offtake" increase{_NF}',
            f'"silver bullion" shortage{_NF}',
            f'"industrial silver" demand{_NF}',
            # --- Layer 3 ---
            f'silver demand "ahead of forecast"{_NF}',
            f'"unexpected" silver demand{_NF}',
            f'silver shortage "solar"{_NF}',
            f'silver "supply deficit"{_NF}',
        ],
    },
}

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS_PER_QUERY = 5   # クエリ数増加分を相殺（重複除去後で十分な量が残る）
SLEEP_BETWEEN_QUERIES = 6.0
MAX_RETRIES = 3

SCRAPE_TEXT_LIMIT = 600     # LLMに渡す本文の最大文字数
SCRAPE_SLEEP = 2.0          # 記事サイト負荷対策
SCRAPE_TIMEOUT = 10         # trafilatura fetch のタイムアウト（秒）

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ============================================================
# 日付パース（堅牢化）
# GDELTの seendate は "20260509T141023Z" や "20260509141023" 等が混在
# ============================================================
_DATE_PATTERNS = [
    "%Y%m%dT%H%M%SZ",   # 20260509T141023Z
    "%Y%m%dT%H%M%S",    # 20260509T141023
    "%Y%m%d%H%M%S",     # 20260509141023
    "%Y%m%d",           # 20260509
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
]


def parse_gdelt_date(raw: str) -> str:
    """GDELTの日付文字列を YYYY-MM-DD に変換。失敗時は 'Unknown' を返す。"""
    if not raw:
        return "Unknown"
    cleaned = raw.strip()
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # 先頭8文字が数字ならフォールバック
    if re.match(r"^\d{8}", cleaned):
        try:
            return datetime.strptime(cleaned[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return "Unknown"


# ============================================================
# タイトル正規化（シンジケーション重複排除用）
# ============================================================
def normalize_title(title: str) -> str:
    """小文字化・前後空白除去・連続空白を1つに圧縮。"""
    return re.sub(r"\s+", " ", title.lower().strip())


# ============================================================
# 記事本文スクレイピング
# ============================================================
def scrape_article_text(url: str) -> Optional[str]:
    """
    trafilatura で記事本文を取得し SCRAPE_TEXT_LIMIT 文字でトリミングして返す。
    取得失敗・ペイウォール等の場合は None を返す。
    """
    if not _TRAFILATURA_AVAILABLE or not url:
        return None
    try:
        downloaded = trafilatura.fetch_url(url, timeout=SCRAPE_TIMEOUT)
        if not downloaded:
            return None
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        if not text:
            return None
        text = text.strip()
        if len(text) > SCRAPE_TEXT_LIMIT:
            text = text[:SCRAPE_TEXT_LIMIT] + "…"
        return text
    except Exception:
        return None


def enrich_articles_with_text(articles: List[Dict]) -> None:
    """
    articles リストを破壊的に更新し、各要素に 'body' キーを追加する。
    取得失敗時は 'body' = None。
    """
    total = len(articles)
    for i, art in enumerate(articles, 1):
        url = art.get("url", "")
        domain = art.get("domain", "")
        print(f"  [{i}/{total}] 本文取得中: {domain}", flush=True)
        art["body"] = scrape_article_text(url)
        if art["body"]:
            print(f"    ✅ 取得成功 ({len(art['body'])} 文字)", flush=True)
        else:
            print(f"    ⚠️  取得失敗（タイトルのみで分析）", flush=True)
        time.sleep(SCRAPE_SLEEP)


# ============================================================
# API呼び出し
# ============================================================
def fetch_articles(query: str, days: int = 14) -> List[Dict]:
    timespan = f"{days * 24}h" if days <= 1 else f"{days}d"

    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": MAX_RECORDS_PER_QUERY,
        "timespan": timespan,
        "format": "json",
        "sourcelang": "eng",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                GDELT_DOC_API, params=params, headers=HEADERS, timeout=20
            )

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 15 * attempt))
                print(
                    f"  [429] レート制限。{wait}秒待機してリトライ"
                    f" ({attempt}/{MAX_RETRIES})...",
                    flush=True,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json().get("articles", []) or []

        except requests.exceptions.HTTPError as e:
            print(f"  [HTTP ERROR] {e} (試行 {attempt}/{MAX_RETRIES})", flush=True)
            if attempt < MAX_RETRIES:
                time.sleep(8 * attempt)
        except requests.exceptions.RequestException as e:
            print(f"  [ERROR] リクエスト失敗 (試行 {attempt}/{MAX_RETRIES}): {e}", flush=True)
            if attempt < MAX_RETRIES:
                time.sleep(8 * attempt)
        except (json.JSONDecodeError, ValueError):
            snippet = resp.text[:300] if "resp" in dir() else "(no response)"
            print(f"  [ERROR] JSONパース失敗: {snippet}", flush=True)
            return []

    return []


# ============================================================
# LLMプロンプト生成
# ============================================================
def build_llm_prompt(commodity_label: str, articles: List[Dict], exclude_consensus: bool = False) -> str:
    sep = "=" * 64

    if not articles:
        return (
            f"\n{sep}\n"
            f"【{commodity_label}】\n"
            f"記事が取得できませんでした。クエリまたは期間を見直してください。\n"
            f"{sep}\n"
        )

    lines = [
        sep,
        f"【LLM PROMPT】{commodity_label} 需要シグナル分析",
        sep,
        "",
        f"以下は「{commodity_label}」に関する直近のニュース記事の一覧です。",
        "各記事のタイトル・URL・日付をもとに、下記のルールに従って分析してください。",
        "",
        "【除外ルール（最優先）】",
        "以下に該当する記事は分析対象から除外し、番号の横に「[除外]」と明記してください。",
        "  - 市況まとめ・デイリーマーケットレポート・株価サマリー記事",
        "  - 当該コモディティの需要と直接関係のない一般的な金融・経済ニュース",
        "  - 明らかに同一内容の焼き直しや重複・類似記事",
        *(
            [
                "  - 「AIがデータセンターを増設している」「EVが普及している」など、",
                "    市場参加者の間ですでにコンセンサスとなっている長期トレンドを",
                "    単に追認しているだけの記事（新規情報がない）",
            ]
            if exclude_consensus else []
        ),
        "",
        "【評価基準（重要度の高い順）】",
        "除外対象でない記事を、以下の観点で評価し重要度を判定してください。",
        "",
        "★★★ 最高評価: コンセンサスからの「ズレ・変化率（デルタ）」を示す記事",
        "  - 既存の需要予測・生産計画を上回る急加速、または予期せぬ下振れ",
        "  - アナリスト・業界コンセンサスを覆す新データや企業発表",
        "  - 市場がまだ織り込んでいないと思われる先行指標や政策転換",
        "",
        "★★☆ 高評価: 需要の「二次的影響」を報じる記事",
        "  - 需要増の結果として生じる代替品へのシフト、素材・製品の代替調達",
        "  - サプライチェーンのボトルネック、精錬・加工能力の限界に言及",
        "  - 周辺インフラ（港湾・輸送・電力網等）の逼迫や投資加速",
        "",
        "★☆☆ 低評価（参考情報）: 既知トレンドの再確認にとどまる記事",
        "  - AI・EV・エネルギー転換などの長期トレンドを述べるだけで",
        "    新たな具体的事実・数値・発表を含まない記事",
        "  → 「中立・ベースライン（新規シグナルとしての価値は低い）」と判定",
        "",
        "【分析タスク】",
        "1. 除外対象外の各記事に対し、上記★評価と判定理由を1〜2文で記述する",
        "2. 需要変動のドライバー（原因・背景）を具体的に特定する",
        "   例: 予想を超えたデータセンター着工件数、代替銅線不足による入札急増、等",
        "3. ★★★または★★☆に該当する記事の中から最も重要な上位3件を選び、",
        "   「なぜコンセンサスを超える情報といえるか」を明確に述べる",
        "",
        "【記事一覧】",
        "",
    ]

    for i, art in enumerate(articles, 1):
        title = art.get("title", "(タイトルなし)")
        url = art.get("url", "")
        domain = art.get("domain", "")
        date_str = parse_gdelt_date(art.get("seendate", ""))
        body = art.get("body")  # scrape済みの場合のみ存在

        lines.append(f"[{i}] {title}")
        lines.append(f"    日付: {date_str} | メディア: {domain}")
        lines.append(f"    URL: {url}")
        if body:
            lines.append(f"    本文抜粋: {body}")
        else:
            lines.append(f"    本文抜粋: （取得不可 - タイトルのみで判断）")
        lines.append("")

    lines.append(sep)
    return "\n".join(lines)


# ============================================================
# メイン処理
# ============================================================
def run(commodity_keys: List[str], days: int, scrape: bool = True, exclude_consensus: bool = False) -> None:
    all_prompts = []

    for key in commodity_keys:
        config = COMMODITIES.get(key)
        if not config:
            print(f"[WARN] 不明なコモディティ: {key}", flush=True)
            continue

        label = config["label"]
        print(f"\n{'─' * 50}", flush=True)
        print(f"  検索中: {label}", flush=True)
        print(f"{'─' * 50}", flush=True)

        seen_urls: Set[str] = set()
        seen_titles: Set[str] = set()
        articles: List[Dict] = []

        for q in config["queries"]:
            print(f"  クエリ: {q}", flush=True)
            results = fetch_articles(q, days=days)
            new_count = 0
            skipped_domain = 0
            skipped_dup = 0

            for art in results:
                url = art.get("url", "")
                domain = art.get("domain", "")
                title = art.get("title", "")

                # ドメインブラックリストフィルタ
                if domain in DOMAIN_BLACKLIST:
                    skipped_domain += 1
                    continue

                # URL重複チェック
                if url and url in seen_urls:
                    skipped_dup += 1
                    continue

                # タイトル正規化による重複チェック（シンジケーション対策）
                norm = normalize_title(title)
                if norm and norm in seen_titles:
                    skipped_dup += 1
                    continue

                if url:
                    seen_urls.add(url)
                if norm:
                    seen_titles.add(norm)
                articles.append(art)
                new_count += 1

            print(
                f"  → 新規 {new_count} 件"
                f" (ドメイン除外 {skipped_domain} 件, 重複除外 {skipped_dup} 件,"
                f" 累計 {len(articles)} 件)",
                flush=True,
            )
            time.sleep(SLEEP_BETWEEN_QUERIES)

        print(f"  ✅ ユニーク記事合計: {len(articles)} 件", flush=True)

        if scrape and articles:
            if _TRAFILATURA_AVAILABLE:
                print(f"\n  📄 本文取得開始 ({len(articles)} 件)...", flush=True)
                enrich_articles_with_text(articles)
            else:
                print(
                    "  [WARN] trafilatura が未インストールのため本文取得をスキップ。"
                    " pip install trafilatura で導入できます。",
                    flush=True,
                )

        all_prompts.append(build_llm_prompt(label, articles, exclude_consensus=exclude_consensus))

    print("\n\n")
    print("=" * 64)
    print("  以下をコピーしてLLMに貼り付けてください")
    print("=" * 64)
    print()
    for p in all_prompts:
        print(p)
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="需要シグナル収集スクリプト (GDELT DOC API版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "使用例:",
            "  python demand_fetcher.py",
            "  python demand_fetcher.py --commodity gold uranium",
            "  python demand_fetcher.py --commodity all --days 7",
            "",
            f"利用可能なコモディティ: {', '.join(COMMODITIES.keys())}, all",
        ]),
    )
    parser.add_argument(
        "--commodity",
        nargs="+",
        default=["copper"],
        metavar="NAME",
        help="対象コモディティ (複数指定可, 'all'で全件)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="遡及日数 (デフォルト: 14日、最大約90日)",
    )
    parser.add_argument(
        "--no-scrape",
        action="store_true",
        help="本文取得をスキップしてタイトルのみで実行（高速化）",
    )
    parser.add_argument(
        "--exclude-consensus",
        action="store_true",
        help="コンセンサス的な長期トレンド記事をLLMプロンプトの除外対象に含める",
    )
    args = parser.parse_args()

    keys = list(COMMODITIES.keys()) if "all" in args.commodity else args.commodity

    invalid = [k for k in keys if k not in COMMODITIES]
    if invalid:
        parser.error(
            f"不明なコモディティ: {invalid}\n利用可能: {list(COMMODITIES.keys())}"
        )

    scrape = not args.no_scrape
    exclude_consensus = args.exclude_consensus
    print(f"対象コモディティ: {keys}", flush=True)
    print(f"取得期間: 過去 {args.days} 日", flush=True)
    print(f"本文取得: {'あり' if scrape else 'なし（--no-scrape）'}", flush=True)
    print(f"コンセンサス記事: {'除外' if exclude_consensus else '含める'}", flush=True)

    run(keys, args.days, scrape=scrape, exclude_consensus=exclude_consensus)


if __name__ == "__main__":
    main()
