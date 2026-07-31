"""Tests for [3] the documentation index (ai_service/docsindex.py).

Built from files on disk rather than pushed up from Postgres, and deliberately
NOT persisted — it rebuilds from the corpus in seconds, so a Redis copy could only
drift from the source of truth.

These use a fake encoder (the project's existing pattern), so they run without
``requirements-ml.txt``. The properties worth pinning are the failure modes: every
one of them must leave a service that starts and answers from incident history
alone, never a crash.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_service import docsindex, settings

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "ai_service" / "knowledge"


class _FakeEncoder:
    """Deterministic bag-of-characters vector — no ML, but real cosine behaviour."""

    def encode(self, text: str):
        vector = [0.0] * 26
        for char in text.lower():
            if "a" <= char <= "z":
                vector[ord(char) - 97] += 1.0
        return vector


@pytest.fixture
def built():
    deps = docsindex.build(KNOWLEDGE_DIR, _FakeEncoder())
    docsindex.configure(deps)
    yield deps
    docsindex.configure(None)


# --- the happy path -----------------------------------------------------------
def test_builds_the_whole_corpus(built):
    assert built.error is None
    assert built.chunks > 200
    assert sorted(built.registry.documented) == [
        "inbound-order",
        "jam-ws",
        "order-engine",
        "order-validator-web-service",
        "rsm-ws",
    ]


def test_nothing_is_evicted(built):
    """Every chunk was deliberately authored — age is not a reason to drop one.

    ``RagIndex`` evicts oldest-first past its cap; that is right for an incident
    stream and wrong here, so the cap is set from the corpus size.
    """
    assert len(built.index) == built.chunks


def test_stats_report_size_and_kinds(built):
    stats = docsindex.stats()
    assert stats["enabled"] is True
    assert stats["chunks"] == built.chunks
    assert stats["error"] is None
    assert "trap" in stats["kinds"] and "escalation" in stats["kinds"]
    assert sum(stats["kinds"].values()) == built.chunks


def test_retrieve_returns_doc_chunks(built):
    hits = docsindex.retrieve("what does jam-ws do?", k=3)
    assert hits
    assert all(h["kind"] == "doc" for h in hits)
    assert all(h["metadata"]["service"] for h in hits)


def test_retrieve_respects_k(built):
    assert len(docsindex.retrieve("jam-ws", k=2)) <= 2


def test_retrieve_narrows_to_the_named_service(built):
    hits = docsindex.retrieve("what does jam-ws do?", k=5)
    assert {h["metadata"]["service"] for h in hits} == {"jam-ws"}


# --- every failure mode degrades, none crashes --------------------------------
def test_no_encoder_disables_the_index_without_raising():
    deps = docsindex.build(KNOWLEDGE_DIR, None)
    assert deps.chunks == 0
    assert "no encoder" in (deps.error or "")
    docsindex.configure(deps)
    try:
        assert docsindex.retrieve("anything") == []
        assert docsindex.stats()["enabled"] is False
    finally:
        docsindex.configure(None)


def test_missing_knowledge_dir_disables_the_index(tmp_path):
    deps = docsindex.build(tmp_path / "nope", _FakeEncoder())
    assert deps.chunks == 0
    assert "not found" in (deps.error or "")


def test_empty_knowledge_dir_disables_the_index(tmp_path):
    deps = docsindex.build(tmp_path, _FakeEncoder())
    assert deps.chunks == 0
    assert "empty" in (deps.error or "")


def test_retrieve_before_configure_returns_empty():
    docsindex.configure(None)
    assert docsindex.retrieve("anything") == []
    assert docsindex.stats()["enabled"] is False


def test_a_corpus_of_only_answering_policy_is_empty(tmp_path):
    """D9: no frontmatter ⇒ not a service doc ⇒ nothing to index."""
    (tmp_path / "answering-policy.md").write_text("# policy\n\ntext\n", encoding="utf-8")
    assert docsindex.build(tmp_path, _FakeEncoder()).chunks == 0


# --- reload -------------------------------------------------------------------
def test_reload_picks_up_an_edited_doc(tmp_path):
    doc = tmp_path / "jam-ws.md"
    doc.write_text(
        '```yaml\nservice: "jam-ws"\naliases: ["JAM"]\n```\n\n# jam-ws\n\n'
        "## 1. What this service does\n\noriginal text\n",
        encoding="utf-8",
    )
    docsindex.configure(docsindex.build(tmp_path, _FakeEncoder()))
    try:
        before = docsindex.stats()["chunks"]
        doc.write_text(
            doc.read_text(encoding="utf-8") + "\n## 11. Blind spots and traps\n\nnew\n",
            encoding="utf-8",
        )
        after = docsindex.reload()
        assert after is not None and after.chunks == before + 1
        assert docsindex.stats()["chunks"] == before + 1
    finally:
        docsindex.configure(None)


def test_a_failed_reload_keeps_the_working_index(tmp_path):
    """Breaking the corpus must not take the running index down with it."""
    doc = tmp_path / "jam-ws.md"
    doc.write_text(
        '```yaml\nservice: "jam-ws"\n```\n\n# jam-ws\n\n## 1. What this service does\n\ntext\n',
        encoding="utf-8",
    )
    docsindex.configure(docsindex.build(tmp_path, _FakeEncoder()))
    try:
        before = docsindex.stats()["chunks"]
        assert before > 0
        doc.unlink()  # corpus now empty — the rebuild will produce nothing
        docsindex.reload()
        assert docsindex.stats()["chunks"] == before
    finally:
        docsindex.configure(None)


def test_reload_before_configure_is_a_noop():
    docsindex.configure(None)
    assert docsindex.reload() is None


# --- settings -----------------------------------------------------------------
def test_docs_index_does_not_share_the_incident_index_settings():
    """Separate numbers are the point of a separate index (rag-plan.md D1)."""
    assert settings.DOCSINDEX_MIN_SCORE != settings.RAGINDEX_MIN_SCORE
    assert settings.DOCSINDEX_K != settings.RAGINDEX_MAX_ENTRIES
    # ...but the MODEL is shared, or startup loads a second encoder.
    assert settings.DOCSINDEX_MODEL == settings.RAGINDEX_MODEL
