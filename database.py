import os
from datetime import datetime, date
from sqlalchemy import create_engine, Integer, String, DateTime, Boolean, Date, Text, ARRAY
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

    # ターゲット識別
    target: Mapped[str] = mapped_column(String, index=True, nullable=False)
    collection_mode: Mapped[str] = mapped_column(String, nullable=False, default="analyze")  # monitor / analyze

    # 時系列3点アンカー
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)   # LLM抽出（事象発生日）
    publish_date: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)  # GDELT seendate
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)               # 収集実行日時

    # コンテンツ（raw_dataから昇格）
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    source_domain: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    url: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # LLM分析
    is_llm_processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    llm_analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
    print("データベーステーブルが正常に作成されました。")
