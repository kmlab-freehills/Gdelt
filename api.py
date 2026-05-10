import os
from fastapi import FastAPI, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
from datetime import datetime
import uvicorn
from dotenv import load_dotenv

from database import SessionLocal, Article

# 環境変数の読み込み
load_dotenv()

app = FastAPI(
    title="GDELT データ収集 API",
    description="収集されたGDELTニュース記事にアクセスするためのAPI",
    version="1.1.0"
)

# リクエストごとにDBセッションを取得するための依存関係
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/")
def health_check():
    """APIの状態確認用エンドポイント"""
    return {"status": "ok", "message": "GDELT Collector API is running."}

@app.get("/api/v1/articles")
def get_articles(
    task_name: Optional[str] = Query(None, description="タスク名でフィルタリング"),
    start_date: Optional[datetime] = Query(None, description="開始日 (ISO 8601形式)"),
    end_date: Optional[datetime] = Query(None, description="終了日 (ISO 8601形式)"),
    is_llm_processed: Optional[bool] = Query(None, description="LLM処理ステータスでフィルタリング"),
    limit: int = Query(100, ge=1, le=1000, description="取得件数"),
    offset: int = Query(0, ge=0, description="オフセット"),
    db: Session = Depends(get_db)
):
    """フィルタリングとページネーションを使用して記事を取得"""
    query = db.query(Article)

    if task_name:
        query = query.filter(Article.task_name == task_name)
    if start_date:
        query = query.filter(Article.publish_date >= start_date)
    if end_date:
        query = query.filter(Article.publish_date <= end_date)
    if is_llm_processed is not None:
        query = query.filter(Article.is_llm_processed == is_llm_processed)

    query = query.order_by(Article.publish_date.desc())
    
    total_count = query.count()
    articles = query.offset(offset).limit(limit).all()

    return {
        "total": total_count,
        "limit": limit,
        "offset": offset,
        "data": [
            {
                "id": a.id,
                "task_name": a.task_name,
                "publish_date": a.publish_date,
                "url": a.url,
                "is_llm_processed": a.is_llm_processed,
                "llm_is_relevant": a.llm_is_relevant,
                "llm_tone_score": a.llm_tone_score,
                "raw_data": a.raw_data
            }
            for a in articles
        ]
    }

@app.get("/api/v1/stats")
def get_stats(db: Session = Depends(get_db)):
    """タスクごとの統計情報を取得"""
    stats_query = db.query(
        Article.task_name,
        func.count(Article.id).label("total_articles"),
        func.min(Article.publish_date).label("oldest_article"),
        func.max(Article.publish_date).label("newest_article")
    ).group_by(Article.task_name).all()

    return [
        {
            "task_name": row.task_name,
            "total_articles": row.total_articles,
            "oldest_article": row.oldest_article,
            "newest_article": row.newest_article
        }
        for row in stats_query
    ]

if __name__ == "__main__":
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", 8000))
    uvicorn.run("api:app", host=host, port=port, reload=False)
