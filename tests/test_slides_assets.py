"""Smoke test: every local asset the slide deck references exists in the repo."""

import os
import re

SLIDES_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "slides")
REF_PATTERN = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']|url\(\s*["']?([^"')]+)["']?\s*\)""")


def _local_refs(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for match in REF_PATTERN.finditer(text):
        ref = (match.group(1) or match.group(2)).split("#")[0].split("?")[0]
        if ref and not re.match(r"^(?:[a-z]+:|//)", ref, re.IGNORECASE):
            yield ref


def _missing(path):
    base = os.path.dirname(path)
    return [ref for ref in _local_refs(path) if not os.path.isfile(os.path.join(base, ref))]


def test_deck_index_exists():
    assert os.path.isfile(os.path.join(SLIDES_DIR, "index.html"))


def test_deck_html_assets_exist():
    assert _missing(os.path.join(SLIDES_DIR, "index.html")) == []


def test_deck_css_assets_exist():
    assert _missing(os.path.join(SLIDES_DIR, "assets", "colors_and_type.css")) == []
