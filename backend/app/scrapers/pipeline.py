"""Orchestrator: Google search → fetch → LLM extract → persist.
Idempotent — reruns upsert on dedupe_hash."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from ..db import session_scope
from ..models import FailedUrl, Job, Source, ScrapeRun
from ..schemas import SearchResult, ExtractedJob
from ..dedup import dedupe_hash
from ..tagging import set_tags
from ..config import settings
from . import google_search, page_fetcher, llm_extractor
from ..security_utils import redact, safe_job_url
from .common import clip
from .llm_extractor import ExtractionError

log = logging.getLogger(__name__)

# Advice, self-promotion, and careers indexes. "We're hiring" stays. "When I'm hiring, I look for" does not.
_SELF_PROMO = re.compile(r"^\s*\[?\s*(for hire|hire me)\b", re.I)
_CAREERS_INDEX = re.compile(r"^\s*(careers at|all open positions)\b", re.I)
# "Copywriter Jobs Board" and "Creative Strategist Job Board" are directories, not one opening.
# "post it on our job board" in a description stays.
_DIRECTORY_TITLE = re.compile(r"\bjobs? board\b", re.I)
_DIRECTORY = re.compile(r"\b\d+\s+writer jobs\b|\bjobs exclusive to paid subscribers\b", re.I)
# "Remote-First Marketing Jobs" and a snippet of "Browse Marketing jobs" are category pages.
_JOBS_INDEX_TITLE = re.compile(r"\bremote-first\b.{0,40}\bjobs\s*$", re.I)
_BROWSE_JOBS = re.compile(r"\bbrowse\b.{0,40}\bjobs\b", re.I)
# "Freelance and remote Copywriters jobs" / "Browse the latest ... jobs" is a board index.
# "Browse Marketing jobs" is already covered above. A single freelance copywriter opening stays.
_BROWSE_LATEST_JOBS = re.compile(
    r"^\s*freelance and remote\b.{0,60}\bjobs\b"
    r"|\bbrowse the latest\b.{0,80}\bjobs\b",
    re.I,
)
# "9000+ Copywriter jobs in United States" is a search-results page, not one opening.
_JOB_COUNT_INDEX = re.compile(r"^\s*\d[\d,]*\+?\s+.{0,80}\bjobs\b", re.I)
# "Creative Strategist Jobs in Manhattan, NY (3403)" is the same list with the count at the end.
# A year in parentheses, such as "(2026)", is not a count.
_JOB_COUNT_SUFFIX = re.compile(
    r"\bjobs\b.{0,60}\(\s*(?:\d{1,3}(?:,\d{3})+|(?!20\d\d\b)\d{3,})\s*\)\s*$",
    re.I,
)
# "$20-$72/hr Funnel Jobs (NOW HIRING)" and "Email Marketing Jobs in New Jersey (NOW HIRING)"
# are lists. A place can sit between jobs and the parenthesis. Title only.
_AGGREGATOR_TITLE = re.compile(r"\bjobs\b.{0,40}\(\s*now hiring\s*\)", re.I)
# "Remote Copywriter Jobs ($69K to $103K)" / "Search 216 copywriter remote jobs" is a list.
# "$26-$54/hr No Experience Copywriting Jobs in Royal Oak" is the same list with the pay first.
_SALARY_JOBS_INDEX = re.compile(
    r"^\s*(?:remote\s+)?.{0,40}\bjobs\s*\(\s*\$"
    r"|\bsearch\s+\d[\d,]*\s+.{0,60}\bjobs\b"
    r"|^\s*\$\d[\d,]*\s*-\s*\$\d[\d,]*\s*/\s*h(?:r|our)\b.{0,80}\bjobs\b",
    re.I,
)
# "Top 38 Email copywriter Jobs" and "Top Copywriter Jobs in NYC" are result lists.
_TOP_N_JOBS = re.compile(
    r"^\s*top\s+\d[\d,]*\s+.{0,60}\bjobs\b"
    r"|^\s*top\s+[a-z]+\s+jobs\b",
    re.I,
)
# "Creative Strategy Jobs – Reduced Hours (2026)" is a category page. A job that offers reduced hours stays.
_REDUCED_HOURS_INDEX = re.compile(r"\bjobs\b.{0,30}\breduced hours\b", re.I)
# "Copywriter Jobs in October 2026" and "Copywriting Jobs (October 2026)" are monthly lists.
_DATED_JOBS_INDEX = re.compile(
    r"\bjobs\s+in\s+(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+20\d\d\b"
    r"|\bjobs\s*\(\s*(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+20\d\d\s*\)",
    re.I,
)
# "Creative Strategist Jobs in Atlanta" and "Viral Marketing Copywriter Jobs in Greenville" are city lists.
# The role does not have to be the first words. A single opening titled as the role stays.
_JOBS_IN_PLACE = re.compile(
    r"\b(?:copywriters?|copywriting|creative strategists?|funnel builders?|"
    r"email marketers?|email copywriters?|landing pages?)\s+jobs\s+in\b",
    re.I,
)
# "Copywriter Jobs in Miami (492 Open Roles)" and a title that is only "Creative strategist jobs",
# "Remote Copywriting Jobs", or "Email Copywriter Remote Jobs". A single opening stays.
_OPEN_ROLES_TITLE = re.compile(
    r"\(\s*\d[\d,]*\s+open roles\s*\)"
    r"|^\s*(?:remote\s+)?(?:copywriter|copywriting|creative strategist|funnel builder|email marketer|email copywriter|landing page)\s+(?:remote\s+)?jobs\s*$",
    re.I,
)
# Title only. A real post can mention a template in the description.
_JOB_TEMPLATE = re.compile(r"\bjob description template\b", re.I)
# A "hiring landing page" search returns templates, galleries, stock files, and docs. Not openings.
_NOT_A_JOB_PAGE = re.compile(
    r"\bai landing page generator\b|\bdesigns on dribbble\b|\bstock photos\b"
    r"|\bfree for commercial use\b|\blearn how to build a landing page\b"
    r"|\brecruitment landing page best practices\b",
    re.I,
)
_ADVICE = re.compile(
    r"\b(when i(?:'m| am) hiring|why you should hire|why i look for|what i look for when|the rise of the|"
    r"how to hire|how should \w+ hire|how i built|legitimate career path|has anyone made the transition|"
    r"\d+ signs you(?:'re| are)? not)\b",
    re.I,
)
# Title only. A real job can say "how to become" in the description. These headlines are articles.
# "Hiring a creative strategist?" is a question about the work, not an opening. "Hiring a strategist for a brand" is.
_ADVICE_TITLE = re.compile(
    r"\b(?:how to become|how i(?:'d| would) become|checklist for hiring|how to get hired|how can i get hired)\b"
    r"|^\s*seeking advice\b"
    r"|^\s*advice on hiring\b"
    r"|^\s*job searches for\b"
    r"|^\s*should i hire\b"
    r"|^\s*best platform to\b"
    r"|\bwho to hire\b"
    r"|^\s*(?:the\s+)?\d+\s+(?:smart\s+)?(?:questions|things)\b.{0,60}\bhir(?:e|ing)\b"
    r"|^\s*what makes a (?:great|good)\b"
    r"|\bfast[- ]growing job\b"
    r"|^\s*the best\b.{0,80}\b20\d\d\b"
    r"|^\s*best\b.{0,40}\bjobs\b"
    r"|^\s*\d+\s+best\b"
    r"|^\s*(?:the\s+)?\d+\+?\s+(?:the\s+)?best\b"
    r"|^\s*\d+\s+mistakes\b"
    r"|\bscope the role\b"
    r"|^\s*best practices\b"
    r"|\beasy guide\b"
    r"|\bpsa to anyone hiring\b"
    r"|^\s*how to make a\b"
    r"|^\s*how to get into\b"
    r"|^\s*how i got\b"
    r"|^\s*strategy to hire\b"
    r"|\band other job listings\b"
    r"|^\s*is\b.{0,80}\bstill in demand\b"
    r"|^\s*what to look for when hiring\b"
    r"|\bexamples:\s*what works\b"
    r"|^\s*(?:q:\s*)?what does (?:a|an)\b.{0,60}\bdo\b"
    r"|\bfreelancers?\s*-\s*hire\b"
    r"|^\s*hiring an?\b[^?\n]{0,80}\?",
    re.I,
)
# A company can be named "Funnel Builder" (or use that phrase as a tagline) and be hiring an
# engineer. Platform, backend, and fullstack titles are the same miss as "software engineer".
_ENGINEERING_ROLE = re.compile(
    r"\b(?:software|platform|fullstack|full[\s-]stack|backend|front[\s-]?end)\s+engineers?\b"
    r"|\bsoftware engineering\b",
    re.I,
)
# Perspective's product tagline. It is not a funnel-builder job. A title that names the role still stays.
_MOBILE_FUNNEL_PRODUCT = re.compile(r"\bmobile funnel builder\b", re.I)
# Heyflow's product page. "Say hello to the Heyflow funnel builder" is the tool, not an opening.
# A post that is also hiring someone stays.
_FUNNEL_PRODUCT_PAGE = re.compile(
    r"^\s*say hello to\b.{0,80}\bfunnel builder\b"
    r"|\bfunnel builder at your fingertips\b",
    re.I,
)
# Recruit Rooster and Teamdash sell software that builds job-ad landing pages.
# A post that is also hiring someone stays.
_LANDING_PAGE_TOOL = re.compile(
    r"\bcapture job seeker information\b"
    r"|\bwithout a designer or developer\b"
    r"|\blanding page setup wizard\b"
    r"|\bgrow your leads and sales with unbounce\b"
    r"|\bleading landing page platform\b",
    re.I,
)
# "Find freelance funnel builder jobs" is a signup for people offering services.
# "We're hiring a freelance funnel builder" stays.
_FREELANCE_DIRECTORY = re.compile(
    r"^\s*find freelance\b.{0,80}\bjobs\b"
    r"|\blooking for clients as a freelance\b",
    re.I,
)
# "We provide copywriting services" on a writer-jobs page is the agency selling work.
# A post that is also hiring someone stays.
_COPYWRITING_SERVICES = re.compile(r"\bwe provide copywriting services\b", re.I)
# Spearlance sells hiring software. "Google for Jobs schema" and "Own Your Hiring Pipeline"
# are the product, not an opening. A post that is also hiring someone stays.
_HIRING_SOFTWARE = re.compile(
    r"\bgoogle for jobs schema\b|\bown your hiring pipeline\b",
    re.I,
)
# "I have 4+ years" under a #hiring tag is someone selling themselves. "We're hiring" plus that line is still a job.
_FIRST_PERSON_YEARS = re.compile(r"\bi have \d+\+?\s*years\b", re.I)
# "Hi, I'm a copywriter" is the same ad. "Hi, I'm a founder and we're hiring" is not, and neither is
# "Hi everyone, I'm hiring" (that line has no "a/an" before a role).
_FIRST_PERSON_INTRO = re.compile(
    r"\bhi[,!]?\s+i(?:'m| am)\s+(?:a|an)\s+(?:\w+\s+){0,3}?(?:copywriters?|writers?|freelancers?|marketers?|strategists?|builders?)\b",
    re.I,
)
_HIRING_SOMEONE = re.compile(
    r"\b(we(?:'re| are) hiring|we(?:'re| are) looking|looking for (?:a|someone)|now hiring|is hiring)\b",
    re.I,
)
# "is anyone hiring" is a person looking for work, often for a friend. "We're hiring" in the same post is an opening.
_JOB_SEEKER = re.compile(r"\bis anyone hiring\b", re.I)
# An agency selling "we help founders hire a creative strategist" is not an opening.
_RECRUITER_PITCH = re.compile(r"\bwe help\b.{0,80}\b(?:founders|brands|companies)\s+hire\b", re.I)
# "Copywriter Recruitment Agency – Hire ..." sells hiring. A job at an agency stays
# when the title does not also say hire. Title only, so a description mention stays.
_RECRUITMENT_AGENCY_TITLE = re.compile(r"\brecruitment agency\b", re.I)
_HIRE_IN_TITLE = re.compile(r"\bhire\b", re.I)
# "We're a hiring agency that hires skilled copywriters for client companies" is a talent pool.
# A post that is also hiring someone stays.
_HIRING_AGENCY_POOL = re.compile(r"\bhiring agency that hires\b", re.I)
# "Why should I hire a copywriter?" is advice. A post that is also hiring someone stays.
_WHY_HIRE = re.compile(r"\bwhy should i hire\b", re.I)
# "Copywriter Hiring • 500+ reels on Instagram" is a topic page. Title only.
_INSTAGRAM_REEL_INDEX = re.compile(r"\breels on instagram\b", re.I)
# "Free and customizable copywriting templates" is a design gallery. Title only.
_TEMPLATE_GALLERY = re.compile(r"\bcustomizable\b.{0,60}\btemplates\b", re.I)
# "Copywriter Near Me (ID:1608474)" is a generated listing. Title only.
_NEAR_ME_LISTING = re.compile(r"\bnear me\s*\(\s*id\s*:", re.I)
# Ads of the World describes an old ad, not an opening.
_AD_ARCHIVE = re.compile(r"\bthis professional campaign titled\b", re.I)
# "All Job Posts" is a category of many wanted ads. Title only.
_ALL_JOB_POSTS = re.compile(r"^\s*all job posts\b", re.I)
# "A Funnel Builder is a specialized role within a marketing agency" explains the job.
# A post that is also hiring someone stays.
_ROLE_DEFINITION = re.compile(r"\bis a specialized role\b", re.I)
# MultiplyMii offers a free Google Doc and a salary guide for hiring. Not an opening.
# A post that is also hiring someone stays.
_SALARY_GUIDE = re.compile(
    r"\bfree editable google doc\b"
    r"|\bsalary guide to start hiring\b",
    re.I,
)
# "Cost to build a funnel? I checked Fiverr already" is a price question, not an opening.
_PRICE_QUESTION = re.compile(r"^\s*cost to build\b|\bi checked fiverr\b", re.I)
# Remote OK says this when the listing is dead. City jobs says the vacancy has expired.
# Freelancer says "Closed. Posted almost 5 years ago." Reddit says the thread is archived.
# "Please do not apply unless..." on a live post stays.
_CLOSED_JOB = re.compile(
    r"\bjob post is closed\b|\bposition is probably filled\b|\bno longer accepting applications\b"
    r"|\bvacancy has now expired\b"
    r"|\bthis position is now closed\b"
    r"|\bclosed\.\s*posted\b|\bposted almost \d+\s+years ago\b"
    r"|\barchived post\b|\bnew comments cannot be posted\b",
    re.I,
)
# These titles are a different job. A title that also names the searched role still stays.
_OTHER_ROLE_TITLE = re.compile(
    r"\b(?:account managers?|graphics? designers?|performance marketing specialists?)\b",
    re.I,
)


_ROLE_STOPWORDS = frozenset({
    "hiring", "hire", "jobs", "job", "remote", "the", "and", "for", "with", "from", "a", "an",
})


def _role_needles(query: str) -> list[str]:
    """Role words from the Google query, without the site: scope or the word hiring."""
    text = re.sub(r"site:\S+", " ", query or "", flags=re.I)
    phrases = [p.strip().lower() for p in re.findall(r'"([^"]+)"', text)]
    phrases = [p for p in phrases if p and p not in {"hiring", "hire"}]
    if phrases:
        return phrases
    bare = re.sub(r'"', " ", text).lower()
    return [w for w in re.findall(r"[a-z0-9']+", bare) if w not in _ROLE_STOPWORDS and len(w) >= 4]


def _marketplace_index(url: str) -> bool:
    """Category and search pages on hire marketplaces, not one opening."""
    u = (url or "").lower()
    if "upwork.com" in u and "/hire/" in u:
        return True
    if "fiverr.com" in u and "/cat/" in u:
        return True
    if "freelancer.com/job-search/" in u or "freelancer.com/jobs/" in u:
        return True
    # PeoplePerHour /freelance-…-jobs is a category. A project URL has more path after it.
    if "peopleperhour.com" in u:
        path = u.split("peopleperhour.com", 1)[1].split("?", 1)[0].strip("/")
        if path.startswith("freelance-") and path.endswith("-jobs") and "/" not in path:
            return True
    # Toptal /freelance-jobs/ is a network signup, not one opening.
    if "toptal.com/freelance-jobs/" in u:
        return True
    if "roberthalf.com" in u and "/jobs/all/" in u:
        return True
    # Indeed /q-...-jobs.html and SimplyHired /search are result lists, not one opening.
    if "indeed.com" in u and ("/q-" in u or u.split("?", 1)[0].rstrip("/").endswith("-jobs.html")):
        return True
    if "simplyhired.com/search" in u:
        return True
    return False


def not_a_hiring_post(title: str, snippet: str = "", query: str = "", url: str = "") -> bool:
    """True when the search result is commentary, a person advertising themselves, or a careers index."""
    title = title or ""
    blob = f"{title}\n{snippet or ''}"
    if _marketplace_index(url):
        return True
    # Quora is people asking where to hire, not an employer posting an opening.
    if "quora.com" in (url or "").lower():
        return True
    # Instagram /popular and /explore are reel indexes, not one post.
    low_url = (url or "").lower()
    if "instagram.com/popular/" in low_url or "instagram.com/explore/" in low_url or _INSTAGRAM_REEL_INDEX.search(title):
        return True
    if "canva.com/templates" in low_url or "venngage.com/templates" in low_url or _TEMPLATE_GALLERY.search(title):
        return True
    if "vitacv.com" in low_url or "job-near-me" in low_url or _NEAR_ME_LISTING.search(title):
        return True
    # Vaia reprints a title plus "largest job board for Gen-Z" chrome.
    if "vaia.com" in low_url:
        return True
    # Emerging Blue /tag/ pages list many staffing posts, not one opening.
    if "emergingblue.com" in low_url and "/tag/" in low_url:
        return True
    if "adsoftheworld.com" in low_url or _AD_ARCHIVE.search(blob):
        return True
    # A LinkedIn profile is a person, not an opening. Job pages are /jobs/view.
    if "linkedin.com/in/" in low_url or "linkedin.com/pub/" in low_url:
        return True
    # LinkedIn's hiring product explains a landing-page wizard. It is not an opening.
    if "business.linkedin.com/hire/landing-pages" in low_url:
        return True
    if "moderncopywriter.com/category" in low_url or _ALL_JOB_POSTS.search(title):
        return True
    # Expertini reprints one opening across city domains, plus a roles index.
    if "expertini.com" in low_url:
        return True
    # BeBee puts several jobs in one result. The title is often a different role.
    if "bebee.com" in low_url:
        return True
    # Builder Funnel is a remodeler agency. The site and company profiles are not a funnel-builder opening.
    # A post on their site that is also hiring someone stays.
    if "builderfunnel.com" in low_url and not _HIRING_SOMEONE.search(blob):
        return True
    if ("indeed.com/cmp/builder-funnel" in low_url
            or "glassdoor.com/overview/working-at-builder-funnel" in low_url
            or "instagram.com/builder_funnel" in low_url):
        return True
    # ZipRecruiter /e/ pages explain a job title. They are not openings.
    if "ziprecruiter.com/e/" in low_url:
        return True
    # Unbounce's homepage sells the landing page builder. A job elsewhere on the site stays.
    if "unbounce.com" in low_url and low_url.split("unbounce.com", 1)[1].split("?", 1)[0].strip("/") == "":
        return True
    if "recruitment-agency" in (url or "").lower() or _RECRUITER_PITCH.search(blob):
        return True
    if _RECRUITMENT_AGENCY_TITLE.search(title) and _HIRE_IN_TITLE.search(title):
        return True
    if _HIRING_AGENCY_POOL.search(blob) and not _HIRING_SOMEONE.search(blob):
        return True
    if _WHY_HIRE.search(blob) and not _HIRING_SOMEONE.search(blob):
        return True
    # LinkedIn and Equip how-to-hire pages are sample descriptions, not openings.
    if "how-to-hire" in (url or "").lower() or _JOB_TEMPLATE.search(title):
        return True
    if (_SELF_PROMO.search(title) or _CAREERS_INDEX.search(title) or _DIRECTORY_TITLE.search(title)
            or _JOBS_INDEX_TITLE.search(title) or _JOB_COUNT_INDEX.search(title)
            or _JOB_COUNT_SUFFIX.search(title) or _DATED_JOBS_INDEX.search(title)
            or _AGGREGATOR_TITLE.search(title) or _SALARY_JOBS_INDEX.search(blob)
            or _OPEN_ROLES_TITLE.search(title) or _JOBS_IN_PLACE.search(title)
            or _TOP_N_JOBS.search(title) or _REDUCED_HOURS_INDEX.search(title)
            or _DIRECTORY.search(blob) or _BROWSE_JOBS.search(blob)
            or _BROWSE_LATEST_JOBS.search(blob)
            or _ADVICE.search(blob) or _ADVICE_TITLE.search(title) or _PRICE_QUESTION.search(blob)
            or _CLOSED_JOB.search(blob) or _NOT_A_JOB_PAGE.search(blob)):
        return True
    if (_FIRST_PERSON_YEARS.search(blob) or _FIRST_PERSON_INTRO.search(blob) or _JOB_SEEKER.search(blob)) and not _HIRING_SOMEONE.search(blob):
        return True
    # Title only. A real funnel-builder post can mention working with engineers in the body.
    if re.search(r"funnel builder", query or "", re.I) and _ENGINEERING_ROLE.search(title):
        return True
    if (re.search(r"funnel builder", query or "", re.I) and _MOBILE_FUNNEL_PRODUCT.search(blob)
            and "funnel builder" not in title.lower()):
        return True
    if _FUNNEL_PRODUCT_PAGE.search(blob) and not _HIRING_SOMEONE.search(blob):
        return True
    if _LANDING_PAGE_TOOL.search(blob) and not _HIRING_SOMEONE.search(blob):
        return True
    if _FREELANCE_DIRECTORY.search(blob):
        return True
    if _COPYWRITING_SERVICES.search(blob) and not _HIRING_SOMEONE.search(blob):
        return True
    if _HIRING_SOFTWARE.search(blob) and not _HIRING_SOMEONE.search(blob):
        return True
    if _ROLE_DEFINITION.search(blob) and not _HIRING_SOMEONE.search(blob):
        return True
    if _SALARY_GUIDE.search(blob) and not _HIRING_SOMEONE.search(blob):
        return True
    # On Reddit the role often appears only in the related-posts snippet of a different thread.
    # LinkedIn titles are the author's name, so this check stays on reddit.com.
    # A "Freelance Writer" title can still be a copywriter job, so other roles are an explicit list.
    needles = _role_needles(query)
    if "reddit.com" in (url or "").lower():
        if needles and not all(needle in title.lower() for needle in needles):
            return True
    if (_OTHER_ROLE_TITLE.search(title) and needles
            and not all(needle in title.lower() for needle in needles)):
        return True
    return False


def _get_or_create_source(db: Session, key: str, kind: str, display_name: str) -> Source:
    s = db.scalar(select(Source).where(Source.key == key))
    if s:
        return s
    s = Source(key=key, kind=kind, display_name=display_name, enabled=True, config={})
    db.add(s)
    db.flush()
    return s


def result_hash(result: SearchResult) -> str:
    """Identity of a job = the page we found it on, so direct feeds and Google agree and re-runs are free."""
    return dedupe_hash(url=result.url, title=result.title)


def _upsert_job(db: Session, source: Source, result: SearchResult, extracted: ExtractedJob,
                seen_in_batch: set[str], fetched: bool = True) -> tuple[bool, bool, int | None]:
    """Returns (added, updated, job_id)."""
    # The model reads attacker-controlled pages: only ever store a plain http(s) link (never javascript:, data:, etc.)
    url = safe_job_url(extracted.apply_url) or safe_job_url(result.url)
    if url is None:
        return (False, False, None)
    h = result_hash(result)
    if h in seen_in_batch:
        return (False, False, None)
    seen_in_batch.add(h)
    existing = db.scalar(select(Job).where(Job.dedupe_hash == h))
    now = datetime.now(timezone.utc)
    if existing:
        existing.last_seen_at = now
        # Fill in anything we didn't have before
        for field in ("company_or_poster", "pay_text", "pay_min", "pay_max", "pay_period",
                      "type", "experience_level", "location", "remote", "description"):
            v = clip(getattr(extracted, field), field)
            if v is not None and getattr(existing, field) in (None, ""):
                setattr(existing, field, v)
        if extracted.skills and not existing.skills:
            existing.skills = extracted.skills
        set_tags(db, existing)
        return (False, True, existing.id)

    job = Job(
        dedupe_hash=h,
        source_url=url,
        platform=result.platform,
        title=clip(extracted.title or result.title, "title"),
        company_or_poster=clip(extracted.company_or_poster, "company_or_poster"),
        raw_snippet=extracted.raw_snippet or result.snippet,
        description=extracted.description,
        type=clip(extracted.type, "type"),
        pay_text=clip(extracted.pay_text, "pay_text"),
        pay_min=extracted.pay_min,
        pay_max=extracted.pay_max,
        pay_period=clip(extracted.pay_period, "pay_period"),
        experience_level=clip(extracted.experience_level, "experience_level"),
        location=clip(extracted.location, "location"),
        remote=extracted.remote,
        skills=extracted.skills or None,
        posted_at=extracted.posted_at,
        first_seen_at=now,
        last_seen_at=now,
        source_key=source.key,
        is_real_job=extracted.is_real_job,
        extraction_model=(settings.extraction_model if not settings.dev_fixtures else "heuristic-mock"),
        extra={"usage": extracted.usage, "page_fetched": fetched},
    )
    sp = db.begin_nested()
    try:
        db.add(job)
        db.flush()
        set_tags(db, job)
        sp.commit()
        return (True, False, job.id)
    except IntegrityError:
        sp.rollback()
        return (False, True, db.scalar(select(Job.id).where(Job.dedupe_hash == h)))


def _source_key(query: str) -> str:
    key = "google:" + query.lower().strip().replace(" ", "_").replace('"', "")
    if len(key) <= 64:
        return key
    return key[:55] + "_" + hashlib.sha1(key.encode()).hexdigest()[:8]


MAX_FAILURE_ATTEMPTS = 5   # after this many failures a URL is marked dead and no longer auto-requeued


def _record_failure(db: Session, source_key: str, result: SearchResult, error: str) -> None:
    h = result_hash(result)
    now = datetime.now(timezone.utc)
    fu = db.scalar(select(FailedUrl).where(FailedUrl.url_hash == h))
    if fu:
        fu.attempts = (fu.attempts or 0) + 1 if fu.status != "resolved" else 1
        fu.error, fu.last_failed_at = redact(error)[:1000], now
        fu.status = "dead" if fu.attempts >= MAX_FAILURE_ATTEMPTS else "pending"
        return
    db.add(FailedUrl(url_hash=h, url=result.url, title=result.title[:512], snippet=result.snippet,
                     platform=result.platform, source_key=source_key, source_query=result.source_query[:256],
                     error=redact(error)[:1000], attempts=1, status="pending", first_failed_at=now, last_failed_at=now))


def _resolve_failure(db: Session, result: SearchResult) -> None:
    fu = db.scalar(select(FailedUrl).where(FailedUrl.url_hash == result_hash(result)))
    if fu and fu.status != "resolved":
        fu.status = "resolved"


def _process(db: Session, source: Source, result: SearchResult, seen: set[str], stats: dict) -> None:
    h = result_hash(result)
    known = db.scalar(select(Job).where(Job.dedupe_hash == h))
    if known:  # already stored (maybe by a direct feed): no fetch, no Claude call
        if h not in seen:
            known.last_seen_at = datetime.now(timezone.utc)
            stats["updated"] += 1
            stats["job_ids"].append(known.id)
        seen.add(h)
        _resolve_failure(db, result)
        return
    if not_a_hiring_post(result.title, result.snippet, result.source_query, result.url):
        stats["skipped"] += 1
        _resolve_failure(db, result)
        return
    page, fetched = page_fetcher.fetch_page_for_result(result)
    extracted = llm_extractor.extract(result, page)
    _add_usage(stats, extracted.usage)
    if extracted.is_real_job and not_a_hiring_post(extracted.title or "", extracted.raw_snippet or "", result.source_query):
        extracted.is_real_job = False
    if not extracted.is_real_job:
        stats["skipped"] += 1
        _resolve_failure(db, result)
        return
    added, updated, job_id = _upsert_job(db, source, result, extracted, seen, fetched)
    if job_id is not None:
        stats["job_ids"].append(job_id)
    stats["extracted"] += 1
    stats["added"] += int(added)
    stats["updated"] += int(updated)
    _resolve_failure(db, result)


def _run_one(db: Session, source: Source, result: SearchResult, seen: set[str], stats: dict) -> None:
    """One result, isolated in a savepoint so a failure cannot poison the batch; failures are persisted."""
    try:
        with db.begin_nested():
            _process(db, source, result, seen, stats)
    except ExtractionError as e:
        log.warning("skipping %s: %s", result.url, e)
        stats["skipped"] += 1
        if e.usage:                                          # the model WAS called: those tokens cost money, so they are counted
            _add_usage(stats, e.usage)
        _record_failure(db, source.key, result, f"extraction: {e}")
    except Exception as e:
        log.exception("pipeline row failed: %s", e)
        stats["errors"] += 1
        _record_failure(db, source.key, result, f"{type(e).__name__}: {e}")


def _new_stats(query: str) -> dict:
    return {"query": query, "searched": False, "search_hits": 0, "extracted": 0, "added": 0, "updated": 0,
            "skipped": 0, "errors": 0, "tokens_in": 0, "tokens_out": 0, "job_ids": [], "calls": []}


def _add_usage(stats: dict, usage: dict | None) -> None:
    """Count one model call on its own. The long-prompt rate is per call, so calls must not be glued into one prompt."""
    if not usage:
        return
    tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    stats["tokens_in"] += tin
    stats["tokens_out"] += tout
    stats.setdefault("calls", []).append((tin, tout))


def _live_ready(stats: dict, what: str) -> bool:
    if not settings.dev_fixtures and not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set; skipping %s rather than storing unextracted rows", what)
        stats["error"] = "ANTHROPIC_API_KEY not set"
        return False
    return True


def run_pipeline_for_query(query: str, num: int = 10, freshness: str | None = None, *, source_key: str | None = None,
                           source_label: str | None = None, exhausted: set[str] | None = None) -> dict:
    """Execute the full pipeline for one search query. Returns a stats dict.
    stats["searched"] is True only if a search was actually attempted (i.e. may have cost a credit)."""
    stats = _new_stats(query)
    if not _live_ready(stats, repr(query)):
        return stats
    stats["searched"] = True
    try:
        used: list[str] = []
        results = google_search.search(query, num=num, freshness=freshness, exhausted=exhausted, used=used)
        if used:
            stats["provider"] = used[-1]
    except Exception as e:
        log.exception("search failed for %r", query)
        stats["search_error"] = redact(e)
        return stats
    stats["search_hits"] = len(results)
    if not results:
        log.info("no search results for %r (freshness=%s)", query, freshness)
        return stats

    with session_scope() as db:
        source = _get_or_create_source(db, source_key or _source_key(query), "google_search", (source_label or f"Google: {query}")[:128])
        run = ScrapeRun(source_id=source.id, status="running")
        db.add(run)
        db.flush()
        seen: set[str] = set()
        for result in results:
            _run_one(db, source, result, seen, stats)
        run.finished_at = datetime.now(timezone.utc)
        run.status = "ok" if stats["errors"] == 0 else "failed"
        run.jobs_added = stats["added"]
        run.jobs_updated = stats["updated"]
        if stats["errors"]:
            run.error = f"{stats['errors']} rows failed; see failed_urls"
    log.info("query %r: %s", query, stats)
    return stats


def requeue_failed(limit: int = 50, ids: list[int] | None = None) -> dict:
    """Reprocess failed URLs (fetch + extract + store). Costs no search credit, only fetch/Claude.
    Without ids: pending rows, oldest failure first. With ids: those rows even if dead (one more try)."""
    stats = _new_stats("requeue")
    stats["requeued"] = 0
    if not _live_ready(stats, "requeue"):
        return stats
    with session_scope() as db:
        q = select(FailedUrl).order_by(FailedUrl.last_failed_at)
        q = q.where(FailedUrl.id.in_(ids), FailedUrl.status != "resolved") if ids else q.where(FailedUrl.status == "pending")
        rows = db.scalars(q.limit(limit)).all()
        seen: set[str] = set()
        for fu in rows:
            src = db.scalar(select(Source).where(Source.key == fu.source_key)) or _get_or_create_source(
                db, fu.source_key, "google_search", fu.source_key)
            result = SearchResult(url=fu.url, title=fu.title, snippet=fu.snippet, source_query=fu.source_query, platform=fu.platform)
            if fu.status == "dead":
                fu.status, fu.attempts = "pending", MAX_FAILURE_ATTEMPTS - 1   # one more try, then dead again
            stats["requeued"] += 1
            _run_one(db, src, result, seen, stats)
    log.info("requeue: %s", stats)
    return stats


def run_pipeline_for_queries(queries: Iterable[str] | None = None, num: int = 10) -> list[dict]:
    queries = list(queries) if queries else list(google_search.DEFAULT_QUERIES)
    out = []
    exhausted: set[str] = set()
    for q in queries:
        try:
            out.append(run_pipeline_for_query(q, num=num, exhausted=exhausted))
        except Exception as e:  # one bad query (HTTP error, quota) must not stop the rest
            log.exception("query %r failed", q)
            out.append({"query": q, "error": redact(e)})
    return out
