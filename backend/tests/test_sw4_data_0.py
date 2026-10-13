import pytest

from app.scrapers.common import title_matches
from app.scrapers import lever


@pytest.mark.parametrize("t", [
    "Email & SMS Marketing Manager", "Email and SMS Marketing Manager", "Email/SMS Marketing Specialist",
    "E-mail Marketing Manager", "Copy-writer", "Head of Email", "Email Campaign Manager", "CRO Manager",
    "SMS Marketing Manager", "Landing-page Developer", "Klaviyo Email Developer",
])
def test_target_titles_pass(t):
    assert title_matches(t)


@pytest.mark.parametrize("t", ["Software Engineer", "Email Security Engineer", "Macro Economist", "Micro Services Lead"])
def test_other_titles_rejected(t):
    assert not title_matches(t)


def test_lever_keeps_email_sms_manager():
    res = lever.parse("acme", [{"text": "Email & SMS Marketing Manager", "hostedUrl": "https://jobs.lever.co/x/1"},
                               {"text": "Copywriter", "hostedUrl": "https://jobs.lever.co/x/2"}])
    assert len(res.jobs) == 2
