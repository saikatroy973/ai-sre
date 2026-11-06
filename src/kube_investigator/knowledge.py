"""Read-only search over the bundled Kubernetes documentation.

The corpus is the upstream ``kubernetes/website`` English documentation
(``content/en/docs``, CC BY 4.0), kept outside the repository in
``.data/k8s-docs`` and indexed into SQLite FTS5.  Nothing here is generated at
import time: the MCP tool reports that the index is missing instead of guessing.

Build the index with::

    python scripts/index_kubernetes_docs.py
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

DOCS_URL = "https://kubernetes.io/docs/"

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_SHORTCODE = re.compile(r"\{\{[<%].*?[>%]\}\}", re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BLANK = re.compile(r"\n{3,}")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")

# Words too common to narrow an FTS query.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
        "you", "your", "can", "not", "but", "has", "have", "had", "its", "into",
        "when", "why", "how", "what", "which", "who", "will", "would", "should",
        "may", "might", "must", "does", "did", "doing", "been", "being", "about",
        "there", "their", "then", "than", "them", "these", "those", "use", "used",
        "using", "one", "two", "also", "any", "all", "each", "more", "most",
    }
)


def index_path(data_dir: Path) -> Path:
    override = os.getenv("INVESTIGATOR_DOCS_INDEX")
    if override:
        return Path(override).expanduser()
    return data_dir / "k8s-docs" / "docs.sqlite3"


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "-")):
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values, text[match.end() :]


def _clean(text: str) -> str:
    text = _COMMENT.sub(" ", text)
    text = _SHORTCODE.sub(" ", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = _BLANK.sub("\n\n", text)
    return text.strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _url_for(relative: Path, heading: str) -> str:
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "_index":
        parts = parts[:-1]
    base = DOCS_URL + "/".join(parts)
    return f"{base}/#{_slug(heading)}" if heading else f"{base}/"


def _chunks(body: str, size: int = 1200) -> list[str]:
    body = body.strip()
    if not body:
        return []
    if len(body) <= size:
        return [body]
    pieces: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", body):
        if len(current) + len(paragraph) + 2 > size and current:
            pieces.append(current.strip())
            current = ""
        if len(paragraph) > size:
            for start in range(0, len(paragraph), size):
                pieces.append(paragraph[start : start + size].strip())
        else:
            current = f"{current}\n\n{paragraph}".strip()
    if current:
        pieces.append(current)
    return [piece for piece in pieces if piece]


def parse_document(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return ``(title, [(heading, body), ...])`` for one Markdown document."""
    meta, body = _front_matter(text)
    title = meta.get("title", "")
    sections: list[tuple[str, str]] = []
    matches = list(_HEADING.finditer(body))
    if not matches:
        cleaned = _clean(body)
        return title or "Kubernetes documentation", [("", cleaned)] if cleaned else []
    first_heading = matches[0].group(2)
    title = title or first_heading
    preamble = _clean(body[: matches[0].start()])
    if preamble:
        sections.append(("", preamble))
    for index, match in enumerate(matches):
        heading = match.group(2).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        content = _clean(body[match.end() : end])
        if content:
            sections.append((heading, content))
    return title, sections


SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    url TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, heading, body,
    content='chunks', content_rowid='id',
    tokenize='porter unicode61'
);
"""


def build_index(source: Path, index: Path, *, reset: bool = True) -> dict[str, int]:
    """Index every Markdown file under ``source`` into an FTS5 database."""
    if not source.is_dir():
        raise FileNotFoundError(f"no Kubernetes docs source at {source}")
    index.parent.mkdir(parents=True, exist_ok=True)
    if reset and index.exists():
        index.unlink()
    connection = sqlite3.connect(index)
    try:
        connection.executescript(SCHEMA)
        documents = 0
        rows = 0
        for path in sorted(source.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            title, sections = parse_document(text)
            relative = path.relative_to(source)
            documents += 1
            for heading, body in sections:
                for piece in _chunks(body):
                    connection.execute(
                        "INSERT INTO chunks (path, title, heading, body, url) VALUES (?,?,?,?,?)",
                        (
                            str(relative),
                            title,
                            heading,
                            piece,
                            _url_for(relative, heading),
                        ),
                    )
                    rows += 1
        connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        connection.commit()
        return {"documents": documents, "chunks": rows}
    finally:
        connection.close()


def search_docs(data_dir: Path, query: str, limit: int = 5) -> list[dict[str, str]]:
    """Return up to ``limit`` documentation passages for ``query`` (read-only)."""
    path = index_path(data_dir)
    if not path.is_file():
        return []
    terms = [word.lower() for word in _WORD.findall(query)]
    terms = [word for word in terms if word not in _STOPWORDS][:12]
    if not terms:
        return []
    limit = max(1, min(int(limit), 10))
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for operator in (" AND ", " OR "):
            expression = operator.join(f'"{term}"' for term in terms)
            try:
                rows = connection.execute(
                    """
                    SELECT chunks.title, chunks.heading, chunks.url,
                           snippet(chunks_fts, 2, '[', ']', ' ... ', 24) AS passage
                    FROM chunks_fts
                    JOIN chunks ON chunks.id = chunks_fts.rowid
                    WHERE chunks_fts MATCH ?
                    ORDER BY bm25(chunks_fts) - CASE
                        WHEN chunks.path LIKE 'concepts/%'
                          OR chunks.path LIKE 'tasks/%' THEN 1.0
                        ELSE 0.0
                    END
                    LIMIT ?
                    """,
                    (expression, limit),
                ).fetchall()
            except sqlite3.Error:
                rows = []
            if rows:
                return [
                    {
                        "title": str(title),
                        "heading": str(heading),
                        "url": str(url),
                        "passage": " ".join(str(passage).split()),
                    }
                    for title, heading, url, passage in rows
                ]
        return []
    finally:
        connection.close()
