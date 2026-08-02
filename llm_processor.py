"""
LLM分析スケジューラ

DB内の未処理記事に対して記事単位でLLM分析を実行し、llm_analysis カラムに保存する。
収集モードに関わらず全記事に同一のフル分析スキーマを適用する。
event_date は独立カラムにのみ保存する（時系列SQLクエリ用）。llm_analysis には含めない。
"""

import json
import logging
import os
import re
import time
from datetime import date

from google import genai
from google.genai import types
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from database import Article, SessionLocal, init_db
from ngrams_fetcher import TARGETS

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

CONSTRUCTION_TARGETS = {"large_scale_construction", "large_scale_construction_ja"}

LLM_INTERVAL_HOURS = int(os.getenv("LLM_INTERVAL_HOURS", "6"))
LLM_INTERVAL_MINUTES = int(os.getenv("LLM_INTERVAL_MINUTES", "0"))  # 設定時はLLM_INTERVAL_HOURSより優先
LLM_BATCH_LIMIT = int(os.getenv("LLM_BATCH_LIMIT", "20"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def _get_target_filter() -> set[str] | None:
    """LLM_TARGET_FILTER環境変数（カンマ区切りターゲットキー）が設定されていれば
    その集合を返す。未設定ならNone（全ターゲット対象）。
    """
    raw = os.getenv("LLM_TARGET_FILTER", "").strip()
    if not raw:
        return None
    return {k.strip() for k in raw.split(",") if k.strip()}


# ============================================================
# LLM バックエンド
# ============================================================
class _GeminiBackend:
    REQUEST_INTERVAL = 4

    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY が .env にありません")
        self.client = genai.Client(api_key=api_key)
        self.config = types.GenerateContentConfig(
            temperature=0.01,
            max_output_tokens=4096,
            response_mime_type="application/json",
        )
        logger.info(f"Gemini ({GEMINI_MODEL}) 初期化完了")

    def call(self, prompt: str) -> str:
        MAX_RETRIES = 3
        delay = self.REQUEST_INTERVAL
        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=self.config,
                )
                return response.text
            except Exception as e:
                if "429" in str(e) or "quota" in str(e).lower():
                    if attempt < MAX_RETRIES - 1:
                        logger.warning(f"Gemini rate limit。{delay}秒後にリトライ ({attempt+1}/{MAX_RETRIES})")
                        time.sleep(delay)
                        delay *= 2
                        continue
                raise


def _get_llm_backend():
    if os.getenv("GEMINI_API_KEY"):
        return _GeminiBackend()
    logger.warning("GEMINI_API_KEY 未設定。LLM処理をスキップします。")
    return None


def _parse_json(raw: str) -> dict | None:
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1:
            clean = clean[start:end + 1]
            clean = re.sub(r",\s*([}\]])", r"\1", clean)
            return json.loads(clean)
    except Exception as e:
        logger.error(f"JSONパース失敗: {e} | raw={raw[:200]}")
    return None


# ============================================================
# プロンプト生成（記事1件単位）
# ============================================================
def _build_prompt(target_label: str, title: str, domain: str, publish_date: str, body: str, is_construction: bool = False) -> str:
    body_text = body.strip() if body else "(本文取得不可 - タイトルのみで判断)"

    construction_fields = ""
    construction_guide = ""
    if is_construction:
        construction_fields = """,
  "construction_status": <"planned", "groundbreaking", "under_construction", "halted", "cancelled", "resumed", "topped_out", "completed", or "unknown">,
  "building_name": "<name of the building/tower/project, or null>",
  "location": {
    "city": "<city, or null>",
    "country": "<country, or null>",
    "address": "<street address if mentioned, or null>"
  }"""
        construction_guide = """
construction_status: the current status of the construction project as described in the article (planned=announced but not started, groundbreaking=ceremony/start event, under_construction=actively building, halted=temporarily stopped, cancelled=project scrapped, resumed=restarted after a halt, topped_out=structure reached final height, completed=finished/opened, unknown=status unclear)
building_name/location: best-effort extraction to help cross-reference the project against satellite imagery later; null when not stated in the article
"""

    return f"""You are analyzing a news article as a demand signal for {target_label}.

Article:
- Title: {title}
- Source: {domain}
- Published: {publish_date}
- Body: {body_text}

Return ONLY valid JSON with this exact structure:
{{
  "rating": <1, 2, or 3, or null if excluded>,
  "excluded": <true or false>,
  "tone": <"bullish", "bearish", or "neutral">,
  "tone_score": <-100.0 to +100.0, overall article sentiment, positive=positive coverage, negative=negative coverage>,
  "reason": "<1-2 sentence evaluation in Japanese>",
  "why_notable": "<why this breaks consensus, in Japanese, or null if rating < 2>",
  "event_date": "<YYYY-MM-DD of the actual event described, or null if unclear>",
  "causal": {{
    "trigger": "<what caused this demand change, or null>",
    "mechanism": "<how it propagates through supply/demand, or null>",
    "effect": "<the demand impact, or null>",
    "timeframe": "<immediate/short/medium/long, or null>"
  }},
  "drivers": ["<demand driver 1>", "<demand driver 2>"]{construction_fields}
}}

Rating guide:
- 3: Deviation from consensus (unexpected demand surge, forecast beat, new policy, supply shock)
- 2: Secondary demand effect (supply chain bottleneck, substitute shift, infrastructure strain)
- 1: Known trend reconfirmation (no new specific fact or figure)
- null + excluded=true: Irrelevant, duplicate, market summary, or unrelated to {target_label} demand

tone: bullish=demand increase signal, bearish=demand decrease signal, neutral=mixed/unclear
tone_score: overall article sentiment aligned with GDELT V2Tone scale (-100=very negative, 0=neutral, +100=very positive), independent of demand direction
event_date: the date the described event actually occurred (not the article publication date)
causal fields: null is acceptable when the article does not contain enough information
{construction_guide}"""


# ============================================================
# バッチ処理（全ターゲットの未処理記事を一括処理）
# ============================================================
def run_all(backend, target_keys: set[str] | None = None) -> None:
    session = SessionLocal()
    try:
        query = session.query(Article).filter(Article.is_llm_processed == False)
        if target_keys:
            query = query.filter(Article.target.in_(target_keys))
        rows = query.order_by(Article.publish_date.desc()).limit(LLM_BATCH_LIMIT).all()
    finally:
        session.close()

    if not rows:
        logger.info("未処理記事なし")
        return

    logger.info(f"{len(rows)} 件のLLM処理開始")

    target_labels = {k: v["label"] for k, v in TARGETS.items()}
    request_interval = getattr(backend, "REQUEST_INTERVAL", 1)

    processed = 0
    failed = 0

    for row in rows:
        target_label = target_labels.get(row.target, row.target)
        title = row.title or row.raw_data.get("title", "")
        domain = row.source_domain or row.raw_data.get("domain", "")
        publish_date = row.publish_date.strftime("%Y-%m-%d") if row.publish_date else ""

        is_construction = row.target in CONSTRUCTION_TARGETS
        prompt = _build_prompt(target_label, title, domain, publish_date, row.body or "", is_construction)

        try:
            raw_response = backend.call(prompt)
        except Exception as e:
            logger.error(f"[article_id={row.id}] LLM呼び出し失敗: {e}")
            failed += 1
            continue

        result = _parse_json(raw_response)
        if not result:
            logger.error(f"[article_id={row.id}] JSONパース失敗")
            failed += 1
            continue

        # event_date を独立カラムに昇格（時系列SQL検索用）
        event_date_str = result.get("event_date")
        if event_date_str:
            try:
                row.event_date = date.fromisoformat(event_date_str)
            except ValueError:
                row.event_date = None
        else:
            row.event_date = None

        row.llm_analysis = {
            "rating":       result.get("rating"),
            "excluded":     result.get("excluded", False),
            "tone":         result.get("tone"),
            "tone_score":   result.get("tone_score"),
            "reason":       result.get("reason"),
            "why_notable":  result.get("why_notable"),
            "causal":       result.get("causal"),
            "drivers":      result.get("drivers", []),
        }
        if is_construction:
            row.llm_analysis["construction"] = {
                "status":        result.get("construction_status"),
                "building_name": result.get("building_name"),
                "location":      result.get("location"),
            }
        row.is_llm_processed = True

        session = SessionLocal()
        try:
            session.add(row)
            session.commit()
            processed += 1
            extra_log = f" construction_status={result.get('construction_status')}" if is_construction else ""
            logger.info(
                f"[{row.target}/{row.collection_mode}] id={row.id} "
                f"rating={result.get('rating')} tone={result.get('tone')} "
                f"event_date={event_date_str}{extra_log}"
            )
        except Exception as e:
            session.rollback()
            logger.error(f"[article_id={row.id}] DB保存エラー: {e}")
            failed += 1
        finally:
            session.close()

        time.sleep(request_interval)

    logger.info(f"LLM処理完了: 成功 {processed} 件, 失敗 {failed} 件")


# ============================================================
# スケジューラ
# ============================================================
def main() -> None:
    logger.info("llm_processor 起動中...")
    init_db()

    backend = _get_llm_backend()
    if not backend:
        logger.error("LLMバックエンドが利用できません。終了します。")
        return

    interval_kwargs = (
        {"minutes": LLM_INTERVAL_MINUTES} if LLM_INTERVAL_MINUTES > 0 else {"hours": LLM_INTERVAL_HOURS}
    )

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_all,
        "interval",
        args=[backend, _get_target_filter()],
        id="llm_process_all",
        misfire_grace_time=600,
        **interval_kwargs,
    )
    interval_desc = f"{LLM_INTERVAL_MINUTES}分ごと" if LLM_INTERVAL_MINUTES > 0 else f"{LLM_INTERVAL_HOURS}時間ごと"
    logger.info(f"LLM処理スケジュール登録 ({interval_desc})")

    try:
        logger.info("スケジューラ開始")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("スケジューラ停止")


if __name__ == "__main__":
    main()
