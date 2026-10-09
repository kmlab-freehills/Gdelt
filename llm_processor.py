"""
LLM分析スケジューラ

DB内の未処理記事に対して記事単位でLLM分析を実行し、llm_analysis カラムに保存する。
収集モードに関わらず、需要系ターゲットには需要シグナル用プロンプト、
建設ターゲットには建設案件追跡用の専用プロンプトを適用する。
event_date は検証後に独立カラムへ保存する（時系列SQLクエリ用）。検証で補正した場合のみ
LLMの元の値を llm_analysis["event_date_raw"] に残す（精度評価用）。
"""

import json
import logging
import os
import re
import time
from datetime import date, timedelta

import requests

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

# LLM_BACKEND: "ollama" / "gemini"。未設定時は従来通り GEMINI_API_KEY があれば Gemini
LLM_BACKEND = os.getenv("LLM_BACKEND", "").strip().lower()
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300"))
# ローカルLLMはレート制限がないため、1回あたりの処理件数をGemini(LLM_BATCH_LIMIT)とは別に設定する
OLLAMA_BATCH_LIMIT = int(os.getenv("OLLAMA_BATCH_LIMIT", "100"))
OLLAMA_STARTUP_WAIT_SECONDS = int(os.getenv("OLLAMA_STARTUP_WAIT_SECONDS", "300"))

# 建設ターゲットの工程（milestones）で使う工程種別・状態
MILESTONE_EVENTS = {
    "announced", "approved", "groundbreaking", "topped_out",
    "completed", "opened", "halted", "resumed", "cancelled", "other",
}
MILESTONE_STATUSES = {"actual", "planned"}
EVENT_DATE_BASES = {"explicit", "relative", "publication"}  # event_date の根拠（建設ターゲットのみ）
PROJECT_TYPES = {"building", "industrial", "infrastructure", "energy", "public_facility", "other"}
# 建設記事の主題の区分。project_progress 以外は除外する
MAIN_SUBJECTS = {
    "project_progress", "policy_or_program", "multiple_projects", "company_or_product",
    "incident_at_building", "history_or_feature", "market_or_opinion", "other",
}
# llm_analysis に記録するプロンプトのバージョン（結果の世代を判別するため）
PROMPT_VERSIONS = {"construction": "construction-v5", "demand": "demand-v4"}
# 需要プロンプトで品目の範囲を明示するもの（キーはターゲット名）。他の品目と混同されやすいものだけ書く
DEMAND_SCOPES = {
    "rare earth": (
        "Scope: rare earths ONLY, i.e. the 17 rare earth elements (e.g. neodymium, praseodymium, dysprosium, "
        "terbium, lanthanum, cerium, yttrium, scandium) and products made mainly from them, such as rare earth "
        "magnets. Other critical or battery minerals (lithium, cobalt, nickel, graphite, manganese, tungsten, "
        "gallium, germanium, antimony, uranium) are NOT rare earths: set excluded=true unless the article also "
        "reports a concrete fact about rare earths themselves. A passing mention of rare earth deposits or of "
        "\"critical minerals\" in general is not enough."
    ),
}
_DEMAND_SCOPE_BY_LABEL = {TARGETS[k]["label"]: v for k, v in DEMAND_SCOPES.items() if k in TARGETS}
_DATE_PRECISION = {4: "year", 7: "month", 10: "day"}  # YYYY / YYYY-MM / YYYY-MM-DD


def _get_target_filter() -> set[str] | None:
    """LLM_TARGET_FILTER環境変数（カンマ区切りターゲットキー）が設定されていれば
    その集合を返す。未設定ならNone（全ターゲット対象）。
    """
    raw = os.getenv("LLM_TARGET_FILTER", "").strip()
    if not raw:
        return None
    return {k.strip() for k in raw.split(",") if k.strip()}


# LLM_MODE_FILTER: 処理する記事の収集モード（monitor/analyze、カンマ区切り）。未設定なら全て
LLM_MODE_FILTER = {m.strip() for m in os.getenv("LLM_MODE_FILTER", "").split(",") if m.strip()} or None


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
        self.model_name = GEMINI_MODEL
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


class _OllamaBackend:
    """ローカルLLM（Ollama）バックエンド。モデル名・接続先は環境変数で指定する。"""
    REQUEST_INTERVAL = 0
    BATCH_LIMIT = OLLAMA_BATCH_LIMIT

    def __init__(self):
        # PC起動直後などOllamaがDockerより後に立ち上がる場合に備え、一定時間は接続を待つ
        deadline = time.time() + OLLAMA_STARTUP_WAIT_SECONDS
        while True:
            try:
                resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
                resp.raise_for_status()
                break
            except Exception as e:
                if time.time() >= deadline:
                    raise ConnectionError(f"Ollama ({OLLAMA_HOST}) に接続できません: {e}")
                logger.warning(f"Ollama ({OLLAMA_HOST}) 接続待ち… 10秒後に再試行")
                time.sleep(10)
        self.model_name = OLLAMA_MODEL
        models = [m.get("name") for m in resp.json().get("models", [])]
        if OLLAMA_MODEL not in models:
            logger.warning(f"モデル {OLLAMA_MODEL} が未取得の可能性があります（取得済み: {models}）")
        logger.info(f"Ollama ({OLLAMA_MODEL} @ {OLLAMA_HOST}) 初期化完了")

    def call(self, prompt: str) -> str:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "format": "json",
                "stream": False,
                "think": False,  # Qwen3等の思考モードを無効化（速度優先）
                "options": {"temperature": 0.01, "num_ctx": OLLAMA_NUM_CTX},
            },
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]


def _get_llm_backend():
    if LLM_BACKEND == "ollama":
        try:
            return _OllamaBackend()
        except Exception as e:
            logger.error(str(e))
            return None
    if LLM_BACKEND in ("", "gemini") and os.getenv("GEMINI_API_KEY"):
        return _GeminiBackend()
    if LLM_BACKEND not in ("", "gemini"):
        logger.warning(f"不明な LLM_BACKEND={LLM_BACKEND}。LLM処理をスキップします。")
        return None
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
    """ターゲット種別に応じたプロンプトを返す。建設ターゲットは専用プロンプト、それ以外は需要シグナル用。"""
    body_text = body.strip() if body else "(本文取得不可 - タイトルのみで判断)"
    if is_construction:
        return _build_construction_prompt(target_label, title, domain, publish_date, body_text)
    return _build_demand_prompt(target_label, title, domain, publish_date, body_text)


def _build_demand_prompt(target_label: str, title: str, domain: str, publish_date: str, body_text: str) -> str:
    scope = _DEMAND_SCOPE_BY_LABEL.get(target_label)
    scope_text = f"\n{scope}\n" if scope else ""
    return f"""You are analyzing a news article as a demand signal for {target_label}.
{scope_text}
Article:
- Title: {title}
- Source: {domain}
- Published: {publish_date}
- Body: {body_text}

Return ONLY valid JSON with this exact structure:
{{
  "reason": "<1-2 sentences in Japanese: which demand-relevant fact the article reports, or why it is not relevant>",
  "causal_evidence": "<a short phrase copied verbatim from the article that states the cause of the supply-demand change, or null>",
  "surprise_evidence": "<a short phrase copied verbatim from the article that explicitly compares a figure or development with expectations, forecasts, consensus, or records, or null>",
  "excluded": <true or false>,
  "rating": <1, 2, or 3, or null if excluded>,
  "causal": {{
    "trigger": "<the cause of the supply-demand change as stated in the article, summarized in Japanese, or null>",
    "mechanism": "<how it propagates through supply and demand as stated in the article, summarized in Japanese, or null>",
    "effect": "<the resulting impact on supply-demand or price as stated in the article, summarized in Japanese, or null>",
    "timeframe": "<immediate/short/medium/long, or null>"
  }},
  "price_direction": <"up", "down", or "none">,
  "demand_direction": <"increase", "decrease", or "none">,
  "supply_direction": <"tighter", "looser", or "none">,
  "tone_score": <-100.0 to +100.0, overall article sentiment, positive=positive coverage, negative=negative coverage>,
  "why_notable": "<why this is notable, in Japanese, or null if rating < 2>",
  "event_date": "<YYYY-MM-DD of the actual event described, or null if unclear>",
  "drivers": ["<demand driver 1, in Japanese>", "<demand driver 2, in Japanese>"],
  "causal_inferred": {{
    "mechanism": "<your own reasoning about how this could propagate through supply and demand, in Japanese, or null>",
    "effect": "<your own expectation of the impact on supply-demand or price, in Japanese, or null>"
  }}
}}

Decide in this order: first write reason, then the two evidence fields, then excluded and rating. Decide excluded and rating ONLY from what the article states; causal_inferred comes last and must not affect them.

excluded: true when the article does not report a concrete fact that affects demand (or the supply-demand balance) for {target_label}. Exclude price-only market reports and technical analysis, investment advice, generic commentary, promotional content, unrelated topics, and duplicates. If the article states no cause of a supply-demand change, excluded must be true.

Rating guide (null when excluded):
- 3: The article itself explicitly states that a figure or development deviates from expectations, forecasts, consensus, or records (e.g. "above forecasts", "unexpected", "record high", "first time since 2015"), or reports a sudden supply or demand shock (export ban, plant closure, new mandate) together with its scale. surprise_evidence must quote that statement. Never infer a surprise by yourself.
- 2: A concrete new fact with a plausible effect on demand (new order, capacity expansion, policy, consumption data) without an explicit comparison to expectations.
- 1: Reconfirms a known trend, or commentary without a new specific fact or figure.
Most relevant articles should be rated 1 or 2; rating 3 should be uncommon.

causal_evidence / surprise_evidence: copy verbatim in the article's original language; never paraphrase or translate. Use null when the article contains no such statement.
causal: summarize in Japanese ONLY what the article itself states about trigger, mechanism, and effect; use null for any part the article does not state. Never put your own reasoning into causal.
causal_inferred: your own reasoning goes here, clearly separated from what the article states. Use null when you have nothing to add.
price_direction: the price movement of {target_label} that the article states or attributes to the reported fact ("up" / "down"); "none" when the article does not say.
demand_direction: whether the reported fact increases or decreases demand for {target_label} ("none" when it does not affect demand).
supply_direction: whether the reported fact makes supply of {target_label} tighter (export ban, mine or plant closure, blocked shipping route, sanctions, outage) or looser (new capacity, restrictions lifted, surplus, inventory build); "none" when it does not affect supply.
tone_score: overall article sentiment aligned with GDELT V2Tone scale (-100=very negative, 0=neutral, +100=very positive), independent of demand direction
event_date: the date the described event actually occurred (not the article publication date). It must not be later than the publication date; scheduled future events are not event_date. Return null when the article does not state the date; never guess or use a placeholder date. Return null when excluded=true.

IMPORTANT: Write reason, why_notable, causal (trigger, mechanism, effect), causal_inferred, and drivers in Japanese, even when the article is in English. Keep causal_evidence and surprise_evidence exactly in the article's original language.
"""


def _build_construction_prompt(target_label: str, title: str, domain: str, publish_date: str, body_text: str) -> str:
    """建設ターゲット専用プロンプト。需要シグナルではなく「特定可能な建設案件の進捗」を抽出する。"""
    try:
        weekday = date.fromisoformat(publish_date).strftime("%A")
        published = f"{publish_date} ({weekday})"
    except ValueError:
        published = publish_date or "unknown"

    return f"""You are extracting information about specific large-scale construction projects of any type for {target_label}: high-rise buildings, large residential/commercial/mixed-use developments, factories, plants and data centers, transport infrastructure (stations, airports, ports, roads, bridges, railways), energy facilities, and large public facilities (stadiums, hospitals, schools, pools). The goal is to track each project's progress so that it can later be verified with satellite imagery.

Article:
- Title: {title}
- Source: {domain}
- Published: {published}
- Body: {body_text}

Return ONLY valid JSON with this exact structure:
{{
  "reason": "<1-2 sentences in Japanese: what the article is mainly about, and which specific project (if any) and what happened to it>",
  "main_subject": <"project_progress", "policy_or_program", "multiple_projects", "company_or_product", "incident_at_building", "history_or_feature", "market_or_opinion", or "other">,
  "project_evidence": "<a short phrase copied verbatim from the article that names or locates the specific project, or null>",
  "excluded": <true or false>,
  "rating": <1, 2, or 3, or null if excluded>,
  "tone": <"bullish", "bearish", or "neutral">,
  "tone_score": <-100.0 to +100.0>,
  "event_date": "<YYYY-MM-DD, or null>",
  "event_date_basis": <"explicit", "relative", "publication", or null>,
  "construction_status": <"planned", "groundbreaking", "under_construction", "halted", "cancelled", "resumed", "topped_out", "completed", or "unknown">,
  "project_type": <"building", "industrial", "infrastructure", "energy", "public_facility", or "other">,
  "building_name": "<proper name of the building/project, or null>",
  "location": {{
    "city": "<city, or null>",
    "country": "<country, or null>",
    "address": "<street address if mentioned, or null>"
  }},
  "milestones": [
    {{
      "event": <"announced", "approved", "groundbreaking", "topped_out", "completed", "opened", "halted", "resumed", "cancelled", or "other">,
      "status": <"actual" or "planned">,
      "date": "<YYYY, YYYY-MM, or YYYY-MM-DD, or null>",
      "date_text": "<the original date wording in the article, or null>"
    }}
  ]
}}

Decide in this order: first write reason, then main_subject, then project_evidence, then excluded.

main_subject: what the article is MAINLY about. Ask: "Is this article's main news the planning, construction, or completion of one specific project?"
- project_progress: the main news is a development of one specific construction project (or two or three closely related ones): announced, approved, financed, contract awarded, groundbreaking, construction progress, delay, halt, lawsuit or opposition that blocks it, labor, safety, or legal issues at its construction site, cancellation, topping out, completion, opening.
- policy_or_program: government policy, national or city-wide programs and targets (e.g. a national affordable-housing target), zoning or regulation in general.
- multiple_projects: a roundup, editorial, or analysis covering several unrelated projects without focusing on one.
- company_or_product: company earnings, strategy, services, products, or promotional content about a firm (even if it mentions where its products are used).
- incident_at_building: a fire, accident, attack, crime, flood, or other event that happens at or to a building, not about building it.
- history_or_feature: history, anniversaries, retrospectives, interviews or memoirs about past projects, book reviews, models or artworks, celebrity homes, lifestyle features.
- market_or_opinion: real estate market, prices, rents, sales, or opinion/essays about cities and architecture in general.
- other: anything else.
A building or construction site merely being the setting of the story does NOT make it project_progress.

project_evidence: copy verbatim, in the article's original language, the shortest phrase that names or locates the specific project (e.g. "a 40-storey tower in Al Khobar", "Jeddah Tower"). Use null when the article has no specific project.
excluded: false ONLY when main_subject is "project_progress" AND the project is a specific, identifiable large-scale construction project (a named project, or a clearly identified site such as "a 40-storey tower in Al Khobar"). In every other case excluded must be true. If project_evidence is null, excluded must be true. Also exclude small works (a single house, minor repairs or renovations). When the article covers two or three related projects, describe the main one.

rating (null when excluded):
- 3: A status change of the project: newly announced, approved, groundbreaking, halted, cancelled, resumed, topped out, completed/opened
- 2: Progress update, contract award, financing, or a schedule change/delay of an ongoing project
- 1: The project is mentioned, but there is no new development

tone: bullish=the project is advancing (new, started, progressing, completed), bearish=setback (halted, cancelled, delayed, funding problems), neutral=mixed/unclear
tone_score: overall article sentiment aligned with GDELT V2Tone scale (-100=very negative, 0=neutral, +100=very positive)

event_date: the date on which the main reported development happened. First look for an explicit date or weekday of that development in the article (e.g. "on March 3", "on Tuesday"); use the publication date only when the article gives no timing at all. It must not be later than the publication date; scheduled future events go to milestones, not event_date.
event_date_basis:
- "explicit": the article states the date
- "relative": computed from relative wording such as "on Tuesday" or "last week", using the publication date and weekday above. A weekday means the most recent such day before the publication date: if published on Wednesday 2026-03-04, "on Monday" is 2026-03-02 and "on Wednesday" is 2026-03-04.
- "publication": the article reports it as current news without any date, so the publication date is used
Set event_date and event_date_basis to null when excluded=true, or when the timing cannot be determined (e.g. a background feature about an older event). Never guess or use a placeholder date.

construction_status: the status of the main project as of the publication date. Decide it from what the article says about physical work on the site.
- under_construction: the article describes the project as being built, rising, under construction, or with work underway, or the groundbreaking took place earlier (weeks, months, or years before). A project whose first phases have opened but whose main part is still being built is also under_construction.
- planned: ONLY when the article indicates that construction has not started yet: proposed, designed, seeking or receiving approval, permits, financing, or contracts, or a groundbreaking ceremony that is only scheduled for the future.
- groundbreaking: the main news is that the groundbreaking ceremony or the start of construction took place within the last few days.
- halted: construction stopped or suspended. resumed: restarted after a halt. cancelled: the project was scrapped.
- topped_out: the structure reached its final height.
- completed: construction finished, or the facility opened or was inaugurated.
- unknown: the article does not make the status clear.
project_type: building=high-rise/residential/commercial/mixed-use buildings and developments, industrial=factories/plants/data centers/warehouses, infrastructure=stations/airports/ports/roads/bridges/railways, energy=power plants/grids/pipelines, public_facility=stadiums/hospitals/schools/pools and other public buildings, other=anything else.
building_name: the proper name of the building or project, copied exactly as written in the article (do not translate it, shorten it, or add descriptive words). If the article gives any proper or working name for the project (e.g. "Hudson Yards", "Line 5 Extension", "Riverside Medical Pavilion"), you must fill it. Use null only when the article gives no name, and never for generic words such as "skyscraper", "tower", or "高層ビル".
location: best-effort extraction to help locate the site on satellite imagery; fill city whenever the article mentions the city or town of the site; null when not stated.

milestones: every milestone of the main project mentioned in the article, both past (status=actual) and scheduled (status=planned), e.g. a groundbreaking ceremony scheduled for next month and a target completion year.
- date must keep the full precision of date_text: "August 4, 2026" -> "2026-08-04", "March 2027" -> "2027-03", "completion in 2029" -> "2029". Never reduce precision, and never pad a missing month or day with 01.
- When the article gives a month and day without a year, infer the year from the publication date. An "actual" milestone cannot be later than the publication date.
- Return [] when no milestone is mentioned or when excluded=true.

IMPORTANT: Write "reason" in Japanese, even when the article is in English. Copy every other text field (project_evidence, building_name, location, date_text) character by character in the same language and script as the article. Never translate or transliterate them: for an English article, "Hudson Yards" and "New York" must stay in Latin letters, never katakana or kanji.
"""


# ============================================================
# 日付・工程の検証（LLM出力の後処理）
# ============================================================
_PARTIAL_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_YEAR_RE = re.compile(r"\d{4}")


def _as_bool(value) -> bool:
    """LLMが真偽値を文字列("false"等)で返しても正しく判定する。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return value == 1


def _normalize_partial_date(value) -> str | None:
    """YYYY / YYYY-MM / YYYY-MM-DD のいずれかで実在する日付なら文字列を返す。それ以外はNone。"""
    if not isinstance(value, str) or not _PARTIAL_DATE_RE.match(value.strip()):
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        elif len(value) == 7:
            date.fromisoformat(f"{value}-01")
    except ValueError:
        return None
    return value


def _is_after_publication(partial: str, publish_date) -> bool:
    """部分日付(YYYY/YYYY-MM/YYYY-MM-DD)が公開日の翌日より後か。同じ精度で比較する。"""
    if not publish_date:
        return False
    reference = (publish_date + timedelta(days=1)).date().isoformat()
    return partial > reference[:len(partial)]


def _shift_year_back(partial: str) -> str | None:
    """部分日付の年を1年戻す（2/29など実在しない日付になる場合はNone）。"""
    shifted = f"{int(partial[:4]) - 1:04d}{partial[4:]}"
    return _normalize_partial_date(shifted)


def _sanitize_milestones(raw, publish_date) -> list[dict]:
    """LLMが返したmilestonesを検証・正規化する。

    - precision は日付の書式から機械的に決める（LLMの自己申告は使わない）
    - date_text が ISO形式でより詳細なら、そちらを採用する（LLMが精度を落とす対策）
    - status=actual なのに公開日より後の日付:
        年の書かれていない日付（例 "August 31"）→ 年の推定ミスとみなし1年戻す
        それ以外 → 予定の誤判定とみなし status を planned にする
      補正した場合は "fixed" に内容を記録する（精度評価用）
    """
    if not isinstance(raw, list):
        return []
    milestones = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        event = m.get("event") if m.get("event") in MILESTONE_EVENTS else "other"
        status = m.get("status") if m.get("status") in MILESTONE_STATUSES else "unknown"
        date_text = m.get("date_text")
        date_value = _normalize_partial_date(m.get("date"))
        fixed = None

        text_date = _normalize_partial_date(date_text)
        if text_date and len(text_date) > len(date_value or "") and text_date.startswith(date_value or ""):
            date_value = text_date
            fixed = "precision_from_date_text"

        if status == "actual" and date_value and _is_after_publication(date_value, publish_date):
            shifted = _shift_year_back(date_value) if len(date_value) > 4 and not _YEAR_RE.search(str(date_text or "")) else None
            if shifted and not _is_after_publication(shifted, publish_date):
                date_value = shifted
                fixed = "year_shifted_back"
            else:
                status = "planned"
                fixed = "status_to_planned"

        milestone = {
            "event":     event,
            "status":    status,
            "date":      date_value,
            "precision": _DATE_PRECISION.get(len(date_value)) if date_value else None,
            "date_text": date_text,
        }
        if fixed:
            milestone["fixed"] = fixed
        milestones.append(milestone)
    return milestones


def _sanitize_dates(result: dict, publish_date, is_construction: bool) -> tuple[date | None, str | None, str | None, list[dict]]:
    """event_date / event_date_basis / milestones を検証する。

    戻り値: (event_date, event_date_raw, event_date_basis, milestones)
      - event_date_raw: 検証で値を変えた場合のみLLMの元の値（精度評価用）、変えなければNone
      - 除外記事 / 日付として読めない値 → event_date を None
      - 公開日の翌日より後（未来の日付）→ event_date を None。建設ターゲットでは
        予定情報を失わないよう milestones に planned として移す
      - event_date_basis は建設ターゲットのみ（event_date が None なら None）
    """
    excluded = _as_bool(result.get("excluded"))
    milestones = [] if excluded or not is_construction else _sanitize_milestones(result.get("milestones"), publish_date)
    basis = result.get("event_date_basis")
    basis = basis if is_construction and basis in EVENT_DATE_BASES else None

    raw = result.get("event_date")
    if not raw:
        return None, None, None, milestones
    if excluded:
        return None, raw, None, milestones
    try:
        event_date = date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None, raw, None, milestones

    if publish_date and event_date > (publish_date + timedelta(days=1)).date():
        if is_construction and not any(m["date"] == raw for m in milestones):
            milestones.append({
                "event": "other", "status": "planned", "date": raw,
                "precision": "day", "date_text": None, "fixed": "moved_from_event_date",
            })
        return None, raw, None, milestones

    return event_date, None, basis, milestones


_JA_CHARS = re.compile(r"[ぁ-んァ-ヶ一-龥]")  # ひらがな・カタカナ・漢字


def _null_if_blank(value):
    """LLMが空の代わりに返す "null" / "None" / "N/A" / 空文字を None に揃える。"""
    if isinstance(value, str) and value.strip().lower() in ("", "null", "none", "n/a", "unknown"):
        return None
    return value


def _normalize_for_match(text: str) -> str:
    """引用照合用に小文字化し、記号・空白を除去する（CJK文字は残る）。"""
    return re.sub(r"\W+", "", str(text).lower())


def _apply_subject_rule(result: dict) -> dict:
    """建設記事: 主題（main_subject）が案件の進捗でないのに採用されていたら除外に直す。

    プロンプトでは「project_progress 以外は除外」と指示しているが、LLMが主題の分類と
    除外判定を食い違えることがあるため、分類の方を正としてそろえる。直した場合は
    excluded_by_subject=True を付ける（元の判定は精度評価のために残す）。
    """
    subject = result.get("main_subject")
    if subject in MAIN_SUBJECTS and subject != "project_progress" and not _as_bool(result.get("excluded")):
        result = {**result, "excluded": True, "rating": None, "excluded_by_subject": True}
    return result


def _apply_demand_rules(result: dict) -> dict:
    """需要記事の出力を整える（方向の合成と、記事に因果がない記事の除外）。

    tone: 需要と供給の向きを足し合わせて決める（需要増・供給の引き締まり=+1、需要減・供給の緩み=-1）。
    どちらも動かないか打ち消し合うときだけ、記事が書く価格の向きを使う（それもなければ neutral）。
    記事が述べたきっかけ（causal.trigger）が空なのに採用されている記事は除外し、excluded_by_rule を立てる。
    """
    causal = result.get("causal") if isinstance(result.get("causal"), dict) else {}
    result["causal"] = {k: _null_if_blank(v) for k, v in causal.items()}
    inferred = result.get("causal_inferred") if isinstance(result.get("causal_inferred"), dict) else {}
    result["causal_inferred"] = {k: _null_if_blank(v) for k, v in inferred.items()} or None

    price = result.get("price_direction")
    score = {"increase": 1, "decrease": -1}.get(result.get("demand_direction"), 0) + \
        {"tighter": 1, "looser": -1}.get(result.get("supply_direction"), 0)
    if score:
        result["tone"] = "bullish" if score > 0 else "bearish"
    else:
        result["tone"] = {"up": "bullish", "down": "bearish"}.get(price, "neutral")

    if not _as_bool(result.get("excluded")) and not result["causal"].get("trigger"):
        result["excluded"] = True
        result["rating"] = None
        result["excluded_by_rule"] = "no_stated_trigger"
    return result


def _verify_demand_evidence(result: dict, article_text: str) -> tuple[int | None, dict]:
    """需要用の根拠の引用が記事（タイトル＋本文）に実在するかを照合する。

    ★3（予想・コンセンサスとの乖離）は、記事中の比較の記述を根拠とする基準のため、
    surprise_evidence が記事に実在しない★3は★2に下げ、元の値を rating_raw に残す。
    戻り値: (保存するrating, llm_analysis["evidence"] に入れる内容)
    """
    normalized_text = _normalize_for_match(article_text)

    def check(value):
        value = _null_if_blank(value)
        normalized = _normalize_for_match(value) if value else ""
        return value, bool(normalized) and normalized in normalized_text

    causal_evidence, causal_found = check(result.get("causal_evidence"))
    surprise_evidence, surprise_found = check(result.get("surprise_evidence"))
    evidence = {
        "causal_evidence":  causal_evidence,
        "causal_found":     causal_found,
        "surprise_evidence": surprise_evidence,
        "surprise_found":   surprise_found,
    }

    rating = result.get("rating")
    try:
        rating = int(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating = None
    if _as_bool(result.get("excluded")):
        rating = None
    elif rating == 3 and not surprise_found:
        evidence["rating_raw"] = 3
        rating = 2
    return rating, evidence


def _build_construction_info(result: dict, milestones: list[dict], article_text: str = "") -> dict:
    """建設ターゲットの llm_analysis["construction"] を組み立てる。

    evidence_found: LLMが引用した project_evidence が記事（タイトル＋本文）に実在するか。
    identifiable: 引用が実在し、かつ案件名か所在都市が取れているか。除外されていないのに False の記事は
    「具体的な案件が特定できない／LLMの作り話」の可能性が高く、分析時の絞り込みに使う
    （LLMの除外判定そのものは上書きしない）。
    """
    location = result.get("location") if isinstance(result.get("location"), dict) else None
    if location:
        location = {k: _null_if_blank(v) for k, v in location.items()}
    building_name = _null_if_blank(result.get("building_name"))
    project_type = result.get("project_type")
    evidence = _null_if_blank(result.get("project_evidence"))
    normalized_evidence = _normalize_for_match(evidence) if evidence else ""
    evidence_found = bool(normalized_evidence) and normalized_evidence in _normalize_for_match(article_text)

    # 理由の欄（日本語）につられて、日本語でない記事の案件名・都市が日本語に訳されることがある。
    # 記事に日本語が無いのに日本語が入っていれば訳出とみなし、案件名は記事から書き写した根拠の引用に、
    # 都市は空に置き換える（衛星画像との照合では、誤った表記より空の方が安全なため）
    name_fixed = None
    if not _JA_CHARS.search(article_text or ""):
        if building_name and _JA_CHARS.search(building_name):
            building_name = evidence if evidence_found else None
            name_fixed = "translated"
        if location and location.get("city") and _JA_CHARS.search(location["city"]):
            location = {**location, "city": None}
            name_fixed = name_fixed or "translated_city"
    return {
        "status":           result.get("construction_status"),
        "main_subject":     result.get("main_subject") if result.get("main_subject") in MAIN_SUBJECTS else None,
        "excluded_by_subject": bool(result.get("excluded_by_subject")),
        "name_fixed":       name_fixed,
        "project_type":     project_type if project_type in PROJECT_TYPES else "other",
        "building_name":    building_name,
        "location":         location,
        "project_evidence": evidence,
        "evidence_found":   evidence_found,
        "identifiable":     evidence_found and bool(building_name or (location and location.get("city"))),
        "milestones":       milestones,
    }


# ============================================================
# バッチ処理（全ターゲットの未処理記事を一括処理）
# ============================================================
def run_all(backend, target_keys: set[str] | None = None, article_ids: list[int] | None = None) -> None:
    """未処理記事をLLMで処理する。article_ids を渡すと、その記事（未処理のもの）だけを件数上限なしで処理する。"""
    session = SessionLocal()
    try:
        query = session.query(Article).filter(Article.is_llm_processed == False)
        if target_keys:
            query = query.filter(Article.target.in_(target_keys))
        if LLM_MODE_FILTER and article_ids is None:
            query = query.filter(Article.collection_mode.in_(LLM_MODE_FILTER))
        if article_ids is not None:
            query = query.filter(Article.id.in_(article_ids))
        batch_limit = len(article_ids) if article_ids is not None else getattr(backend, "BATCH_LIMIT", LLM_BATCH_LIMIT)
        rows = query.order_by(Article.publish_date.desc()).limit(batch_limit).all()
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

        if is_construction:
            result = _apply_subject_rule(result)
        else:
            result = _apply_demand_rules(result)

        # event_date を検証して独立カラムに昇格（時系列SQL検索用）
        event_date, event_date_raw, event_date_basis, milestones = _sanitize_dates(
            result, row.publish_date, is_construction
        )
        row.event_date = event_date
        event_date_str = event_date.isoformat() if event_date else None

        article_text = f"{title}\n{row.body or ''}"
        rating = result.get("rating")
        demand_evidence = None
        if not is_construction:
            rating, demand_evidence = _verify_demand_evidence(result, article_text)

        # 建設ターゲットは専用プロンプトのため why_notable/causal/drivers は返らない（キーは互換性のため残す）
        row.llm_analysis = {
            "rating":       rating,
            "excluded":     _as_bool(result.get("excluded", False)),
            "tone":         result.get("tone"),
            "tone_score":   result.get("tone_score"),
            "reason":       result.get("reason"),
            "why_notable":  result.get("why_notable"),
            "causal":       result.get("causal"),
            "drivers":      result.get("drivers", []),
            "llm_model":    getattr(backend, "model_name", None),
            "prompt_version": PROMPT_VERSIONS["construction" if is_construction else "demand"],
        }
        if event_date_raw is not None:
            row.llm_analysis["event_date_raw"] = event_date_raw
        if demand_evidence is not None:
            row.llm_analysis["evidence"] = demand_evidence
            row.llm_analysis["causal_inferred"] = result.get("causal_inferred")
            row.llm_analysis["direction"] = {k: result.get(f"{k}_direction") for k in ("price", "demand", "supply")}
            if result.get("excluded_by_rule"):
                row.llm_analysis["excluded_by_rule"] = result["excluded_by_rule"]
        if is_construction:
            row.llm_analysis["event_date_basis"] = event_date_basis
            row.llm_analysis["construction"] = _build_construction_info(result, milestones, article_text)
        row.is_llm_processed = True

        session = SessionLocal()
        try:
            session.add(row)
            session.commit()
            processed += 1
            extra_log = (
                f" construction_status={result.get('construction_status')} milestones={len(milestones)}"
                if is_construction else ""
            )
            if event_date_raw is not None:
                extra_log += f" event_date_raw={event_date_raw}"
            if demand_evidence is not None:
                extra_log += (f" causal_found={demand_evidence['causal_found']}"
                              f" surprise_found={demand_evidence['surprise_found']}")
                if "rating_raw" in demand_evidence:
                    extra_log += " rating_raw=3"
            logger.info(
                f"[{row.target}/{row.collection_mode}] id={row.id} "
                f"rating={rating} tone={result.get('tone')} "
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
