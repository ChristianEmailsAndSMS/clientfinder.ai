"""The password field's accessible name must not include the Show/Hide toggle."""
from html.parser import HTMLParser
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "app" / "static" / "site"


class P(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs, self.buttons = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("type") == "password":
            self.inputs.append(a)
        if tag == "button" and "data-show" in a:
            self.buttons.append(a)


def test_password_inputs_named_and_toggle_has_state():
    for page in ("login.html", "home.html"):
        p = P()
        p.feed((SITE / page).read_text())
        assert len(p.inputs) == 2 and len(p.buttons) == 2
        for i in p.inputs:
            assert i.get("aria-label") == "Password"
        for b in p.buttons:
            assert b.get("aria-label") and b.get("aria-pressed") == "false"
    assert "aria-pressed" in (SITE / "login.js").read_text()
