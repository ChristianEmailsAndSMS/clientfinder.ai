"""On phones the wrapped three-row app header must scroll away instead of staying pinned over the job list."""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "app" / "static" / "site" / "site.css").read_text()


def _media_block(query: str) -> str:
    start = CSS.index("@media (" + query + ")")
    depth, i = 0, CSS.index("{", start)
    begin = i
    while True:
        if CSS[i] == "{":
            depth += 1
        elif CSS[i] == "}":
            depth -= 1
            if depth == 0:
                return CSS[begin + 1:i]
        i += 1


def test_apphead_not_sticky_on_phones():
    assert re.search(r"\.apphead\{[^}]*position:sticky", CSS)          # desktop keeps the pinned header
    block = _media_block("max-width:960px")
    rule = re.search(r"\.apphead\{([^}]*)\}", block)
    assert rule and "flex-wrap:wrap" in rule.group(1)
    assert "position:static" in rule.group(1)

