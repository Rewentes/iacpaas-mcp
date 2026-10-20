"""Keep shipped examples and relative documentation links usable."""

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_json_examples_are_parseable():
    files = [*ROOT.glob("docs/**/*.json"), *ROOT.glob(".vscode/*.json")]
    assert files
    for path in files:
        json.loads(path.read_text(encoding="utf-8"))


def test_relative_documentation_links_exist():
    for path in [ROOT / "README.md", *ROOT.glob("docs/**/*.md"), *ROOT.glob("ci/*.md")]:
        for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            url = urlsplit(link)
            if url.scheme or url.netloc or not url.path:
                continue
            assert (path.parent / unquote(url.path)).exists(), f"{path}: broken link {link}"
