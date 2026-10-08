"""Where a job was found: a real job board, a social network post, or a general web page."""
from __future__ import annotations

BOARDS = frozenset({"remoteok", "weworkremotely", "remotive", "greenhouse", "lever", "ashby", "problogger", "indeed", "upwork",
                    "mediabistro", "wellfound", "workable", "smartrecruiters", "linkedin_jobs"})
SOCIAL = frozenset({"reddit", "twitter", "x", "linkedin", "facebook", "instagram", "threads", "tiktok", "youtube", "bluesky", "mastodon"})

KIND_LABEL = {"board": "Job boards", "social": "Social media", "web": "Other websites"}


def kind_of(platform: str | None) -> str:
    p = (platform or "").lower()
    return "board" if p in BOARDS else "social" if p in SOCIAL else "web"


def platforms_of(kind: str, known: list[str]) -> list[str]:
    return [p for p in known if kind_of(p) == kind]
