"""Reducing a scraped page to the decision it contains.

The brief's transformation step asks for the page stripped of everything that is
not the document -- navigation, header, footer, scripts. What survives is the
title and the decision body: `h1.page-title` and `div.content`, which is exactly
the region highlighted in the brief's own screenshot.

The approach is a whitelist, not a blacklist. Removing known-bad elements means
every new widget the site adds silently ends up in the curated output; keeping
only the two known-good regions means new furniture is excluded by default. The
failure mode of a whitelist is a visible gap, which someone notices. The failure
mode of a blacklist is quiet contamination, which nobody does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

# Never carried into the curated zone even from inside the content region.
# Scripts and styles are not the document, and `<script>` in particular would
# make a stored artifact executable if anyone ever opened it in a browser.
_STRIP_TAGS = ("script", "style", "noscript", "iframe", "object", "embed")

# U+00A0 is in this class deliberately, and is written as an escape rather than
# as the character itself so it is visible to a reader. The site pads for visual
# alignment with runs of non-breaking spaces -- 32 of them inside a single
# representative's name on one WRC page. Left in, they survive into the curated
# text and defeat any exact match on that name, while being indistinguishable
# from ordinary spaces on screen.
_WHITESPACE = re.compile("[ \t\r\f\v\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class CleanedDocument:
    """The curated form of one decision page."""

    html: str
    text: str
    title: str

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def clean_html(raw_html: str) -> CleanedDocument:
    """Strip a scraped page down to its decision.

    Returns both a minimal HTML rendering and a plain-text one. The HTML keeps
    the document's structure -- headings, tables of parties, paragraph breaks --
    which matters because a decision's meaning partly lives in that structure.
    The text form exists for search, diffing and word counts, where markup is
    noise.
    """
    soup = BeautifulSoup(raw_html, "lxml")

    title_node = soup.select_one("h1.page-title")
    title = _collapse(title_node.get_text(" ", strip=True)) if title_node else ""

    content = soup.select_one("div.content")

    # Build a fresh document rather than pruning the original. Pruning leaves
    # behind whatever the whitelist did not think to remove; constructing from
    # scratch means only what was explicitly copied can appear.
    output = BeautifulSoup("<article></article>", "lxml")
    article = output.article
    assert article is not None  # constructed immediately above

    if title:
        heading = output.new_tag("h1")
        heading.string = title
        article.append(heading)

    if content is not None:
        cleaned_content = _sanitise(content)
        article.append(cleaned_content)

    return CleanedDocument(
        html=str(article),
        text=_plain_text(article),
        title=title,
    )


def _sanitise(node: Tag) -> Tag:
    """Copy a node with executable and presentational elements removed."""
    # Re-parsing is the cheapest reliable deep copy here, and it also normalises
    # any malformed markup the site served rather than propagating it.
    copy = BeautifulSoup(str(node), "lxml")
    for tag_name in _STRIP_TAGS:
        for element in copy.find_all(tag_name):
            element.decompose()

    # Event handlers and inline styles are presentation, not content, and an
    # `onclick` surviving into a stored artifact is a small liability for no
    # benefit.
    for element in copy.find_all(True):
        for attribute in list(element.attrs):
            if attribute.startswith("on") or attribute == "style":
                del element[attribute]

    body = copy.body
    result = body.find(node.name) if body else None
    return result if isinstance(result, Tag) else node


def _plain_text(node: Tag) -> str:
    """Render to text, preserving paragraph breaks but not markup.

    Block-level separation is kept because a decision's headings and table rows
    become meaningless run together -- "Date of Hearing:16/01/2024Procedure:"
    is worse than useless for search.
    """
    text = node.get_text("\n", strip=True)
    text = _WHITESPACE.sub(" ", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def _collapse(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()
