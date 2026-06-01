import os
from datetime import datetime
from sqlalchemy import create_engine, Integer, String, DateTime, Boolean, Text
from sqlalchemy.orm import sessionmaker, declarative_base, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://gdelt_user:gdelt_password@localhost:5432/gdelt_db"
)

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_name: Mapped[str] = mapped_column(String, index=True, nullable=False)  # commodity key (e.g. "copper")
    publish_date: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    url: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)          # スクレイプ済み本文
    is_llm_processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    llm_analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True) # 構造化分析結果


def init_db():
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
    print("データベーステーブルが正常に作成されました。")
