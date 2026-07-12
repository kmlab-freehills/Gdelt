"""
定期収集スケジューラ

targets/commodities.yaml の各ターゲットについて
Monitor（速報性重視・広域）と Analyze（需要シグナル特化）の収集ジョブを
APScheduler で管理する。どちらか一方のみ定義されたターゲットも正しく動作する。
"""

import logging
import os
import random
import time
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_fixed

from database import Article, SessionLocal, init_db
from demand_fetcher import (
    DOMAIN_BLACKLIST,
    SLEEP_BETWEEN_QUERIES,
    TARGETS,
    enrich_articles_with_text,
    fetch_articles,
    normalize_title,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def fetch_and_store(target_key: str, mode: str) -> None:
    """
    指定ターゲットを指定モード（monitor / analyze）で収集してDBに保存する。
    YAMLにそのモードが定義されていなければ何もしない。
    """
    config = TARGETS.get(target_key)
    if not config:
        logger.error(f"不明なターゲット: {target_key}")
        return

    mode_config = config.get(mode)
    if not mode_config:
        return

    label = config["label"]
    queries = mode_config["queries"]
    timespan = mode_config["timespan"]
    max_records = mode_config["max_records"]
    sourcelang = config.get("sourcelang", "eng")
    fetched_at = datetime.now(timezone.utc)

    jitter = random.uniform(2, 7)
    logger.info(f"[{label}/{mode}] {jitter:.1f}秒待機後に収集開始 (timespan={timespan}, max={max_records})")
    time.sleep(jitter)

    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    articles: list[dict] = []

    for q in queries:
        logger.info(f"[{label}/{mode}] クエリ: {q[:80]}...")

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_fixed(300),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        def _fetch_with_retry(query=q):
            return fetch_articles(query, timespan=timespan, max_records=max_records, sourcelang=sourcelang)

        try:
            results = _fetch_with_retry()
        except Exception as e:
            logger.error(f"[{label}/{mode}] クエリ取得断念: {e}")
            time.sleep(SLEEP_BETWEEN_QUERIES)
            continue

        new_count = 0
        for art in results:
            url = art.get("url", "")
            domain = art.get("domain", "")
            title = art.get("title", "")

            if any(domain == b or domain.endswith("." + b) for b in DOMAIN_BLACKLIST):
                continue
            if url and url in seen_urls:
                continue
            norm = normalize_title(title)
            if norm and norm in seen_titles:
                continue

            if url:
                seen_urls.add(url)
            if norm:
                seen_titles.add(norm)
            articles.append(art)
            new_count += 1

        logger.info(f"[{label}/{mode}] クエリ完了: 新規 {new_count} 件 (累計 {len(articles)} 件)")
        time.sleep(SLEEP_BETWEEN_QUERIES)

    if not articles:
        logger.info(f"[{label}/{mode}] 取得記事なし")
        return

    enrich_articles_with_text(articles)

    session = SessionLocal()
    inserted = 0
    skipped = 0
    try:
        for art in articles:
            url = art.get("url")
            if not url:
                continue

            seendate_str = art.get("seendate", "")
            try:
                publish_date = datetime.strptime(seendate_str, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            except (ValueError, TypeError):
                publish_date = datetime.now(timezone.utc)

            new_article = Article(
                target=target_key,
                collection_mode=mode,
                publish_date=publish_date,
                fetched_at=fetched_at,
                title=art.get("title"),
                source_domain=art.get("domain"),
                url=url,
                raw_data=art,
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
                logger.error(f"[{label}/{mode}] DB保存エラー: {e}")
    finally:
        session.close()

    logger.info(f"[{label}/{mode}] 保存完了 (新規: {inserted}, 重複スキップ: {skipped})")


def main() -> None:
    logger.info("fetcher 起動中...")
    init_db()

    scheduler = BlockingScheduler()

    for target_key, config in TARGETS.items():
        label = config.get("label", target_key)
        for mode in ("monitor", "analyze"):
            mode_config = config.get(mode)
            if not mode_config:
                continue
            interval = mode_config["interval_minutes"]
            scheduler.add_job(
                fetch_and_store,
                "interval",
                minutes=interval,
                args=[target_key, mode],
                id=f"{mode}_{target_key}",
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=interval),
                misfire_grace_time=600,
            )
            logger.info(f"タスク登録: [{label}/{mode}] ({interval}分ごと, timespan={mode_config['timespan']})")

    try:
        logger.info("スケジューラ開始")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("スケジューラ停止")


if __name__ == "__main__":
    main()
