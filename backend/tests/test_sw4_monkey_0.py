"""The account menu must stay on screen when the app header wraps on phones."""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "site" / "site.css").read_text()


def _media_block(query: str) -> str:
    start = CSS.index("@media (max-width:" + query + ")")
    i = CSS.index("{", start)
    depth, j = 0, i
    while True:
        depth += CSS[j] == "{"
        depth -= CSS[j] == "}"
        j += 1
        if depth == 0:
            return CSS[i:j]


def test_account_menu_anchored_to_header_when_wrapped():
    block = _media_block("960px")
    # the avatar sits mid-screen once the header wraps, so the popover may not be anchored to it
    assert re.search(r"\.apphead \.menu\{position:static\}", block)
    pop = re.search(r"\.apphead \.menu \.pop\{([^}]*)\}", block)
    assert pop, "popover needs a mobile rule"
    assert "right:" in pop.group(1) and "max-width:calc(100vw" in pop.group(1)
