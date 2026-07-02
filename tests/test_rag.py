from __future__ import annotations

from bot.knowledge.rag import index_markdown, search
from bot.storage.db import connect, init_db


def test_rag_indexes_markdown_and_redacts_secrets(settings, tmp_path):
    doc = tmp_path / "note.md"
    doc.write_text("# Trading Risk\nUse kill switch. PRIVATE_KEY=0x" + "a" * 64)
    settings.rag_paths = [doc]
    init_db(settings.sqlite_path)

    with connect(settings.sqlite_path) as conn:
        assert index_markdown(conn, settings) == 1
        results = search(conn, "kill switch")
        stored = conn.execute("SELECT content FROM rag_documents").fetchone()["content"]

    assert results
    assert results[0].title == "Trading Risk"
    assert "PRIVATE_KEY" not in stored
    assert "0x" + "a" * 64 not in stored

