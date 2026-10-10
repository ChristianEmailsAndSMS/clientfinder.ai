from datetime import datetime
from pydantic import BaseModel, Field, field_validator


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None and not isinstance(item, (dict, list))]


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

    @field_validator("skills", mode="before")
    @classmethod
    def _skills_as_list(cls, value):
        # A single bad JSON value used to 500 the entire GET /jobs response.
        if value is None:
            return None
        return _string_list(value)

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

    @field_validator("skills", mode="before")
    @classmethod
    def _skills_as_list(cls, value):
        return _string_list(value)


class SearchResult(BaseModel):
    """One hit from the Google-search layer."""
    url: str
    title: str
    snippet: str
    source_query: str   # the query that found it
    platform: str = "web"
