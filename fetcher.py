"""
定期収集スケジューラ - GDELT v5 quadgram ngrams版

5分ごとに catch_up() を実行し、DBに保存したカーソル（最後に処理したタイムスタンプ）
から現在時刻(UTC)-5分まで、1分刻みで全タイムスタンプをプローブして
ngrams/tocペアをダウンロード・照合・保存する。
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError

from database import Article, FetchState, SessionLocal, init_db
from ngrams_fetcher import (
    TARGETS,
    download_ngrams_pair,
    enrich_articles_with_text,
    match_articles,
    normalize_title,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

CURSOR_KEY = "ngrams_cursor"
PUBLISH_DELAY_MINUTES = 5  # 公式推奨: 5分前のタイムスタンプまでを取得
BACKFILL_MINUTES = int(os.getenv("NGRAMS_BACKFILL_MINUTES", "60"))
MAX_RETRIES = 2
RETRY_WAIT_SECONDS = 5
FETCH_INTERVAL_MINUTES = int(os.getenv("FETCH_INTERVAL_MINUTES", "5"))


def _get_target_filter() -> dict | None:
    """TARGET_FILTER環境変数（カンマ区切りターゲットキー）が設定されていれば
    ngrams_fetcher.TARGETSを絞り込んだdictを返す。未設定ならNone（全ターゲット対象）。
    """
    raw = os.getenv("TARGET_FILTER", "").strip()
    if not raw:
        return None
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    filtered = {k: v for k, v in TARGETS.items() if k in keys}
    missing = keys - filtered.keys()
    if missing:
        logger.warning(f"TARGET_FILTERに存在しないターゲットキー: {missing}")
    return filtered


def _get_cursor_key(target_filter: dict | None) -> str:
    """TARGET_FILTERの内容ごとに独立したカーソルキーを返す。
    フィルタなし収集（全ターゲット）のカーソルとは別管理にすることで、
    複数のfetcherプロセスを異なるターゲット集合で並行運用しても
    互いのカーソル進行に影響しない。
    """
    if target_filter is None:
        return CURSOR_KEY
    suffix = ",".join(sorted(target_filter.keys()))
    return f"{CURSOR_KEY}:{suffix}"


def _get_cursor(session, cursor_key: str) -> datetime | None:
    row = session.get(FetchState, cursor_key)
    if not row:
        return None
    return datetime.strptime(row.value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _set_cursor(session, cursor_key: str, ts: datetime) -> None:
    value = ts.strftime("%Y%m%d%H%M%S")
    row = session.get(FetchState, cursor_key)
    if row:
        row.value = value
    else:
        row = FetchState(key=cursor_key, value=value)
        session.add(row)
    session.commit()


def _save_articles(articles: list[dict]) -> tuple[int, int]:
    """記事リストをURL・正規化タイトルで重複排除しDBへ保存する。"""
    if not articles:
        return 0, 0

    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[dict] = []
    for art in articles:
        url = art.get("url", "")
        title = art.get("title", "")
        if url and url in seen_urls:
            continue
        norm = normalize_title(title)
        if norm and norm in seen_titles:
            continue
        if url:
            seen_urls.add(url)
        if norm:
            seen_titles.add(norm)
        deduped.append(art)

    enrich_articles_with_text(deduped)

    session = SessionLocal()
    inserted = 0
    skipped = 0
    try:
        for art in deduped:
            url = art.get("url")
            if not url:
                continue

            try:
                publish_date = datetime.fromisoformat(
                    art.get("seendate", "").replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                publish_date = datetime.now(timezone.utc)

            raw_data = dict(art.get("raw") or {})
            raw_data["matched_query"] = art.get("matched_query")

            new_article = Article(
                target=art.get("matched_target"),
                collection_mode=art.get("matched_mode"),
                publish_date=publish_date,
                fetched_at=datetime.now(timezone.utc),
                title=art.get("title"),
                source_domain=art.get("domain"),
                url=url,
                raw_data=raw_data,
                body=art.get("body"),
            )
            session.add(new_article)
            try:
                session.commit()
                inserted += 1
            except IntegrityError:
                session.rollback()
                skipped += 1
            except Exception as e:
                session.rollback()
                logger.error(f"DB保存エラー: {e}")
    finally:
        session.close()

    return inserted, skipped


def _process_timestamp(ts: datetime, targets: dict | None = None) -> bool:
    """1タイムスタンプを処理する。成功したら True、ネットワークエラーで断念したら False。"""
    ts_str = ts.strftime("%Y%m%d%H%M%S")

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            ngrams_lines, toc = download_ngrams_pair(ts_str)
            break
        except Exception as e:
            if attempt > MAX_RETRIES:
                logger.error(f"[{ts_str}] ダウンロード失敗、リトライ上限到達: {e}")
                return False
            logger.warning(f"[{ts_str}] ダウンロードエラー、{RETRY_WAIT_SECONDS}秒後リトライ ({attempt}/{MAX_RETRIES}): {e}")
            time.sleep(RETRY_WAIT_SECONDS)

    if ngrams_lines is None or toc is None:
        logger.info(f"[{ts_str}] ファイルなし（404/空）、スキップ")
        return True

    articles = match_articles(ngrams_lines, toc, targets=targets)
    if not articles:
        logger.info(f"[{ts_str}] 一致記事なし (ngrams行数={len(ngrams_lines)}, toc件数={len(toc)})")
        return True

    inserted, skipped = _save_articles(articles)
    logger.info(
        f"[{ts_str}] 一致 {len(articles)} 件 → 保存 {inserted} 件, 重複スキップ {skipped} 件"
    )
    return True


def catch_up() -> None:
    """カーソルから現在時刻-5分まで1分刻みで追いつく。

    TARGET_FILTER環境変数が設定されている場合は指定ターゲットのみに絞り込む。
    カーソルはTARGET_FILTERの内容ごとに独立管理されるため、フィルタなし収集
    （全ターゲット）のカーソル進行には影響しない。
    """
    target_filter = _get_target_filter()
    cursor_key = _get_cursor_key(target_filter)
    now = datetime.now(timezone.utc)
    limit = (now - timedelta(minutes=PUBLISH_DELAY_MINUTES)).replace(second=0, microsecond=0)

    session = SessionLocal()
    try:
        cursor = _get_cursor(session, cursor_key)
    finally:
        session.close()

    if cursor is None:
        start = limit - timedelta(minutes=BACKFILL_MINUTES)
        logger.info(f"[{cursor_key}] カーソル未設定。{BACKFILL_MINUTES}分前から開始: {start.isoformat()}")
    else:
        start = cursor + timedelta(minutes=1)

    if start > limit:
        logger.info(f"[{cursor_key}] 追いつくべき新規タイムスタンプなし")
        return

    ts = start
    processed_count = 0
    while ts <= limit:
        ok = _process_timestamp(ts, targets=target_filter)
        if not ok:
            logger.warning(f"[{cursor_key}][{ts.strftime('%Y%m%d%H%M%S')}] で断念。次回catch_upで再開する")
            break

        session = SessionLocal()
        try:
            _set_cursor(session, cursor_key, ts)
        finally:
            session.close()

        processed_count += 1
        ts += timedelta(minutes=1)

    logger.info(f"[{cursor_key}] catch_up完了: {processed_count} 分間分を処理")


def main() -> None:
    logger.info("fetcher 起動中...")
    init_db()

    scheduler = BlockingScheduler()
    scheduler.add_job(
        catch_up,
        "interval",
        minutes=FETCH_INTERVAL_MINUTES,
        id="ngrams_catch_up",
        next_run_time=datetime.now(timezone.utc),
        misfire_grace_time=600,
        max_instances=1,
    )
    logger.info(f"タスク登録: [ngrams catch_up] ({FETCH_INTERVAL_MINUTES}分ごと)")

    try:
        logger.info("スケジューラ開始")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("スケジューラ停止")


if __name__ == "__main__":
    main()
