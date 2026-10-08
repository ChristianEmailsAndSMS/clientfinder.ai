from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, Text, Boolean, ForeignKey, UniqueConstraint, Index, JSON, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Source(Base):
    """A data source (SerpAPI query, RemoteOK, ProBlogger, etc.)."""
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)   # e.g. "serpapi:hiring_copywriter"
    kind: Mapped[str] = mapped_column(String(32))               # "google_search" | "direct_api" | "html_scrape" | "rss"
    display_name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)    # query strings, URLs, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    runs: Mapped[list["ScrapeRun"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class ScrapeRun(Base):
    """One execution of a Source. Tracks success/failure for the admin dashboard."""
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | failed
    jobs_added: Mapped[int] = mapped_column(Integer, default=0)
    jobs_updated: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped["Source"] = relationship(back_populates="runs")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_hash", name="uq_jobs_dedupe_hash"),
        # ix_jobs_posted_at / ix_jobs_type / ix_jobs_platform come from index=True on the columns (matches 0001)
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Core identity / dedupe
    dedupe_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str] = mapped_column(Text)                       # the link user clicks
    platform: Mapped[str] = mapped_column(String(64), index=True)       # twitter | reddit | upwork | problogger | greenhouse | ...

    # Content
    title: Mapped[str] = mapped_column(String(512))
    company_or_poster: Mapped[str | None] = mapped_column(String(256), nullable=True)
    raw_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Classification
    type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)   # contract | full_time | hourly | fixed | social_post
    pay_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pay_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    pay_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    pay_period: Mapped[str | None] = mapped_column(String(16), nullable=True)  # hour | project | year
    experience_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    remote: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    skills: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # Dates
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Source attribution
    source_key: Mapped[str] = mapped_column(String(64), index=True)       # which Source produced this
    is_real_job: Mapped[bool] = mapped_column(Boolean, default=True)
    extraction_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SearchQuery(Base):
    """One executed (or failed) Google-layer search. Drives scheduling ("what is due"), the monthly
    search budget, and per-query yield stats."""
    __tablename__ = "search_queries"
    __table_args__ = (Index("ix_search_queries_key_ran", "query_key", "ran_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    query_key: Mapped[str] = mapped_column(String(256))        # full query string incl. site: scope
    provider: Mapped[str] = mapped_column(String(16))          # serpapi | serper
    freshness: Mapped[str | None] = mapped_column(String(4), nullable=True)   # d | w | m
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    results: Mapped[int] = mapped_column(Integer, default=0)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
