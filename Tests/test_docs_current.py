"""Handoff docs that state a fact about the code must state the true one.

README/AGENTS are what the next person — or the next agent — reads before
touching anything, so a stale claim there is worse than no claim. These are
the facts that have actually drifted: the schema version (README said v8 at
v9) and the card-fee base (docs said credit tips only, long after the fee was
widened to auto-gratuity as well).

Test counts are deliberately NOT asserted here: they were hand-copied into
three files and ended up three different numbers. The fix was to stop quoting
them, so this test guards that they stay unquoted.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()
AGENTS = (ROOT / "AGENTS.md").read_text()

from app.db import SCHEMA_VERSION


class TestSchemaVersion:
    def test_readme_states_the_version_the_code_migrates_to(self):
        stated = re.search(r"applied automatically at boot \(currently \*\*v(\d+)\*\*\)",
                           README)
        assert stated, "README no longer states a schema version — keep it or drop the sentence"
        assert int(stated.group(1)) == SCHEMA_VERSION, (
            f"README says v{stated.group(1)}, code migrates to v{SCHEMA_VERSION}")


class TestNoHandCopiedTestCounts:
    """A number that has to be retyped after every change will be wrong."""

    def test_docs_do_not_quote_a_passing_test_count(self):
        for name, text in (("README.md", README), ("AGENTS.md", AGENTS)):
            hits = re.findall(r"\*\*\d{3,} (?:passing )?tests?\*\*", text)
            assert not hits, f"{name} quotes a test count ({hits}); run make test instead"


class TestCardFeeBaseIsDescribedCorrectly:
    """The fee applies to everything the processor handled — card tips AND
    auto-gratuity (owner 2026-08-14). Docs said 'credit tips only' for two
    weeks after that stopped being true."""

    SOURCES = ["docs/M6-poquitos.md", "app/settings_store.py", "Claude.md"]

    def test_no_source_still_says_the_fee_spares_auto_gratuity(self):
        for rel in self.SOURCES:
            text = (ROOT / rel).read_text().lower()
            assert "cash tips and auto-gratuity untouched" not in text, (
                f"{rel} still describes the superseded credit-tips-only fee base")
