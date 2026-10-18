"""Turning an email part into the text the archive stores.

WHY THIS FILE EXISTS (NC-142)
-----------------------------
Both email importers — `gmail_import.py` (the Gmail API) and `email_import.py`
(an exported .mbox file) — had their own copy of "get the readable text out",
and both copies silently threw away four different things:

  * everything past 5,000 characters. A longer email was cut off at that limit,
    stopped mid-sentence, and nothing said so.
  * every quoted line (one starting with ">"). That is not only the duplicated
    thread below a reply — it is also anyone who answers *inside* the quote,
    line by line, which is a normal way to reply to several questions at once.
    Those answers were dropped entirely.
  * everything after a line of "--", the signature separator. Plenty of people
    write below their signature, and "--" also occurs in ordinary prose.
  * the whole message, when it had no text/plain part at all. An HTML-only
    email stored its subject and nothing else.

None of it was recorded, so a truncated email is indistinguishable from a short
one and there is no way to find out later which was which.

The rule now is simply: keep what arrived. All of it should always
be read.

WHAT THIS COSTS
---------------
Storing more text does not degrade semantic search, which is the objection the
cap existed to answer. The embedding model is `all-MiniLM-L6-v2` and its
`max_seq_length` is **256 tokens** — roughly a thousand characters — so it has
never read past the first paragraph of a chunk, cap or no cap. What does improve
is keyword search (FTS5 indexes the whole body), what the conversation view
shows, and what Ask can be handed.

The real cost is disk, and email text is small next to everything else the
archive holds.
"""
from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import List

# Tags whose *content* is machinery, not words. Dropped wholesale — a style
# sheet inlined into a marketing email is thousands of characters of noise that
# would otherwise land in the body and in the search index.
_SILENT = {"script", "style", "head", "title", "meta", "link"}

# Tags that end a line of text. Without these every paragraph of an HTML mail
# runs into the next one and the result is unreadable in the conversation view.
_BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
          "blockquote", "section", "article", "header", "footer", "table"}


class _Text(HTMLParser):
    """HTML in, readable plain text out. Deliberately small and stdlib-only.

    This project has no third-party HTML library and is not getting one for an
    importer — `html.parser` is in the standard library and handles the malformed
    markup real email is full of without raising (convert_charrefs is on, so
    entities arrive already decoded).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._muted = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SILENT:
            self._muted += 1
        elif tag in _BREAK:
            self.parts.append("\n")
        elif tag == "img":
            # Keep the alt text: in plenty of newsletters it is the only words
            # attached to the thing being shown.
            alt = dict(attrs).get("alt") or ""
            if alt.strip():
                self.parts.append(f"[{alt.strip()}]")

    def handle_endtag(self, tag):
        if tag in _SILENT:
            self._muted = max(0, self._muted - 1)
        elif tag in _BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._muted:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_to_text(raw: str) -> str:
    """Readable text from an HTML email part.

    Never raises: an importer that dies on one malformed message imports
    nothing, which is a far worse failure than one ugly body.
    """
    if not raw:
        return ""
    try:
        p = _Text()
        p.feed(raw)
        p.close()
        out = p.text()
    except Exception:  # noqa: BLE001 — see the docstring; bad markup is normal
        # Last resort: strip tags with a regex. Worse output, still the words.
        out = unescape(re.sub(r"<[^>]*>", " ", raw))
    return tidy(out)


def tidy(text: str) -> str:
    """Normalise whitespace without removing anything a person wrote.

    The distinction that matters: this collapses *formatting* — runs of blank
    lines, trailing spaces, the non-breaking spaces HTML mail is littered with —
    and never drops a line for what it contains. Quoted lines stay. Text after a
    signature stays. That is the whole point of NC-142.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).rstrip() for ln in text.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)     # at most one blank line in a row
    return out.strip()
