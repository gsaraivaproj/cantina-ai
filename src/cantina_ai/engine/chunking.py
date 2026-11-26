"""Splits a markdown note into chunks, the unit a search returns.

Too big, and a chunk's embedding becomes an "average" of many subjects that
matches no question well. Too small, and it loses its context. So:

1. Cut at the headings: each section of the note is a chunk.
2. A section over `max_tokens` is split by paragraph, repeating the last
   paragraph of a piece at the start of the next (a small overlap).
3. A section that is only a heading or a line joins the section after it; the
   chunk is then labelled with both headings ("Stack + Repositório").
4. Code blocks and tables are not cut in the middle, unless one alone is bigger
   than `max_tokens`: then it is cut between lines (a code block is re-fenced).
5. A trailing navigation footer ("---" and bare [[wikilinks]], like a "back to
   index" link) is dropped: it is in almost every note and says nothing.
6. Each chunk gets a context header ("Note > Section > Subsection") before it is
   embedded, so "run launchctl load" still knows what it is about.
"""

import re
from dataclasses import dataclass

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# A line that is only a wikilink, e.g. "[[_index|← voltar]]" (a bullet "- [[x]]" is content).
BARE_WIKILINK = re.compile(r"^\s*\[\[[^\]]+\]\]\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
# Sections smaller than this are merged into the next one.
MIN_TOKENS = 30


@dataclass(frozen=True)
class Chunk:
    heading_path: tuple[str, ...]
    content: str
    embedded_text: str
    token_estimate: int


def estimate_tokens(text: str) -> int:
    """Rough count (~4 characters per token). Good enough to size chunks."""
    return max(1, len(text) // 4)


def note_title(text: str, path: str) -> str:
    """The first H1 of the note, or its file name."""
    for line in _strip_frontmatter(text).splitlines():
        match = HEADING.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2)
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def chunk_note(text: str, title: str, max_tokens: int = 500) -> list[Chunk]:
    sections = _sections(_strip_footer(_strip_frontmatter(text)), title)
    chunks: list[Chunk] = []
    pending = ""  # tiny sections waiting to be merged into the next one
    pending_titles: list[str] = []
    for i, (path, body) in enumerate(sections):
        if not body:
            continue
        is_last = i == len(sections) - 1
        if estimate_tokens(pending + body) < MIN_TOKENS and not is_last:
            # Keep the small section's own heading: the next section's path won't have it.
            labeled = f"**{path[-1]}**\n{body}" if path else body
            pending = f"{pending}\n\n{labeled}" if pending else labeled
            if path:
                pending_titles.append(path[-1])
            continue
        if pending:
            body = f"{pending}\n\n{body}"
            path = _merged_path(pending_titles, path)
        pending, pending_titles = "", []
        for piece in _split(body, max_tokens):
            chunks.append(_make_chunk(title, path, piece))
    if pending:
        last_path = sections[-1][0] if sections else ()
        chunks.append(_make_chunk(title, _merged_path(pending_titles, last_path), pending))
    return chunks


def _merged_path(titles: list[str], path: tuple[str, ...]) -> tuple[str, ...]:
    """The path of a chunk made of several sections: "A + B" in place of the last heading."""
    if not titles:
        return path
    last = [path[-1]] if path and path[-1] not in titles else []
    return (*path[:-1], " + ".join([*titles, *last]))


def _make_chunk(title: str, path: tuple[str, ...], content: str) -> Chunk:
    header = " > ".join((title, *path))
    embedded = f"{header}\n\n{content}"
    return Chunk(path, content, embedded, estimate_tokens(embedded))


def _strip_footer(text: str) -> str:
    """Drop the trailing lines that are only "---", blank, or a bare [[wikilink]]."""
    lines = text.rstrip().splitlines()
    while lines and (
        not lines[-1].strip() or lines[-1].strip() == "---" or BARE_WIKILINK.match(lines[-1])
    ):
        lines.pop()
    return "\n".join(lines)


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def _sections(text: str, title: str) -> list[tuple[tuple[str, ...], str]]:
    """(heading path, body) per section. Headings inside code blocks don't count."""
    sections: list[tuple[tuple[str, ...], str]] = []
    stack: list[tuple[int, str]] = []
    lines: list[str] = []
    in_code = False

    def flush() -> None:
        sections.append((tuple(h for _, h in stack), "\n".join(lines).strip()))
        lines.clear()

    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        match = None if in_code else HEADING.match(line)
        if not match:
            lines.append(line)
            continue
        flush()
        level, heading = len(match.group(1)), match.group(2)
        while stack and stack[-1][0] >= level:
            stack.pop()
        # The H1 that is the note's title is already in every context header.
        if not (level == 1 and heading == title):
            stack.append((level, heading))
    flush()
    return sections


def _blocks(body: str) -> list[str]:
    """Paragraphs, with each code block and each table kept whole."""
    blocks: list[str] = []
    current: list[str] = []
    in_code = False
    for line in body.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        if not in_code and not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _split_lines(block: str, max_tokens: int) -> list[str]:
    """Last resort for a single block bigger than max_tokens (e.g. a 70-line list
    with no blank line): cut between lines. A cut code block is re-fenced."""
    lines = block.splitlines()
    fence = lines[0].strip() if FENCE.match(lines[0]) else None
    if fence:
        lines = lines[1:-1] if FENCE.match(lines[-1]) else lines[1:]
    pieces: list[str] = []
    current: list[str] = []
    for line in lines:
        if current and estimate_tokens("\n".join([*current, line])) > max_tokens:
            pieces.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        pieces.append("\n".join(current))
    return [f"{fence}\n{p}\n```" for p in pieces] if fence else pieces


def _split(body: str, max_tokens: int) -> list[str]:
    if estimate_tokens(body) <= max_tokens:
        return [body]
    pieces: list[str] = []
    current: list[str] = []
    blocks: list[str] = []
    for block in _blocks(body):
        oversized = estimate_tokens(block) > max_tokens
        blocks.extend(_split_lines(block, max_tokens) if oversized else [block])
    for block in blocks:
        if current and estimate_tokens("\n\n".join([*current, block])) > max_tokens:
            pieces.append("\n\n".join(current))
            # Overlap: carry the last paragraph over, unless it is itself large.
            last = current[-1]
            current = [last] if estimate_tokens(last) <= max_tokens // 4 else []
        current.append(block)
    if current:
        pieces.append("\n\n".join(current))
    return pieces
