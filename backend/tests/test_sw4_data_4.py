"""A NUL byte in model or page text must not make the whole job fail (PostgreSQL rejects NUL in text)."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Job
from app.schemas import ExtractedJob, SearchResult
import app.scrapers.pipeline as P


def test_nul_bytes_are_stripped_before_insert():
    r = SearchResult(url="https://example.com/nul1", title="Copy\x00writer", snippet="hir\x00ing", platform="web", source_query="q")
    e = ExtractedJob(title="Copy\x00writer", apply_url=r.url, description="hello\x00world", company_or_poster="Ac\x00me",
                     pay_text="$1\x00", location="U\x00S", skills=["a\x00b"])
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        src = P._get_or_create_source(db, "k", "search", "k")
        added, _, jid = P._upsert_job(db, src, r, e, set())
        job = db.get(Job, jid)
        assert added
        for f in ("title", "description", "company_or_poster", "pay_text", "location", "raw_snippet"):
            assert "\x00" not in getattr(job, f), f
        assert job.description == "helloworld"
        assert job.skills == ["ab"]
