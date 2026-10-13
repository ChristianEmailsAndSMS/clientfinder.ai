"""Long unbroken text (URLs, handles) in a job card must wrap, not run off a phone screen."""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "site" / "site.css").read_text()


def _decls(selector: str) -> str:
    out = []
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", CSS):
        if selector in [s.strip() for s in m.group(1).split(",")]:
            out.append(m.group(2))
    return ";".join(out)


def _wraps(selector: str) -> bool:
    d = _decls(selector)
    return bool(re.search(r"overflow-wrap:\s*(anywhere|break-word)", d) or re.search(r"word-break:\s*break-all", d))


def test_job_title_shrinks_and_wraps():
    assert "min-width:0" in _decls(".job .title").replace(" ", "")
    assert _wraps(".job .title")


def test_job_description_and_meta_wrap():
    assert _wraps(".job .desc")
    assert _wraps(".job .meta")
