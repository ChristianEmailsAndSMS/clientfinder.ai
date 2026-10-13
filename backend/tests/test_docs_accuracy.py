"""Written claims must match the code. These tests read .env.example and (when the repo root is present) the docs."""
import collections
import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.config import Settings

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
ENV_EXAMPLE = BACKEND / ".env.example"
LINE = re.compile(r"^(#.*|[A-Z_][A-Z0-9_]*=[^ ,]*|)$")        # comment, blank, or KEY=value with no prose after it


def _same(example: str, default) -> bool:
    if isinstance(default, bool):
        return example.strip().lower() in (("1", "true") if default else ("0", "false", ""))
    if isinstance(default, (int, float)):
        return float(example) == float(default)
    return example == (default or "")


def test_env_example_has_no_garbled_or_duplicate_lines():
    lines = ENV_EXAMPLE.read_text().splitlines()
    bad = [l for l in lines if not LINE.match(l)]
    assert not bad, f"lines that are not KEY=value or a comment: {bad}"
    keys = [l.split("=")[0] for l in lines if re.match(r"^[A-Z_][A-Z0-9_]*=", l)]
    dups = [k for k, n in collections.Counter(keys).items() if n > 1]
    assert not dups, f"duplicate settings (deploy/security_audit.sh warns about these): {dups}"


def test_env_example_lists_every_setting_with_the_code_default():
    vals = dotenv_values(ENV_EXAMPLE)
    fields = Settings.model_fields
    missing = [f.upper() for f in fields if f.upper() not in vals]
    assert not missing, f"settings in app/config.py but not in .env.example: {missing}"
    unknown = [k for k in vals if k.lower() not in fields]
    assert not unknown, f"settings in .env.example that app/config.py ignores: {unknown}"
    diff = {k.upper(): (vals[k.upper()], f.default) for k, f in fields.items()
            if k != "dev_fixtures" and not _same(vals[k.upper()], f.default)}
    assert not diff, f"example value differs from the code default: {diff}"


def test_readme_docs_url_is_reachable_with_the_documented_setup():
    readme = ROOT / "README.md"
    if not readme.exists():
        pytest.skip("repo root not present")
    text = readme.read_text()
    if "/docs" in text:
        assert "ENABLE_DOCS" in text, "README sends people to /docs, which is a 404 unless ENABLE_DOCS=1"


def test_docs_do_not_name_sources_that_do_not_exist():
    from app.scrapers.registry import DURABLE
    for name in ("CHECKLIST.md", "README.md"):
        p = ROOT / name
        if not p.exists():
            pytest.skip("repo root not present")
        text = p.read_text()
        for ghost in ("Mediabistro", "Braintrust", "Upwork RSS", "Upwork public RSS"):
            assert ghost not in text or "not built" in text.lower() or ghost.lower() in DURABLE, f"{name} claims source {ghost}"


def test_docs_do_not_mention_the_removed_admin_token_header():
    for name in ("docs/INFRA.md", "CHECKLIST.md"):
        p = ROOT / name
        if not p.exists():
            pytest.skip("repo root not present")
        assert "X-Admin-Token" not in p.read_text(), f"{name} documents X-Admin-Token, which was removed (docs/SECURITY.md finding 9)"


def test_security_doc_states_welcome_credit_risks():
    doc = ROOT / "docs" / "SECURITY.md"
    if not doc.exists():
        pytest.skip("docs not present")
    text = doc.read_text()
    assert "**$0 credit** (`SIGNUP_BONUS_USD`)" not in text        # old claim: credit is off for everyone and farming is prevented
    assert "zero-width" in text and "first come, first served" in text
