"""
収集ユーティリティ - GDELT v5 quadgram ngrams版

GDELT v5 weblegacy ngramsファイル（.ngrams.txt.gz / .toc.json.gz ペア）の
ダウンロード、targets/*.yaml の読込、ブール式照合エンジン、本文スクレイピング、
重複排除のロジックを提供する。
スケジューリングとDB保存は fetcher.py が担当する。
LLM分析は llm_processor.py が担当する。

ngramsファイルの仕様（実測で検証済み）:
- URL: https://storage.googleapis.com/data.gdeltproject.org/gdeltv5/weblegacy/ngrams/YYYYMMDDHHMMSS.ngrams.txt.gz
        と同ディレクトリの .toc.json.gz。タイムスタンプはUTC・秒は常に00。
- 認証・APIキー・レート制限なしの静的GCSホスティング。マスターファイルリストは存在しないため
  タイムスタンプは自前で組み立て、全分をプローブして404を許容する。
- .ngrams.txt.gz: UTF-8タブ区切り3列（ヘッダなし） DOCID \t QUADGRAM \t COUNT。
  QUADGRAMはスペース区切り最大4トークン。日本語・中国語等（scriptio continua）は
  連続4文字をスペース区切りにしたもの。句読点はトークンに付着したまま残る。
  DOCIDはファイルごとにリセットされる。
- .toc.json.gz: UTF-8改行区切りJSON（1行1記事）。ID/date/img/lang/title/url。
"""

import gzip
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests

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
# ngrams ファイル配信元
# ============================================================
NGRAMS_BASE_URL = "https://storage.googleapis.com/data.gdeltproject.org/gdeltv5/weblegacy/ngrams"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

DOWNLOAD_TIMEOUT = 30


# ============================================================
# グローバル除外フィルタ（全クエリのexcludeに自動付加）
# 英語フレーズなのでCJK文書には実質無害
# ============================================================
GLOBAL_EXCLUDE: List[str] = [
    "market report", "daily report", "trading update",
    "stock market", "stock price", "share price",
    "price today", "market wrap", "earnings report",
]

_TARGETS_DIR = Path(__file__).parent / "targets"


def _load_targets(dir_path: Path) -> Dict[str, Dict]:
    """targets/*.yaml を全て読み込み、各クエリにグローバル除外フィルタを付加して返す。"""
    if not _YAML_AVAILABLE:
        raise ImportError("pyyaml が未インストールです。pip install pyyaml で導入してください。")
    if not dir_path.exists():
        raise FileNotFoundError(f"ターゲット定義ディレクトリが見つかりません: {dir_path}")
    data: Dict[str, Dict] = {}
    for yaml_path in sorted(dir_path.glob("*.yaml")):
        with open(yaml_path, encoding="utf-8") as f:
            data.update(yaml.safe_load(f) or {})
    for target_config in data.values():
        for mode in ("monitor", "analyze"):
            mode_config = target_config.get(mode)
            if not mode_config or "queries" not in mode_config:
                continue
            for q in mode_config["queries"]:
                exclude = list(q.get("exclude", []))
                for phrase in GLOBAL_EXCLUDE:
                    if phrase not in exclude:
                        exclude.append(phrase)
                q["exclude"] = exclude
    return data


TARGETS: Dict[str, Dict] = _load_targets(_TARGETS_DIR)


# ============================================================
# ドメインブラックリスト
# 非英語言語ドメイン（旧: sourcelang=eng でも混入していたもの）は
# langフィルタで対応できるため除外。プレスリリース配信系と
# アグリゲーター系低品質ドメインのみ残す。
# ============================================================
DOMAIN_BLACKLIST: Set[str] = {
    # プレスリリース配信
    "prnewswire.com", "businesswire.com", "globenewswire.com",
    "accesswire.com", "einpresswire.com",
    # アグリゲーター系低品質
    "markets.businessinsider.com", "247wallst.com", "insidermonkey.com",
}

SCRAPE_TEXT_LIMIT = 1000
SCRAPE_TIMEOUT = 10
_SCRAPE_MAX_WORKERS = 5


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


# ============================================================
# ngrams / toc ダウンロード
# ============================================================
def _download_gz(url: str) -> Optional[bytes]:
    """URLからgzファイルを取得し展開して返す。404/0バイトはNone。ネットワークエラーは例外を上げる。"""
    resp = requests.get(url, headers=HEADERS, timeout=DOWNLOAD_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    if not resp.content:
        return None
    try:
        raw = gzip.decompress(resp.content)
    except OSError:
        # 0バイトgz等、展開できない不正データはスキップ扱い
        return None
    if not raw:
        return None
    return raw


def download_ngrams_pair(timestamp: str) -> Tuple[Optional[List[str]], Optional[Dict[int, dict]]]:
    """
    指定タイムスタンプ（YYYYMMDDHHMMSS）のngrams/tocペアをダウンロードする。
    戻り値: (ngrams行のリスト, docid→tocレコードのdict)。
    どちらか片方でも取得できなければ (None, None) を返す（404・0バイトを含む）。
    ネットワークエラーは例外を上げる（呼び出し側でリトライ判断）。
    """
    ngrams_url = f"{NGRAMS_BASE_URL}/{timestamp}.ngrams.txt.gz"
    toc_url = f"{NGRAMS_BASE_URL}/{timestamp}.toc.json.gz"

    ngrams_raw = _download_gz(ngrams_url)
    if ngrams_raw is None:
        return None, None
    toc_raw = _download_gz(toc_url)
    if toc_raw is None:
        return None, None

    ngrams_lines = ngrams_raw.decode("utf-8", errors="replace").splitlines()

    toc: Dict[int, dict] = {}
    for line in toc_raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        docid = rec.get("ID")
        if docid is None:
            continue
        toc[docid] = rec

    return ngrams_lines, toc


# ============================================================
# 照合エンジン
# ============================================================
# scriptio continua（分かち書きしない言語）。韓国語はスペース区切りのため含めない。
_CJK_LANGS = {"ja", "zh", "zh-cn", "zh-tw"}


def _strip_punct(token: str) -> str:
    """トークン前後の句読点（英数字以外）を除去して小文字化する。"""
    return re.sub(r"^[^\w]+|[^\w]+$", "", token, flags=re.UNICODE).lower()


def _is_valid_url(url: str) -> bool:
    return bool(url) and (url.startswith("http://") or url.startswith("https://"))


def _get_domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _is_blacklisted(domain: str) -> bool:
    return any(domain == b or domain.endswith("." + b) for b in DOMAIN_BLACKLIST)


class _QueryPhrases:
    """1クエリが必要とする全フレーズ（must各要素 + exclude）をまとめたもの。"""

    __slots__ = ("target_key", "mode", "must", "exclude", "raw_query")

    def __init__(self, target_key: str, mode: str, must: list, exclude: list, raw_query: dict):
        self.target_key = target_key
        self.mode = mode
        self.must = must  # list[str | list[str]]
        self.exclude = exclude  # list[str]
        self.raw_query = raw_query

    def all_phrases(self) -> Set[str]:
        phrases: Set[str] = set()
        for item in self.must:
            if isinstance(item, str):
                phrases.add(item.lower())
            else:
                for p in item:
                    phrases.add(p.lower())
        for p in self.exclude:
            phrases.add(p.lower())
        return phrases

    def evaluate(self, matched_phrases: Set[str]) -> bool:
        for item in self.must:
            if isinstance(item, str):
                if item.lower() not in matched_phrases:
                    return False
            else:
                if not any(p.lower() in matched_phrases for p in item):
                    return False
        for p in self.exclude:
            if p.lower() in matched_phrases:
                return False
        return True


def _build_lang_query_map(targets: Dict[str, Dict]) -> Dict[str, List[_QueryPhrases]]:
    """lang -> [_QueryPhrases] のマップを構築する。"""
    lang_map: Dict[str, List[_QueryPhrases]] = {}
    for target_key, config in targets.items():
        lang = config.get("lang", "en")
        for mode in ("monitor", "analyze"):
            mode_config = config.get(mode)
            if not mode_config:
                continue
            for q in mode_config.get("queries", []):
                qp = _QueryPhrases(
                    target_key=target_key,
                    mode=mode,
                    must=q.get("must", []),
                    exclude=q.get("exclude", []),
                    raw_query=q,
                )
                lang_map.setdefault(lang, []).append(qp)
    return lang_map


class _EnQuadgramMatcher:
    """スペース区切り言語用の照合器。フレーズ全集合を1つの正規表現にコンパイルする。"""

    def __init__(self, phrases: Set[str]):
        self._phrase_list = sorted(phrases, key=len, reverse=True)
        if self._phrase_list:
            pattern = "|".join(re.escape(p) for p in self._phrase_list)
            self._regex = re.compile(r"\b(?:" + pattern + r")\b")
        else:
            self._regex = None

    def match_quadgram(self, quadgram: str) -> Set[str]:
        """quadgram（正規化済みトークン列をスペースjoinした文字列）中に現れるフレーズ集合を返す。"""
        if not self._regex:
            return set()
        return set(self._regex.findall(quadgram))

    @staticmethod
    def normalize_quadgram(raw_quadgram: str) -> str:
        tokens = raw_quadgram.split(" ")
        norm_tokens = [_strip_punct(t) for t in tokens]
        norm_tokens = [t for t in norm_tokens if t]
        return " ".join(norm_tokens)


class _CjkPhraseMatcher:
    """CJK言語用の照合器。4文字以下は部分文字列一致、5文字以上は4文字窓の全一致。"""

    def __init__(self, phrases: Set[str]):
        self.short_phrases = {p for p in phrases if len(p) <= 4}
        self.long_phrase_windows: Dict[str, List[str]] = {}
        for p in phrases:
            if len(p) > 4:
                windows = [p[i:i + 4] for i in range(len(p) - 3)]
                self.long_phrase_windows[p] = windows

    def has_long_phrases(self) -> bool:
        return bool(self.long_phrase_windows)


def match_articles(
    ngrams_lines: List[str],
    toc: Dict[int, dict],
    targets: Optional[Dict[str, Dict]] = None,
) -> List[Dict]:
    """
    ngrams行ストリーム＋tocをtargetsのクエリ群と照合し、一致記事のリストを返す。
    各要素: {url, title, seendate, domain, lang, matched_target, matched_mode,
             matched_query, raw}
    """
    if targets is None:
        targets = TARGETS

    lang_query_map = _build_lang_query_map(targets)

    # lang -> 対象docidの候補集合（tocフィルタ済み）
    lang_docids: Dict[str, Set[int]] = {lang: set() for lang in lang_query_map}
    docid_meta: Dict[int, dict] = {}

    for docid, rec in toc.items():
        lang = rec.get("lang")
        if lang not in lang_query_map:
            continue
        url = rec.get("url", "")
        if not _is_valid_url(url):
            continue
        domain = _get_domain(url)
        if _is_blacklisted(domain):
            continue
        lang_docids[lang].add(docid)
        docid_meta[docid] = rec

    if not any(lang_docids.values()):
        return []

    # 言語ごとのフレーズ全集合
    lang_phrases: Dict[str, Set[str]] = {
        lang: set().union(*(qp.all_phrases() for qp in qps)) if qps else set()
        for lang, qps in lang_query_map.items()
    }

    en_like_langs = {lang for lang in lang_query_map if lang not in _CJK_LANGS}
    cjk_langs = {lang for lang in lang_query_map if lang in _CJK_LANGS}

    en_matchers: Dict[str, _EnQuadgramMatcher] = {
        lang: _EnQuadgramMatcher(lang_phrases[lang]) for lang in en_like_langs
    }
    cjk_matchers: Dict[str, _CjkPhraseMatcher] = {
        lang: _CjkPhraseMatcher(lang_phrases[lang]) for lang in cjk_langs
    }

    # docid -> lang （候補docidのみ）
    docid_lang: Dict[int, str] = {}
    for lang, docids in lang_docids.items():
        for d in docids:
            docid_lang[d] = lang

    # docid -> マークされたフレーズ集合
    docid_matched_phrases: Dict[int, Set[str]] = {d: set() for d in docid_lang}
    # CJK長尺フレーズ用: docid -> phrase -> 観測済み窓集合
    docid_long_windows: Dict[int, Dict[str, Set[str]]] = {}

    for line in ngrams_lines:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        docid_str, quadgram, _count = parts
        try:
            docid = int(docid_str)
        except ValueError:
            continue
        lang = docid_lang.get(docid)
        if lang is None:
            continue

        if lang in cjk_langs:
            matcher = cjk_matchers[lang]
            joined = quadgram.replace(" ", "")
            if not joined:
                continue
            # 短いフレーズ: 部分文字列一致
            for p in matcher.short_phrases:
                if p in joined:
                    docid_matched_phrases[docid].add(p)
            # 長いフレーズ: 窓一致を記録
            if matcher.has_long_phrases():
                buckets = docid_long_windows.setdefault(docid, {})
                for p, windows in matcher.long_phrase_windows.items():
                    for w in windows:
                        if w in joined:
                            buckets.setdefault(p, set()).add(w)
        else:
            matcher = en_matchers[lang]
            norm = _EnQuadgramMatcher.normalize_quadgram(quadgram)
            if not norm:
                continue
            found = matcher.match_quadgram(norm)
            if found:
                docid_matched_phrases[docid].update(found)

    # 長尺CJKフレーズ: 全窓が観測されたもののみ確定
    for docid, buckets in docid_long_windows.items():
        matcher = cjk_matchers[docid_lang[docid]]
        for p, seen_windows in buckets.items():
            required = set(matcher.long_phrase_windows[p])
            if required.issubset(seen_windows):
                docid_matched_phrases[docid].add(p)

    results: List[Dict] = []
    for docid, matched_phrases in docid_matched_phrases.items():
        if not matched_phrases:
            continue
        lang = docid_lang[docid]
        rec = docid_meta[docid]
        qps = lang_query_map[lang]

        # ターゲット×モードごとに最良の1件（analyze優先）を選ぶ
        best: Dict[str, Tuple[str, dict]] = {}  # target_key -> (mode, raw_query)
        for qp in qps:
            if not qp.evaluate(matched_phrases):
                continue
            existing = best.get(qp.target_key)
            if existing is None or (existing[0] == "monitor" and qp.mode == "analyze"):
                best[qp.target_key] = (qp.mode, qp.raw_query)

        for target_key, (mode, raw_query) in best.items():
            url = rec.get("url", "")
            results.append({
                "url": url,
                "title": rec.get("title", ""),
                "seendate": rec.get("date", ""),
                "domain": _get_domain(url),
                "lang": lang,
                "matched_target": target_key,
                "matched_mode": mode,
                "matched_query": json.dumps(raw_query, ensure_ascii=False),
                "raw": rec,
            })

    return results
