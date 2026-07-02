from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bot.config import Settings


SECRET_PATTERNS = (
    re.compile(r"0x[a-fA-F0-9]{64}"),
    re.compile(r"(?i)(private[_ -]?key|secret|passphrase|api[_ -]?key)\s*[:=]\s*\S+"),
)


@dataclass(frozen=True)
class RagSearchResult:
    source_path: str
    title: str
    snippet: str


def now_text() -> str:
    return datetime.now(UTC).isoformat()


def iter_markdown_files(settings: Settings) -> list[Path]:
    roots = list(settings.rag_paths)
    if settings.rag_obsidian_vault_path:
        roots.append(settings.rag_obsidian_vault_path)

    files: list[Path] = []
    for root in roots:
        root = root.expanduser()
        if root.is_file() and root.suffix.lower() == ".md":
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in root.rglob("*.md") if ".git" not in path.parts and ".venv" not in path.parts)
    return sorted(set(files))


def sanitize_content(text: str) -> str:
    sanitized = text
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


def index_markdown(conn: sqlite3.Connection, settings: Settings) -> int:
    count = 0
    for path in iter_markdown_files(settings):
        try:
            stat = path.stat()
            content = sanitize_content(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue

        existing = conn.execute("SELECT mtime FROM rag_documents WHERE source_path = ?", (str(path),)).fetchone()
        if existing and float(existing["mtime"]) == stat.st_mtime:
            continue

        title = extract_title(content, path)
        tags = extract_tags(content)
        conn.execute(
            """
            INSERT INTO rag_documents (source_path, title, content, tags, mtime, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET
              title=excluded.title,
              content=excluded.content,
              tags=excluded.tags,
              mtime=excluded.mtime,
              indexed_at=excluded.indexed_at
            """,
            (str(path), title, content, ",".join(tags), stat.st_mtime, now_text()),
        )
        count += 1
    conn.commit()
    return count


def extract_title(content: str, path: Path) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:180] or path.stem
    return path.stem


def extract_tags(content: str) -> list[str]:
    return sorted(set(match.group(1).lower() for match in re.finditer(r"#([a-zA-Z][\w/-]+)", content)))


def normalize_fts_query(query: str) -> str:
    tokens = re.findall(r"[\w/-]+", query.lower())
    return " OR ".join(tokens[:8]) or "trading"


def search(conn: sqlite3.Connection, query: str, limit: int = 8) -> list[RagSearchResult]:
    stmt = conn.execute(
        """
        SELECT d.source_path, d.title, snippet(rag_documents_fts, 1, '', '', '...', 24) AS snippet
        FROM rag_documents d
        JOIN rag_documents_fts ON d.id = rag_documents_fts.rowid
        WHERE rag_documents_fts MATCH ?
        ORDER BY bm25(rag_documents_fts)
        LIMIT ?
        """,
        (normalize_fts_query(query), limit),
    )
    return [RagSearchResult(source_path=row["source_path"], title=row["title"], snippet=row["snippet"]) for row in stmt.fetchall()]

