import pytest
import app.scrapers.pipeline as P

R = "https://www.reddit.com/r/forhire/comments/abc/x/"
Q = '"copywriter" hiring'


@pytest.mark.parametrize("title,body", [
    ("[HIRING] Copywriter", "I have 3 years of runway left and need someone to write our sales pages. Budget $4k"),
    ("[Hiring] Copywriter", "I have 10 years in this industry. Seeking a copywriter, paid per project"),
    ("[HIRING] Copywriter - long term", "I have 12 years of agency experience. Want to hire a copywriter, $35/hr"),
    ("Seeking copywriter for my agency", "I have 8 years in business and I am seeking a copywriter. Pay $50/hr"),
])
def test_employer_with_years_kept(title, body):
    assert not P.not_a_hiring_post(title, body, Q, R)


def test_job_seeker_still_dropped():
    assert P.not_a_hiring_post("Copywriter available", "I have 6 years of experience. Seeking a copywriter role.", Q, R)
