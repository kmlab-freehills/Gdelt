import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, conlist
from typing import List, Optional
from datetime import datetime
import logging
from contextlib import asynccontextmanager

import evidence_fetcher as fetcher
from src.llm_local import LocalLLMHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

llm_instance = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm_instance
    print("\n🚀 Initializing Local LLM System...")
    print("   Please wait while the model loads into VRAM.")
    
    try:
        llm_instance = LocalLLMHandler()
        print("✨ Local LLM is ready to serve!")
    except Exception as e:
        print(f"❌ Failed to load LLM: {e}")
        llm_instance = None
    
    yield
    
    print("🛑 Shutting down server...")
    llm_instance = None

app = FastAPI(
    title="GDELT サプライチェーン・ニュース収集API (Local LLM)",
    description="GDELT BigQueryとローカルQwenモデルを使用して、ニュースを収集・判定します。",
    version="4.0.0",
    lifespan=lifespan
)

class CollectionRequest(BaseModel):
    """収集リクエスト"""
    keywords: conlist(str, min_length=1, max_length=5) = Field(...,
        description="検索キーワード",
        example=["Nippon Steel US Steel", "Boeing strike", "Volkswagen factory"]
    )

class CollectionSummary(BaseModel):
    """収集結果サマリー"""
    total_articles: int = Field(..., description="収集された記事総数")
    relevant_count: int = Field(..., description="AIが「関連あり」と判定した件数")
    noise_count: int = Field(..., description="AIが「ノイズ」と判定した件数")
    unchecked_count: int = Field(..., description="AI判定エラーまたは未実施の件数")

class NewsEvidence(BaseModel):
    """DB保存用 ニュースエビデンスモデル"""
    date: datetime = Field(..., description="記事公表日/イベント日")
    keyword: str = Field(..., description="ヒットした検索キーワード")
    
    status: str = Field(..., description="ステータス (RELEVANT/NOISE/UNCHECKED)")
    ai_summary: str = Field(..., description="AIによる要約または判定コメント")
    
    url: Optional[str] = Field(None, description="記事URL")
    source_name: Optional[str] = Field(None, description="メディア名")
    organizations: Optional[str] = Field(None, description="関連組織 (V2Organizations)")
    persons: Optional[str] = Field(None, description="関連人物 (V2Persons)")
    themes: Optional[str] = Field(None, description="関連テーマ (V2Themes)")
    locations: Optional[str] = Field(None, description="場所 (V2Locations)")
    tone_raw: Optional[str] = Field(None, description="トーン情報 (Tone)")

    class Config:
        orm_mode = True

class CollectionResponse(BaseModel):
    """APIレスポンス全体"""
    summary: CollectionSummary
    details: List[NewsEvidence]
    engine: str = "GDELT + Local Qwen"
    last_updated: datetime = Field(default_factory=datetime.now)

@app.post("/api/v1/news/collect",
         response_model=CollectionResponse,
         summary="ニュースメタデータ収集を実行")
async def run_collection(request: CollectionRequest):
    """
    指定されたキーワードでGDELTを検索し、ローカルLLMで判定を行います。
    """
    global llm_instance
    
    if llm_instance is None:
        raise HTTPException(
            status_code=503, 
            detail="LLM Service is not available (Model failed to load or is loading)."
        )

    logger.info(f"ニュース収集開始: {request.keywords}")

    try:
        summary_data, details_data = fetcher.execute_collection(
            request.keywords, 
            llm_handler=llm_instance
        )

        if "error" in summary_data:
             logger.error(f"収集エンジンエラー: {summary_data['error']}")
             raise HTTPException(status_code=500, detail=summary_data['error'])

        logger.info(f"収集完了: 合計 {summary_data['total_articles']} 件")

        return CollectionResponse(
            summary=CollectionSummary(**summary_data),
            details=details_data
        )

    except Exception as e:
        logger.error(f"サーバー内部エラー: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    print("🚀 GDELT News Collection Server Starting...")
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=True)
