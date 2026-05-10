import os
import time
import logging
from logging.handlers import RotatingFileHandler
import yaml
import requests
import random
from datetime import datetime, timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.exc import IntegrityError
from tenacity import retry, stop_after_attempt, wait_fixed, before_sleep_log
from dotenv import load_dotenv

from database import SessionLocal, Article, init_db

# 環境変数の読み込み
load_dotenv()

# ログの設定: ローテーション機能（最大10MB、バックアップ5ファイル）を追加
log_handler = RotatingFileHandler(
    'fetcher.log', 
    maxBytes=10 * 1024 * 1024, 
    backupCount=5, 
    encoding='utf-8'
)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[log_handler, logging.StreamHandler()] # コンソールにも出力
)
logger = logging.getLogger(__name__)

def fetch_and_store(task_name, base_url, params):
    """GDELT APIからデータを取得し、データベースに保存"""
    
    # 同時アクセス回避のためのランダムジッター
    jitter_time = random.uniform(2, 7) # 少し広めに設定
    logger.info(f"タスク '{task_name}': {jitter_time:.2f} 秒待機してリクエストを開始します...")
    time.sleep(jitter_time)

    @retry(
        stop=stop_after_attempt(3), 
        wait=wait_fixed(300), # 5分間隔
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True
    )
    def perform_request():
        response = requests.get(base_url, params=params, timeout=45) # タイムアウトを少し延長
        response.raise_for_status()
        return response.json()

    try:
        data = perform_request()
    except Exception as e:
        logger.error(f"タスク '{task_name}': 取得を断念しました。エラー: {e}")
        return

    articles = data.get("articles", [])
    if not articles:
        logger.info(f"タスク '{task_name}': 取得データは0件でした。")
        return
    
    session = SessionLocal()
    inserted_count = 0
    duplicate_count = 0

    try:
        for item in articles:
            url = item.get("url")
            if not url:
                continue
                
            seendate_str = item.get("seendate")
            publish_date = datetime.now(timezone.utc)
            if seendate_str:
                try:
                    publish_date = datetime.strptime(seendate_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    pass # デフォルトの現在時刻を使用

            new_article = Article(
                task_name=task_name,
                publish_date=publish_date,
                url=url,
                raw_data=item
            )
            
            session.add(new_article)
            try:
                session.commit()
                inserted_count += 1
            except IntegrityError:
                session.rollback()
                duplicate_count += 1
            except Exception as e:
                session.rollback()
                logger.error(f"タスク '{task_name}': DB保存エラー: {e}")
    finally:
        session.close()
        
    logger.info(f"タスク '{task_name}': 完了 (新規: {inserted_count}, 重複スキップ: {duplicate_count})")

def load_tasks(filepath):
    """YAMLファイルからタスク設定を安全に読み込み"""
    if not os.path.exists(filepath):
        logger.error(f"設定ファイルが見つかりません: {filepath}")
        return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"設定ファイルの解析エラー: {e}")
        return None

def main():
    logger.info("システム起動中...")
    init_db()

    config = load_tasks("tasks.yaml")
    if not config or "tasks" not in config:
        logger.error("有効なタスク設定がありません。")
        return

    base_url = config.get("global_settings", {}).get("base_url", "https://api.gdeltproject.org/api/v2/doc/doc")
    scheduler = BlockingScheduler()
    
    for task in config["tasks"]:
        name = task.get("name", "unnamed_task")
        interval = task.get("interval_minutes", 60)
        start_time_str = task.get("start_time", "00:00")
        
        try:
            hour, minute = map(int, start_time_str.split(":"))
            now = datetime.now()
            start_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except Exception:
            start_date = datetime.now()

        params = {
            "query": task.get("query", ""),
            "mode": "artlist",
            "format": "json",
            "timespan": task.get("timespan", "1h"),
            "maxrecords": task.get("max_records", 250),
            "sort": "datedesc"
        }
        
        scheduler.add_job(
            fetch_and_store,
            'interval',
            minutes=interval,
            start_date=start_date,
            args=[name, base_url, params],
            id=name,
            misfire_grace_time=600
        )
        logger.info(f"タスク登録: {name} (間隔: {interval}分, 基準時刻: {start_time_str})")

    try:
        logger.info("スケジューラを開始しました。")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("スケジューラを停止しました。")

if __name__ == "__main__":
    main()
