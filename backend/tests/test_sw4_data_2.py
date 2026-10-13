import pytest

from app.geo import classify


@pytest.mark.parametrize("text,want", [
    ("Worldwide (excluding US)", "worldwide"),
    ("Worldwide, except United States", "worldwide"),
    ("Remote - Worldwide (no US)", "worldwide"),
    ("Europe only (no US applicants)", "europe_west"),
    ("Remote - Eastern Europe", "europe_east_mea"),
    ("Eastern Europe", "europe_east_mea"),
    ("Great Britain", "europe_west"),
    ("Scandinavia", "europe_west"),
    ("Hamburg", "europe_west"),
    ("Deutschland", "europe_west"),
    ("München", "europe_west"),
    ("Zürich", "europe_west"),
    ("Czechia", "europe_east_mea"),
    ("Türkiye", "europe_east_mea"),
    ("Kraków", "europe_east_mea"),
    ("Brasil", "americas_east"),
    ("México", "americas_west"),
    ("Memphis", "americas_west"),
    ("New Orleans", "americas_west"),
    ("Remote - NZ", "oceania"),
    ("London", "europe_west"),
    ("Remote, US", "americas_west americas_east"),
])
def test_classify(text, want):
    assert classify(text) == want


def test_excluded_only_is_unplaced():
    assert classify("Remote, not US") is None
