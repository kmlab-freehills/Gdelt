"""
収集ユーティリティ - GDELT DOC API版

GDELTへのリクエスト、本文スクレイピング、重複排除のロジックを提供する。
スケジューリングとDB保存は fetcher.py が担当する。
LLM分析は llm_processor.py が担当する。
"""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests

# GDELTは「1リクエスト/5秒」制限。複数スレッドが同時に呼び出す場合も
# このロックで直列化し、インターバルを強制する。
_GDELT_LOCK = threading.Lock()
_GDELT_LAST_REQUEST_TIME: float = 0.0
_GDELT_MIN_INTERVAL = 6.0  # 5秒制限 + 1秒バッファ

# サーキットブレーカー: 連続429が一定回数に達したら一定時間全リクエストを停止する。
# リトライのたびにGDELTを叩き続けてBANを延長するのを防ぐ。
_CIRCUIT_OPEN_UNTIL: float = 0.0        # この時刻までリクエスト停止
_CIRCUIT_FAIL_COUNT: int = 0            # 連続429カウント
_CIRCUIT_THRESHOLD: int = 5            # この回数連続429でサーキット開放
_CIRCUIT_COOLDOWN: float = 3600.0      # サーキット開放後の待機秒数（1時間）

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

try:
    import trafilatura
    _TRAFILATURA_AVAILABLE = True
except ImportError:
    _TRAFILATURA_AVAILABLE = False


# ============================================================
# ノイズフィルタ（全クエリに自動付加）
# ============================================================
_NF = (
    ' -"market report" -"daily report" -"trading update"'
    ' -"stock market" -"stock price" -"share price"'
    ' -"price today" -"market wrap" -"earnings report"'
)

_YAML_PATH = Path(__file__).parent / "targets" / "commodities.yaml"


def _load_targets(path: Path) -> Dict[str, Dict]:
    if not _YAML_AVAILABLE:
        raise ImportError("pyyaml が未インストールです。pip install pyyaml で導入してください。")
    if not path.exists():
        raise FileNotFoundError(f"ターゲット定義ファイルが見つかりません: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # 各モードの全クエリにノイズフィルタを付加
    for target_config in data.values():
        for mode in ("monitor", "analyze"):
            mode_config = target_config.get(mode)
            if mode_config and "queries" in mode_config:
                mode_config["queries"] = [
                    f"{q}{_NF}" for q in mode_config["queries"]
                ]
    return data


TARGETS: Dict[str, Dict] = _load_targets(_YAML_PATH)


# ============================================================
# ドメインブラックリスト
# sourcelang=eng 指定でも混入する非英語・低品質ドメインを除外
# ============================================================
DOMAIN_BLACKLIST: Set[str] = {
    # 中国系
    "cnfol.com", "eastmoney.com", "sina.com.cn", "hexun.com",
    "10jqka.com.cn", "cls.cn", "yicai.com", "caixin.com", "jrj.com.cn",
    "qq.com", "china.com", "163.com", "sohu.com", "ifeng.com",
    "wenxuecity.com", "eeo.com.cn", "kr.xinhuanet.com", "xinhuanet.com",
    # 韓国系
    "insight.co.kr", "hankyung.com", "mk.co.kr", "edaily.co.kr",
    # アラビア語・バングラデシュ・その他非英語
    "masrawy.com", "youm7.com", "prothomalo.com", "banglatribune.com",
    # スペイン語・ポルトガル語系
    "infobae.com", "clarin.com", "globo.com",
    # プレスリリース配信
    "prnewswire.com", "businesswire.com", "globenewswire.com",
    "accesswire.com", "einpresswire.com",
    # アグリゲーター系低品質
    "markets.businessinsider.com", "247wallst.com", "insidermonkey.com",
    # ベトナム系
    "baomoi.com", "vietgiaitri.com", "afamily.vn", "voh.com.vn",
    "vnexpress.net", "tuoitre.vn", "thanhnien.vn", "dantri.com.vn",
    "kenh14.vn", "cafef.vn", "tienphong.vn",
}

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
SLEEP_BETWEEN_QUERIES = 12.0
MAX_RETRIES = 3

SCRAPE_TEXT_LIMIT = 1000
SCRAPE_TIMEOUT = 10
_SCRAPE_MAX_WORKERS = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

_DATE_PATTERNS = [
    "%Y%m%dT%H%M%SZ",
    "%Y%m%dT%H%M%S",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
]


def parse_gdelt_date(raw: str) -> Optional[str]:
    """GDELTの日付文字列を YYYY-MM-DD に変換。失敗時は None を返す。"""
    if not raw:
        return None
    cleaned = raw.strip()
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    if re.match(r"^\d{8}", cleaned):
        try:
            return datetime.strptime(cleaned[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def normalize_title(title: str) -> str:
    """小文字化・空白正規化（重複排除用）。"""
    return re.sub(r"\s+", " ", title.lower().strip())


def scrape_article_text(url: str) -> Optional[str]:
    """trafilatura で記事本文を取得し SCRAPE_TEXT_LIMIT 文字でトリミングして返す。"""
    if not _TRAFILATURA_AVAILABLE or not url:
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=SCRAPE_TIMEOUT)
        resp.raise_for_status()
        text = trafilatura.extract(
            resp.text,
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
    """articles リストに 'body' キーを並列で追加する（破壊的更新）。"""
    total = len(articles)

    def _fetch(item):
        idx, art = item
        art["body"] = scrape_article_text(art.get("url", ""))
        return idx, art

    with ThreadPoolExecutor(max_workers=_SCRAPE_MAX_WORKERS) as executor:
        futures = {
            executor.submit(_fetch, (i, art)): i
            for i, art in enumerate(articles, 1)
        }
        for future in as_completed(futures):
            i, art = future.result()
            domain = art.get("domain", "")
            status = f"{len(art['body'])} 文字" if art["body"] else "取得失敗"
            print(f"  [{i}/{total}] {domain}: {status}", flush=True)


def fetch_articles(query: str, timespan: str = "1d", max_records: int = 10) -> List[Dict]:
    """GDELT DOC API から記事一覧を取得する。"""
    global _GDELT_LAST_REQUEST_TIME, _CIRCUIT_OPEN_UNTIL, _CIRCUIT_FAIL_COUNT

    # サーキットブレーカー確認: 開放中なら即座に諦める
    if time.time() < _CIRCUIT_OPEN_UNTIL:
        remaining = int(_CIRCUIT_OPEN_UNTIL - time.time())
        print(f"  [CIRCUIT OPEN] あと{remaining}秒待機中。GDELTへのリクエストをスキップ", flush=True)
        return []

    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": max_records,
        "timespan": timespan,
        "format": "json",
        "sourcelang": "eng",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # 複数スレッドからの同時呼び出しを直列化し5秒制限を強制する
            with _GDELT_LOCK:
                elapsed = time.time() - _GDELT_LAST_REQUEST_TIME
                if elapsed < _GDELT_MIN_INTERVAL:
                    time.sleep(_GDELT_MIN_INTERVAL - elapsed)
                _GDELT_LAST_REQUEST_TIME = time.time()

            resp = requests.get(GDELT_DOC_API, params=params, headers=HEADERS, timeout=20)

            if resp.status_code == 429:
                _CIRCUIT_FAIL_COUNT += 1
                msg = resp.text[:200].strip()
                if _CIRCUIT_FAIL_COUNT >= _CIRCUIT_THRESHOLD:
                    _CIRCUIT_OPEN_UNTIL = time.time() + _CIRCUIT_COOLDOWN
                    _CIRCUIT_FAIL_COUNT = 0
                    print(
                        f"  [CIRCUIT OPEN] 連続429が{_CIRCUIT_THRESHOLD}回に達した。"
                        f"{int(_CIRCUIT_COOLDOWN / 60)}分間リクエストを停止する",
                        flush=True,
                    )
                    return []
                wait = int(resp.headers.get("Retry-After", min(60 * attempt, 300)))
                print(f"  [429] {wait}秒待機後リトライ ({attempt}/{MAX_RETRIES}) | {msg}", flush=True)
                time.sleep(wait)
                continue

            # 成功: 連続失敗カウントをリセット
            _CIRCUIT_FAIL_COUNT = 0
            resp.raise_for_status()
            return resp.json().get("articles", []) or []

        except requests.exceptions.HTTPError as e:
            print(f"  [HTTP ERROR] {e} (試行 {attempt}/{MAX_RETRIES})", flush=True)
            if attempt < MAX_RETRIES:
                time.sleep(min(30 * (2 ** (attempt - 1)), 300))
        except requests.exceptions.RequestException as e:
            print(f"  [ERROR] {e} (試行 {attempt}/{MAX_RETRIES})", flush=True)
            if attempt < MAX_RETRIES:
                time.sleep(min(30 * (2 ** (attempt - 1)), 300))
        except (json.JSONDecodeError, ValueError):
            snippet = resp.text[:300] if "resp" in dir() else "(no response)"
            print(f"  [ERROR] JSONパース失敗: {snippet}", flush=True)
            return []

    return []
