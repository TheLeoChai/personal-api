import os
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy import String, Text, Integer, ForeignKey, DateTime, func, create_engine
from datetime import datetime

# --- DB engine & session ---
# falls back to your compose network hostnames (db:5432)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:postgres@db:5432/postgres",
)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))

class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(220), unique=True)
    body_md: Mapped[str] = mapped_column(Text)
    # Mapped[...] should use Python types (datetime), not SQLAlchemy types
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # server_default needed: onupdate only fires on UPDATE; without a default,
    # INSERT fails with NotNullViolation (this broke POST /api/posts in prod)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Album(Base):
    __tablename__ = "albums"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")

class Photo(Base):
    __tablename__ = "photos"
    id: Mapped[int] = mapped_column(primary_key=True)
    album_id: Mapped[int] = mapped_column(ForeignKey("albums.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(255))
    caption: Mapped[str] = mapped_column(String(255), default="")
