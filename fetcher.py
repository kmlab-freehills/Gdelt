"""
定期収集スケジューラ
commodities.yaml の各コモディティを定期的に GDELT DOC API から収集し DB に保存する。
"""

import logging
import os
import random
import time
from datetime import datetime, timezone

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_fixed

from database import Article, SessionLocal, init_db
from demand_fetcher import (
    COMMODITIES,
    DOMAIN_BLACKLIST,
    SLEEP_BETWEEN_QUERIES,
    enrich_articles_with_text,
    fetch_articles,
    normalize_title,
    parse_gdelt_date,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

FETCH_INTERVAL_HOURS = int(os.getenv("FETCH_INTERVAL_HOURS", "6"))
FETCH_DAYS = int(os.getenv("FETCH_DAYS", "1"))  # 収集対象期間（日）


def fetch_and_store_commodity(commodity_key: str) -> None:
    config = COMMODITIES.get(commodity_key)
    if not config:
        logger.error(f"不明なコモディティ: {commodity_key}")
        return

    label = config["label"]

    # 同時アクセス回避のランダムジッター
    jitter = random.uniform(2, 7)
    logger.info(f"[{label}] {jitter:.1f}秒待機後にリクエスト開始")
    time.sleep(jitter)

    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    articles: list[dict] = []

    for q in config["queries"]:
        logger.info(f"[{label}] クエリ: {q}")

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_fixed(300),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        def _fetch_with_retry(query=q):
            return fetch_articles(query, days=FETCH_DAYS)

        try:
            results = _fetch_with_retry()
        except Exception as e:
            logger.error(f"[{label}] クエリ取得断念: {e}")
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

        logger.info(f"[{label}] クエリ完了: 新規 {new_count} 件 (累計 {len(articles)} 件)")
        time.sleep(SLEEP_BETWEEN_QUERIES)

    if not articles:
        logger.info(f"[{label}] 取得記事なし")
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
                task_name=commodity_key,
                publish_date=publish_date,
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
                logger.error(f"[{label}] DB保存エラー: {e}")
    finally:
        session.close()

    logger.info(f"[{label}] 保存完了 (新規: {inserted}, 重複スキップ: {skipped})")


def main() -> None:
    logger.info("fetcher 起動中...")
    init_db()

    scheduler = BlockingScheduler()

    for key in COMMODITIES:
        scheduler.add_job(
            fetch_and_store_commodity,
            "interval",
            hours=FETCH_INTERVAL_HOURS,
            args=[key],
            id=f"fetch_{key}",
            misfire_grace_time=600,
        )
        logger.info(f"タスク登録: {key} (間隔: {FETCH_INTERVAL_HOURS}時間ごと)")

    try:
        logger.info("スケジューラ開始")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("スケジューラ停止")


if __name__ == "__main__":
    main()
