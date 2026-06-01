"""
LLM処理スケジューラ
DB内の未処理記事をコモディティごとにバッチ処理し、llm_analysis を保存する。
LLM バックエンドは GEMINI_API_KEY があれば Gemini、なければスタブ（将来ローカルLLMに差し替え）。
"""

import json
import logging
import os
import time

from google import genai
from google.genai import types
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from database import Article, SessionLocal, init_db
from demand_fetcher import COMMODITIES, build_llm_prompt

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

LLM_INTERVAL_HOURS = int(os.getenv("LLM_INTERVAL_HOURS", "6"))
LLM_BATCH_LIMIT = int(os.getenv("LLM_BATCH_LIMIT", "20"))  # 1回に処理する最大記事数/コモディティ
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


# ============================================================
# LLM バックエンド
# ============================================================
class _GeminiBackend:
    REQUEST_INTERVAL = 4
    MAX_RETRIES = 3

    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY が .env にありません")
        self.client = genai.Client(api_key=api_key)
        self.config = types.GenerateContentConfig(
            temperature=0.01,
            max_output_tokens=8192,
            response_mime_type="application/json",
        )
        logger.info(f"Gemini ({GEMINI_MODEL}) 初期化完了")

    def call(self, prompt: str) -> str:
        delay = self.REQUEST_INTERVAL
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self.client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=self.config,
                )
                return response.text
            except Exception as e:
                if "429" in str(e) or "quota" in str(e).lower():
                    if attempt < self.MAX_RETRIES - 1:
                        logger.warning(f"Gemini rate limit。{delay}秒後にリトライ ({attempt+1}/{self.MAX_RETRIES})")
                        time.sleep(delay)
                        delay *= 2
                        continue
                raise


def _get_llm_backend():
    """GEMINI_API_KEY があれば Gemini、なければ None（スタブ）。"""
    if os.getenv("GEMINI_API_KEY"):
        return _GeminiBackend()
    logger.warning("GEMINI_API_KEY 未設定。LLM処理をスキップします。")
    return None


def _parse_json(raw: str) -> dict | None:
    import re
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1:
            clean = clean[start: end + 1]
            # 末尾カンマを除去（Geminiが出力する不正JSONに対応）
            clean = re.sub(r",\s*([}\]])", r"\1", clean)
            return json.loads(clean)
    except Exception as e:
        logger.error(f"JSONパース失敗: {e} | raw={raw[:200]}")
    return None


# ============================================================
# コモディティごとの処理
# ============================================================
def process_commodity(commodity_key: str, backend) -> None:
    config = COMMODITIES.get(commodity_key)
    if not config:
        return

    label = config["label"]
    session = SessionLocal()
    try:
        rows = (
            session.query(Article)
            .filter(
                Article.task_name == commodity_key,
                Article.is_llm_processed == False,
            )
            .order_by(Article.publish_date.desc())
            .limit(LLM_BATCH_LIMIT)
            .all()
        )
    finally:
        session.close()

    if not rows:
        logger.info(f"[{label}] 未処理記事なし")
        return

    logger.info(f"[{label}] {len(rows)} 件を LLM 処理開始")

    # build_llm_prompt が期待する形式に変換
    articles = []
    for row in rows:
        art = dict(row.raw_data)
        art["body"] = row.body
        art["_db_id"] = row.id
        articles.append(art)

    prompt = build_llm_prompt(label, articles)

    try:
        raw_response = backend.call(prompt)
    except Exception as e:
        logger.error(f"[{label}] LLM呼び出し失敗: {e}")
        return

    analysis = _parse_json(raw_response)
    if not analysis:
        logger.error(f"[{label}] レスポンスのJSONパース失敗")
        return

    # 各記事に analysis を紐づけて保存（LLMのid=1始まりとrowsのインデックスが対応）
    article_results = {a["id"]: a for a in analysis.get("articles", [])}
    drivers = analysis.get("drivers", [])
    top3 = analysis.get("top3", [])

    session = SessionLocal()
    try:
        for i, row in enumerate(rows):
            ar = article_results.get(i + 1, {})
            row.llm_analysis = {
                "rating": ar.get("rating"),
                "excluded": ar.get("excluded", False),
                "reason": ar.get("reason"),
                "drivers": drivers,
                "top3": top3,
            }
            row.is_llm_processed = True
        session.add_all(rows)
        session.commit()
        logger.info(f"[{label}] {len(rows)} 件の llm_analysis を保存しました")
    except Exception as e:
        session.rollback()
        logger.error(f"[{label}] DB保存エラー: {e}")
    finally:
        session.close()


# ============================================================
# スケジューラ
# ============================================================
def run_all(backend) -> None:
    for key in COMMODITIES:
        process_commodity(key, backend)


def main() -> None:
    logger.info("llm_processor 起動中...")
    init_db()

    backend = _get_llm_backend()
    if not backend:
        logger.error("LLMバックエンドが利用できません。終了します。")
        return

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_all,
        "interval",
        hours=LLM_INTERVAL_HOURS,
        args=[backend],
        id="llm_process_all",
        misfire_grace_time=600,
    )
    logger.info(f"LLM処理スケジュール登録 (間隔: {LLM_INTERVAL_HOURS}時間ごと)")

    try:
        logger.info("スケジューラ開始")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("スケジューラ停止")


if __name__ == "__main__":
    main()
