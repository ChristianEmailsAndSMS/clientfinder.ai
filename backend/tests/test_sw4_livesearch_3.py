import pytest

from app import live_search


@pytest.mark.parametrize("q", ["​​​", "...", "---", "!?! ?!?", "​.​"])
def test_query_without_letters_or_digits_refused(q):
    with pytest.raises(live_search.SearchRefused) as e:
        live_search.clean_query(q)
    assert e.value.status == 422 if hasattr(e.value, "status") else True


def test_normal_queries_still_accepted():
    assert live_search.clean_query("hiring email copywriter") == "hiring email copywriter"
    assert live_search.clean_query("C++") == "C++"
    assert live_search.clean_query("копирайтер") == "копирайтер"
    assert live_search.clean_query("123") == "123"
