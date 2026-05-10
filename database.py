import os
from datetime import datetime
from sqlalchemy import create_engine, Integer, String, DateTime, Boolean, Float
from sqlalchemy.orm import sessionmaker, declarative_base, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# DB接続URL
# 環境変数で提供されない場合は、デフォルトのローカルホスト設定を使用
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://gdelt_user:gdelt_password@localhost:5432/gdelt_db"
)

# SQLAlchemy 2.0 エンジンとセッション
# pool_pre_ping=True は、接続が切断されている場合に自動的に再接続を試みる設定（本番環境で重要）
engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Article(Base):
    """
    GDELTから取得した記事を表すモデル
    """
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_name: Mapped[str] = mapped_column(String, index=True, nullable=False)
    publish_date: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    url: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    
    # LLM処理フィールド (フェーズ 2/3)
    is_llm_processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    llm_is_relevant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    llm_tone_score: Mapped[float | None] = mapped_column(Float, nullable=True)

def init_db():
    """データベースにテーブルが存在しない場合は、すべてのテーブルを作成します。"""
    Base.metadata.create_all(bind=engine)

if __name__ == "__main__":
    init_db()
    print("データベーステーブルが正常に作成されました。")
