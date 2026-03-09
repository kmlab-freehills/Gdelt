import os
from datetime import datetime, timedelta
from typing import List, Dict, Any
import warnings
from google.cloud import bigquery
import time
import trafilatura

warnings.filterwarnings('ignore')

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "./gcp-key.json"

GDELT_PROJECT = "gdelt-bq"
GDELT_DATASET = "gdeltv2"
GDELT_TABLE = "gkg_partitioned"

try:
    bq_client = bigquery.Client()
    print("✅ BigQuery Client initialized.")
except Exception as e:
    print(f"❌ BigQuery初期化エラー: {e}")
    bq_client = None


def fetch_metadata(keywords: List[str], target_days: int = 7, limit_per_keyword: int = 10) -> List[Dict]:
    """BigQueryを使用してGDELTから関連ニュースのメタデータを取得"""
    if not bq_client:
        return []

    start_date = (datetime.now() - timedelta(days=target_days)).strftime('%Y-%m-%d')
    all_results = []

    print(f"📡 GDELT検索開始 (過去{target_days}日間)...")

    target_themes = [ # TODO: 需要（ニーズ）の動向・その元記事（○○の需要が増）・プレスリリース的なもの。商品リストでとる。先物、LLMに列挙させる。典型フレーズでの検索も視野（○○の売り上げが増加）
                      #       将来的には因果関係の構造化データとして（鉱山の夜間光増 -> データセンターで銅が使われてる -> ○○だから）
        # --- A. 製造・生産 ---
        "TAX_FNCACT_MANUFACTURER",  # 製造業全般
        "ECON_PRODUCTION",          # 生産活動
        "ECON_INDUSTRY",            # 産業一般
        "ENV_MINING",               # 鉱業（原材料供給）
        "TECHE_INNOVATION",         # 技術革新（新製品・新技術）

        # --- B. 物流・インフラ ---
        "CRISISLEX_C04_LOGISTICS_TRANSPORT", # 物流危機・輸送障害
        "ECON_SHIPPING",            # 海運
        "ECON_TRANSPORTATION",      # 輸送全般
        
        # --- C. リスク・有事 ---
        "MANMADE_DISASTER_INDUSTRIAL", # 産業事故（工場火災・爆発等）
        "NATURAL_DISASTER",            # 自然災害（地震・洪水等による稼働停止）
        "TAX_EMPLOYEES_STRIKE",        # ストライキ
        "ECON_BANKRUPTCY",             # 倒産
        
        # --- D. 経済・政治（間接影響） ---
        "ECON_TRADE_DISPUTE",       # 貿易摩擦
        "ECON_TARIFFS",             # 関税
        "ECON_M_A",                 # M&A（業界再編）
        "ECON_INVEST",              # 投資（新工場建設など）
        
        # --- E. 規制・地政学リスク ---
        "ECON_SANCTIONS",           # 経済制裁・輸出規制（半導体規制など）
        "ECON_SUBSIDIES",           # 補助金（サプライチェーンの国内回帰、工場誘致など）
        "LEGISLATION",              # 新たな法規制（環境規制、労働法改正など）
        "ELECTION",                 # 選挙（政権交代による急激な政策変更リスク）
        "MILITARY",                 # 軍事・地政学（紛争による特定地域からの撤退や物流網の断絶）
        "ECON_BOYCOTT",             # 不買運動・ボイコット（政治的理由による製品排除）
        "ECON_NATIONALIZE",         # 国有化（海外拠点の接収リスク）
        "ARMEDCONFLICT",            # 武力衝突（局地的な紛争）

        # --- F. 社会・サイバー・ESG（オペレーション寸断リスク） ---
        "SOC_LABOR_STRIKE",         # 労働ストライキ（物流網や巨大工場の麻痺）
        "CYBER_ATTACK",             # サイバー攻撃（システム乗っ取り・稼働停止）
        "INTELLECTUAL_PROPERTY",    # 知的財産（技術流出・特許侵害による事業停止）
        "ENV_CLIMATECHANGE",        # 気候変動（異常気象による原料不足）
        "WATER_SECURITY",           # 水資源の安全保障（工場稼働のための取水制限）

        # --- G. 経済状況・マクロ指標 ---
        "ECON_INFLATION",           # インフレ
        "ECON_INTEREST_RATES",      # 金利
        "UNEMPLOYMENT",             # 失業率
        "ECON_OILPRICE",            # 原油価格
        "SHORTAGE",                 # 不足
        "AUSTERITY",                # 緊縮財政

        # --- H. 政治・社会不安 ---
        "PROTEST",                  # 抗議活動
        "POLITICAL_TURMOIL",        # 政情不安
        "VIOLENT_UNREST",           # 暴動
        "STATE_OF_EMERGENCY",       # 非常事態宣言
        "TERROR",                   # テロ
        
        # --- I. インフラ・サプライチェーン寸断 ---
        "POWER_OUTAGE",             # 停電
        "BLOCKADE",                 # 封鎖
        "MARITIME_INCIDENT",        # 海難事故
    ]
    
    theme_conditions = " OR ".join([f"V2Themes LIKE '%{t}%'" for t in target_themes])

    for kw in keywords:
        kw_parts = kw.split()
        kw_conditions = []
        for part in kw_parts:
            clean_part = part.replace("'", "\\'").lower()
            kw_conditions.append(f"(LOWER(AllNames) LIKE '%{clean_part}%' OR LOWER(DocumentIdentifier) LIKE '%{clean_part}%')")
        
        keyword_condition_sql = " AND ".join(kw_conditions)

        query = f"""
        SELECT
            DATE(PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATE AS STRING))) as event_date,
            DocumentIdentifier as url,
            SourceCommonName as source_name,
            V2Tone as tone_raw,
            V2Themes as themes,
            V2Locations as locations,
            V2Organizations as organizations,
            V2Persons as persons, 
            '{kw}' as search_keyword
        FROM
            `{GDELT_PROJECT}.{GDELT_DATASET}.{GDELT_TABLE}`
        WHERE
            _PARTITIONDATE >= '{start_date}'
            AND ({keyword_condition_sql})
            AND (
                {theme_conditions}
            )
        ORDER BY
            PARSE_TIMESTAMP('%Y%m%d%H%M%S', CAST(DATE AS STRING)) DESC
        LIMIT {limit_per_keyword}
        """
        
        try:
            job = bq_client.query(query)
            results = [dict(row) for row in job]
            
            formatted_results = []
            for item in results:
                event_datetime = datetime.combine(item['event_date'], datetime.min.time())

                formatted_results.append({
                    "date": event_datetime,
                    "keyword": item['search_keyword'],
                    "status": "UNCHECKED",
                    "ai_summary": "要約待ち...",
                    "url": item['url'],
                    "source_name": item.get('source_name'),
                    "organizations": item.get('organizations'),
                    "persons": item.get('persons'),
                    "themes": item.get('themes'),
                    "locations": item.get('locations'),
                    "tone_raw": item.get('tone_raw')
                })
            
            print(f"  ✅ [{kw}] {len(formatted_results)}件 ヒット")
            all_results.extend(formatted_results)
            
        except Exception as e:
            print(f"  ❌ [{kw}] Query Error: {e}")
            
    return all_results


def enrich_with_ai(raw_data: List[Dict], llm_handler=None) -> List[Dict]:
    """AI判定の前に本文を取得して、判断材料を増やす"""
    if not llm_handler:
        return raw_data
    
    print(f"\n🕷️ 記事本文のスクレイピング開始 ({len(raw_data)}件)...")

    for item in raw_data:
        url = item.get('url')
        if not url:
            continue
            
        try:
            downloaded = trafilatura.fetch_url(url)
            
            if downloaded:
                text_content = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
                
                if text_content:
                    item['scraped_text'] = text_content[:2000] 
                    print(f"  ✅ 本文取得成功: {item['source_name']}")
                else:
                    print(f"  ⚠️ 本文抽出不可: {url}")
            else:
                print(f"  ⚠️ アクセス不可: {url}")
                
        except Exception as e:
            print(f"  ❌ スクレイピングエラー: {e}")
            
        time.sleep(1) 

    enriched_data = llm_handler.process_batch(raw_data)
    
    return enriched_data


def execute_collection(keywords: List[str], llm_handler=None):
    """APIから呼び出されるメイン処理"""
    
    raw_data = fetch_metadata(keywords, target_days=7, limit_per_keyword=10)
    
    if not raw_data and not bq_client:
         return {"error": "BigQuery接続エラー"}, []

    enriched_data = enrich_with_ai(raw_data, llm_handler=llm_handler)
    
    summary = {
        "total_articles": len(enriched_data),
        "relevant_count": sum(1 for x in enriched_data if x.get('status') == 'RELEVANT'),
        "noise_count": sum(1 for x in enriched_data if x.get('status') == 'NOISE'),
        "unchecked_count": sum(1 for x in enriched_data if x.get('status') == 'UNCHECKED')
    }

    return summary, enriched_data

if __name__ == "__main__":
    print("--- Testing Fetcher (No LLM) ---")
    test_kws = ["TESLA cars", "Trump tariff"]
    s, d = execute_collection(test_kws, llm_handler=None)
    print(s)