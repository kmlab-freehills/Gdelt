import os
from datetime import date, datetime
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query
from sqlalchemy import cast, Integer, func
from sqlalchemy.orm import Session

from database import Article, SessionLocal

load_dotenv()

app = FastAPI(
    title="GDELT 需要シグナル API",
    description="収集・分析済みのGDELTニュース記事にアクセスするAPI",
    version="3.0.0",
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
    target: Optional[str] = Query(None, description="ターゲットキー (例: copper, gold)"),
    collection_mode: Optional[str] = Query(None, description="収集モード (monitor / analyze)"),
    start_event_date: Optional[date] = Query(None, description="事象日・開始 (YYYY-MM-DD)"),
    end_event_date: Optional[date] = Query(None, description="事象日・終了 (YYYY-MM-DD)"),
    start_date: Optional[datetime] = Query(None, description="掲載日・開始 (ISO 8601)"),
    end_date: Optional[datetime] = Query(None, description="掲載日・終了 (ISO 8601)"),
    is_llm_processed: Optional[bool] = Query(None, description="LLM処理済みのみ"),
    min_rating: Optional[int] = Query(None, ge=1, le=3, description="最低重要度 (1-3)"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(Article)

    if target:
        q = q.filter(Article.target == target)
    if collection_mode:
        q = q.filter(Article.collection_mode == collection_mode)
    if start_event_date:
        q = q.filter(Article.event_date >= start_event_date)
    if end_event_date:
        q = q.filter(Article.event_date <= end_event_date)
    if start_date:
        q = q.filter(Article.publish_date >= start_date)
    if end_date:
        q = q.filter(Article.publish_date <= end_date)
    if is_llm_processed is not None:
        q = q.filter(Article.is_llm_processed == is_llm_processed)
    if min_rating is not None:
        q = q.filter(
            Article.llm_analysis["rating"].astext.cast(Integer) >= min_rating
        )

    q = q.order_by(Article.publish_date.desc())
    total = q.count()
    rows = q.offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [
            {
                "id": r.id,
                "target": r.target,
                "collection_mode": r.collection_mode,
                "event_date": r.event_date,
                "publish_date": r.publish_date,
                "fetched_at": r.fetched_at,
                "title": r.title,
                "source_domain": r.source_domain,
                "url": r.url,
                "body": r.body,
                "is_llm_processed": r.is_llm_processed,
                "llm_analysis": r.llm_analysis,
            }
            for r in rows
        ],
    }


@app.get("/api/v1/stats")
def get_stats(db: Session = Depends(get_db)):
    rows = (
        db.query(
            Article.target,
            Article.collection_mode,
            func.count(Article.id).label("total"),
            func.sum(cast(Article.is_llm_processed, Integer)).label("llm_processed"),
            func.max(Article.publish_date).label("latest"),
        )
        .group_by(Article.target, Article.collection_mode)
        .all()
    )

    return [
        {
            "target": r.target,
            "collection_mode": r.collection_mode,
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
