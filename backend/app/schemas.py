from datetime import datetime
from pydantic import BaseModel, Field


class JobOut(BaseModel):
    id: int
    source_url: str
    platform: str
    title: str
    company_or_poster: str | None = None
    raw_snippet: str | None = None
    description: str | None = None
    type: str | None = None
    pay_text: str | None = None
    pay_min: float | None = None
    pay_max: float | None = None
    pay_period: str | None = None
    experience_level: str | None = None
    location: str | None = None
    remote: bool | None = None
    skills: list[str] | None = None
    posted_at: datetime | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    source_key: str
    is_real_job: bool
    tags: list[str] = []

    class Config:
        from_attributes = True


class ExtractedJob(BaseModel):
    """What the LLM extractor returns per page."""
    is_real_job: bool = True
    title: str = ""
    company_or_poster: str | None = None
    pay_text: str | None = None
    pay_min: float | None = None
    pay_max: float | None = None
    pay_period: str | None = None  # hour | project | year
    type: str | None = None        # contract | full_time | hourly | fixed | social_post
    experience_level: str | None = None
    location: str | None = None
    remote: bool | None = None
    skills: list[str] = Field(default_factory=list)
    apply_url: str | None = None
    posted_at: datetime | None = None
    raw_snippet: str | None = None
    description: str | None = None
    usage: dict | None = None      # {"model", "input_tokens", "output_tokens"}; set by the Claude extractor


class SearchResult(BaseModel):
    """One hit from the Google-search layer."""
    url: str
    title: str
    snippet: str
    source_query: str   # the query that found it
    platform: str = "web"
