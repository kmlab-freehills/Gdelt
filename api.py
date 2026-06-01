import os
from datetime import datetime
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query
from sqlalchemy import func, cast, Integer
from sqlalchemy.orm import Session

from database import Article, SessionLocal

load_dotenv()

app = FastAPI(
    title="GDELT 需要シグナル API",
    description="収集・分析済みのGDELTニュース記事にアクセスするAPI",
    version="2.0.0",
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.get("/api/v1/articles")
def get_articles(
    commodity: Optional[str] = Query(None, description="コモディティキー (例: copper, gold)"),
    start_date: Optional[datetime] = Query(None, description="開始日 (ISO 8601)"),
    end_date: Optional[datetime] = Query(None, description="終了日 (ISO 8601)"),
    is_llm_processed: Optional[bool] = Query(None, description="LLM処理済みのみ"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(Article)

    if commodity:
        query = query.filter(Article.task_name == commodity)
    if start_date:
        query = query.filter(Article.publish_date >= start_date)
    if end_date:
        query = query.filter(Article.publish_date <= end_date)
    if is_llm_processed is not None:
        query = query.filter(Article.is_llm_processed == is_llm_processed)

    query = query.order_by(Article.publish_date.desc())
    total = query.count()
    rows = query.offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [
            {
                "id": r.id,
                "commodity": r.task_name,
                "publish_date": r.publish_date,
                "url": r.url,
                "body": r.body,
                "is_llm_processed": r.is_llm_processed,
                "llm_analysis": r.llm_analysis,
                "raw_data": r.raw_data,
            }
            for r in rows
        ],
    }


@app.get("/api/v1/stats")
def get_stats(db: Session = Depends(get_db)):
    rows = (
        db.query(
            Article.task_name,
            func.count(Article.id).label("total"),
            func.sum(cast(Article.is_llm_processed, Integer)).label("llm_processed"),
            func.max(Article.publish_date).label("latest"),
        )
        .group_by(Article.task_name)
        .all()
    )

    return [
        {
            "commodity": r.task_name,
            "total_articles": r.total,
            "llm_processed": r.llm_processed or 0,
            "latest_article": r.latest,
        }
        for r in rows
    ]


if __name__ == "__main__":
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("api:app", host=host, port=port, reload=False)
