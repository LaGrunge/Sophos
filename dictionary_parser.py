import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Line number where articles begin (after TOC, authors list, and preface).
_ARTICLES_START_LINE = 1641

# Two patterns for article headers:
#
# 1) Indented article (the bulk of entries):
#    "    TITLE (optional latin/dates) — definition..."
#    "    TITLE (optional latin/dates) - definition..."
#    Requires 2-6 leading spaces.
_INDENTED_HEADER_RE = re.compile(
    r"^\s{2,6}"                             # required indent
    r"(?P<raw_title>[A-ZА-ЯЁ\"].+?)"       # title (lazy)
    r"\s*(?:—|-)\s+"                        # em-dash or hyphen separator
    r"(?P<body>.+)",                        # definition text
)

# 2) Cross-reference at column 0 (no indent):
#    "АВЕРРОЭС — см. ИБН РУШД"
_XREF_RE = re.compile(
    r"^(?P<raw_title>[A-ZА-ЯЁ][A-ZА-ЯЁa-zа-яё\s]+?)"
    r"\s*(?:—|-)\s+"
    r"(?P<body>см\.\s*.+)",
)


def _match_header(line: str) -> re.Match | None:
    """Try to match a line as an article header."""
    return _INDENTED_HEADER_RE.match(line) or _XREF_RE.match(line)


def _extract_title(raw: str) -> str:
    """Clean the raw title: strip parenthetical groups and trailing noise."""
    # Remove everything starting from the first parenthetical group.
    # e.g. "АВГУСТИН БЛАЖЕННЫЙ (Augustinus Sanctus) (354-430) Аврелий"
    #   -> "АВГУСТИН БЛАЖЕННЫЙ"
    title = re.sub(r"\s*\(.*$", "", raw)
    return title.strip()


def parse_dictionary(path: str | Path = "dictionary.txt") -> dict[str, str]:
    """Parse the philosophy dictionary file into {title: article_text}.

    Returns a dict where keys are article titles (stripped, original case)
    and values are the full article text (including the definition line and
    any continuation lines, but excluding the trailing author line).
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()

    # Take only lines after the TOC / preface section.
    body_lines = lines[_ARTICLES_START_LINE - 1 :]

    articles: dict[str, str] = {}
    current_title: str | None = None
    current_body_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_title, current_body_lines
        if current_title is None:
            return
        # Drop trailing empty lines.
        while current_body_lines and not current_body_lines[-1].strip():
            current_body_lines.pop()
        # The last non-empty line is usually the author — drop it.
        if current_body_lines:
            last = current_body_lines[-1].strip()
            # Author lines are short, have no em-dash, and look like names.
            if len(last) < 120 and "—" not in last:
                current_body_lines.pop()
        # Remove any remaining trailing blanks.
        while current_body_lines and not current_body_lines[-1].strip():
            current_body_lines.pop()

        text = "\n".join(line.strip() for line in current_body_lines).strip()
        if text:
            articles[current_title] = text
        current_title = None
        current_body_lines = []

    for line in body_lines:
        stripped = line.strip()

        if not stripped:
            if current_title is not None:
                current_body_lines.append(line)
            continue

        m = _match_header(line)
        if m:
            _flush()
            current_title = _extract_title(m.group("raw_title"))
            current_body_lines = [line]
        else:
            if current_title is not None:
                current_body_lines.append(line)

    _flush()

    logger.info("Parsed %d articles from %s", len(articles), path)
    return articles
