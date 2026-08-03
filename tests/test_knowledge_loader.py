"""Tests for [3] the documentation-corpus loader (ai_service/knowledge_loader.py).

First slice: ``jam-ws.md`` only (the shortest doc, 6 log statements), per
``rag-plan.md`` §11 — "chunk jam-ws.md, load it, ask what does JAM do".

These tests are deliberately about the properties that fail SILENTLY if broken:

* content that exceeds the encoder's ~256-token window is invisible with no
  error, so every chunk must stay inside the budget;
* §11's 17 traps must all survive the split — losing trap 12 looks exactly like
  a working index;
* excluded sections (§7 and its children, §13) must actually be excluded, and
  ``answering-policy.md`` must be skipped for the structural reason (no
  frontmatter), not by name;
* ids must be content-derived, because a positional id silently re-points every
  citation the moment a section is renumbered.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ai_service import knowledge_loader as kl

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "ai_service" / "knowledge"
JAM = KNOWLEDGE_DIR / "jam-ws.md"
POLICY = KNOWLEDGE_DIR / "answering-policy.md"


@pytest.fixture(scope="module")
def jam_chunks() -> list[kl.DocChunk]:
    return kl.load_file(JAM)


@pytest.fixture(scope="module")
def jam_sections() -> list[kl.Section]:
    _, body = kl.split_frontmatter(JAM.read_text(encoding="utf-8"))
    return kl.split_sections(body)


# --- frontmatter --------------------------------------------------------------
def test_frontmatter_parses_service_and_aliases():
    frontmatter, body = kl.split_frontmatter(JAM.read_text(encoding="utf-8"))
    assert frontmatter is not None
    assert frontmatter["service"] == "jam-ws"
    assert "JAM" in frontmatter["aliases"]
    assert "Java Authorisation Management" in frontmatter["aliases"]
    assert len(frontmatter["aliases"]) == 5
    # An empty YAML list must survive as [], not as None or "".
    assert frontmatter["statuses_written"] == []
    # The body must start after the fence, not include it.
    assert "```yaml" not in body
    assert body.lstrip().startswith("# jam-ws")


def test_answering_policy_has_no_frontmatter_and_is_skipped():
    """D9 enforced structurally: no ```yaml block ⇒ not a service doc."""
    frontmatter, _ = kl.split_frontmatter(POLICY.read_text(encoding="utf-8"))
    assert frontmatter is None
    assert kl.load_file(POLICY) == []


def test_every_service_doc_has_frontmatter():
    """The loader's "is this a service doc" test must hold for all five."""
    docs = sorted(p.name for p in KNOWLEDGE_DIR.glob("*.md"))
    with_frontmatter = [
        p.name
        for p in sorted(KNOWLEDGE_DIR.glob("*.md"))
        if kl.split_frontmatter(p.read_text(encoding="utf-8"))[0] is not None
    ]
    assert with_frontmatter == [d for d in docs if d != "answering-policy.md"]


def test_unparseable_frontmatter_degrades_to_not_a_service_doc():
    frontmatter, _ = kl.split_frontmatter("```yaml\nservice: [unclosed\n```\nbody\n")
    assert frontmatter is None


# --- sections -----------------------------------------------------------------
def test_sections_are_split_with_parents(jam_sections):
    headings = [s.heading for s in jam_sections]
    assert headings[0].startswith("1. What this service does")
    assert any(h.startswith("11. Blind spots") for h in headings)
    assert any(h.startswith("13. Provenance") for h in headings)
    # §7.1 must know §7 is its parent — that is what excludes it (its own
    # heading, "Level availability per environment", matches no rule).
    seven_one = next(s for s in jam_sections if s.heading.startswith("7.1"))
    assert seven_one.level == 3
    assert seven_one.parent is not None
    assert seven_one.parent.heading.startswith("7. Log anatomy")


def test_slugify_drops_numbering_and_markdown():
    assert kl.slugify("11. Blind spots and traps") == "blind-spots-and-traps"
    assert kl.slugify("1.1 What jam-ws is *not* responsible for") == (
        "what-jam-ws-is-not-responsible-for"
    )
    assert kl.slugify("9.2 Queue / topology reference") == "queue-topology-reference"


# --- kinds --------------------------------------------------------------------
@pytest.mark.parametrize(
    "heading,expected",
    [
        ("1. What this service does", "what_it_does"),
        ("1. What Order Engine does", "what_it_does"),
        ("1.1 What jam-ws is *not* responsible for", "what_it_does"),
        ("2. Key concepts", "concept"),
        ("2.1 Concepts this document does not cover", "concept"),
        ("3. How work flows through this service", "flow"),
        ("3. How an order flows", "flow"),
        ("3.1 Why a step gets skipped", "why_skipped"),
        ("4. The rules that stop something", "rule"),
        ("4. The rules that stop an order", "rule"),
        ("5. What users can do", "user_actions"),
        ("6. Statuses, and who writes them", "statuses"),
        ("8. Identifiers in the logs", "identifiers"),
        ("9. Journey stages and their log evidence", "journey_stage"),
        ("9.1 Lines that look like errors but are not", "not_an_error"),
        ("9.2 Queue / topology reference", "topology"),
        ("10. Evidence that is not in the log file", "evidence_outside_logs"),
        ("11. Blind spots and traps", "trap"),
        ("12. Escalation", "escalation"),
        ("7. Log anatomy — read this before quoting any line", kl.EXCLUDE),
        ("13. Provenance and known unknowns", kl.EXCLUDE),
    ],
)
def test_kind_for_heading_text(heading, expected):
    """Kinds come from heading TEXT, never the section number.

    Both wordings of §1/§3/§4 are covered because order-engine words them
    differently from the other four docs, and the numbering will be revised.
    """
    assert kl.kind_for(kl.Section(level=2, heading=heading, body="x")) == expected


def test_nested_section_inherits_exclusion_from_its_parent(jam_sections):
    for heading in ("7.1", "7.2", "7.3"):
        section = next(s for s in jam_sections if s.heading.startswith(heading))
        assert kl.kind_for(section) == kl.EXCLUDE, heading


def test_unknown_heading_is_indexed_unlabelled_not_dropped():
    """D7: an unrecognised heading loses precision, never the answer."""
    section = kl.Section(level=2, heading="14. Something nobody predicted", body="hi")
    assert kl.kind_for(section) is None
    assert len(kl.chunk_section(section, "jam-ws")) == 1


# --- exclusions ---------------------------------------------------------------
def test_excluded_sections_produce_no_chunks(jam_chunks):
    headings = {c.heading for c in jam_chunks}
    assert not [h for h in headings if h.startswith(("7.", "7 ", "13."))]
    assert not [h for h in headings if "Log anatomy" in h or "Provenance" in h]
    # And their content is genuinely absent, not merely unlabelled.
    body = "\n".join(c.text for c in jam_chunks)
    assert "jmxConfigurator" not in body          # §7
    assert "0.10.0-SNAPSHOT" not in body          # §13


def test_all_indexable_sections_are_present(jam_chunks):
    section_ids = {c.section_id for c in jam_chunks}
    assert section_ids == {
        "jam-ws#what-this-service-does",
        "jam-ws#what-jam-ws-is-not-responsible-for",
        "jam-ws#key-concepts",
        "jam-ws#concepts-this-document-does-not-cover",
        "jam-ws#how-work-flows-through-this-service",
        "jam-ws#why-a-step-gets-skipped",
        "jam-ws#the-rules-that-stop-something",
        "jam-ws#what-users-can-do",
        "jam-ws#statuses-and-who-writes-them",
        "jam-ws#identifiers-in-the-logs",
        "jam-ws#journey-stages-and-their-log-evidence",
        "jam-ws#lines-that-look-like-errors-but-are-not",
        "jam-ws#queue-topology-reference",
        "jam-ws#evidence-that-is-not-in-the-log-file",
        "jam-ws#blind-spots-and-traps",
        "jam-ws#escalation",
    }


# --- the budget (the silent-failure guard) ------------------------------------
def test_every_chunk_fits_the_encoder_budget(jam_chunks):
    """Over-budget text is truncated by the encoder with NO error at all."""
    oversized = [(c.id, len(c.text)) for c in jam_chunks if len(c.text) > kl.CHUNK_BUDGET_CHARS]
    assert oversized == []


def test_whole_corpus_fits_the_budget():
    chunks = kl.load_corpus(KNOWLEDGE_DIR)
    assert chunks, "corpus must not be empty"
    oversized = [(c.id, len(c.text)) for c in chunks if len(c.text) > kl.CHUNK_BUDGET_CHARS]
    assert oversized == []


def test_oversized_section_is_split_not_truncated(jam_chunks):
    """§11 is ~4.5k chars — it must become several chunks, losing nothing."""
    traps = [c for c in jam_chunks if c.section_id == "jam-ws#blind-spots-and-traps"]
    assert len(traps) >= 4


@pytest.mark.parametrize(
    "phrase",
    [
        "No correlation id on any path",              # trap 1
        "Role matching is exact and case-sensitive",  # trap 8  (the worked example)
        "Two throw sites share one message string",   # trap 9
        "Levels are mutable at runtime",              # trap 10
        "Three different expansions of",              # trap 13
        "No untracked source trees were found",       # trap 17 — the last one
    ],
)
def test_no_trap_is_lost_in_the_split(jam_chunks, phrase):
    """A dropped trap looks exactly like a working index. Pin the tail."""
    body = "\n".join(c.text for c in jam_chunks)
    assert phrase in body


def test_every_trap_item_is_present(jam_chunks):
    """All 17 numbered items of §11 survive chunking."""
    traps = [c for c in jam_chunks if c.section_id == "jam-ws#blind-spots-and-traps"]
    body = "\n".join(c.text for c in traps)
    numbers = {int(m) for m in re.findall(r"^\s*(\d+)\.\s", body, flags=re.M)}
    assert numbers == set(range(1, 18))


# --- chunk identity ----------------------------------------------------------
def test_chunk_ids_are_unique(jam_chunks):
    ids = [c.id for c in jam_chunks]
    assert len(ids) == len(set(ids))


def test_chunk_ids_are_content_derived_not_positional(jam_chunks):
    """No ordinal ids: renumbering a section must not re-point a citation."""
    for chunk in jam_chunks:
        assert not re.search(r"#\d", chunk.id), chunk.id
        assert not re.search(r"--\d+$", chunk.id), chunk.id


def test_trap_chunk_id_names_its_own_content(jam_chunks):
    """The worked example: trap 8 must be addressable as itself."""
    match = [
        c
        for c in jam_chunks
        if "Role matching is exact and case-sensitive" in c.text
    ]
    assert len(match) == 1
    assert match[0].id.startswith("jam-ws#blind-spots-and-traps--")
    assert "role-matching" in match[0].id


def test_single_chunk_section_keeps_the_bare_section_id(jam_chunks):
    """A section that fits needs no lead suffix — the id stays the short one."""
    topology = [c for c in jam_chunks if c.section_id == "jam-ws#queue-topology-reference"]
    assert len(topology) == 1
    assert topology[0].id == "jam-ws#queue-topology-reference"


# --- chunk contents ----------------------------------------------------------
def test_every_chunk_carries_its_own_identity(jam_chunks):
    """A fragment lifted into a prompt must still say where it came from."""
    for chunk in jam_chunks:
        first_line = chunk.text.splitlines()[0]
        assert first_line == f"[jam-ws · {chunk.heading}]", chunk.id


def test_table_rows_keep_their_header(jam_chunks):
    """A row torn from its header is meaningless — §2 is a 9-row table."""
    concepts = [c for c in jam_chunks if c.section_id == "jam-ws#key-concepts"]
    assert len(concepts) >= 2, "the §2 table must split"
    for chunk in concepts:
        assert "| Term | What it actually means in code |" in chunk.text


def test_chunks_carry_service_kind_and_aliases(jam_chunks):
    chunk = next(c for c in jam_chunks if c.section_id == "jam-ws#blind-spots-and-traps")
    assert chunk.service == "jam-ws"
    assert chunk.kind == "trap"
    assert chunk.audience == kl.AUDIENCE_SUPPORT
    assert "JAM" in chunk.aliases
    assert chunk.source == "jam-ws.md"
    assert chunk.metadata["service"] == "jam-ws"
    assert chunk.metadata["kind"] == "trap"


def test_parent_text_is_the_whole_section(jam_chunks):
    """Match on the precise chunk, answer from the section if needed."""
    traps = [c for c in jam_chunks if c.section_id == "jam-ws#blind-spots-and-traps"]
    parents = {c.parent_text for c in traps}
    assert len(parents) == 1, "every piece of a section shares one parent"
    parent = parents.pop()
    assert "No correlation id on any path" in parent
    assert "No untracked source trees were found" in parent
    assert len(parent) > kl.CHUNK_BUDGET_CHARS


def test_chunking_is_deterministic():
    """Same input, same ids — the evaluation set depends on this."""
    assert [c.id for c in kl.load_file(JAM)] == [c.id for c in kl.load_file(JAM)]


# --- the evaluation set -------------------------------------------------------
EVAL_FILE = Path(__file__).resolve().parent / "data" / "knowledge_eval.yaml"


@pytest.fixture(scope="module")
def eval_set() -> dict:
    import yaml

    return yaml.safe_load(EVAL_FILE.read_text(encoding="utf-8"))


def test_every_expected_chunk_id_exists(eval_set):
    """The eval set must never point at a chunk that no longer exists.

    Chunk ids are content-derived, so rewording a doc's opening line silently
    re-points an entry. Scoring that as a retrieval MISS would send someone
    hunting a threshold problem that is really a stale expectation — so it fails
    here instead, in CI, with no encoder needed.
    """
    known = {c.id for c in kl.load_corpus(KNOWLEDGE_DIR)}
    entries = eval_set["questions"] + eval_set["pasted_lines"]
    missing = sorted({e["expect"] for e in entries} - known)
    assert missing == []


def test_eval_set_covers_every_kind(eval_set):
    """A kind with no question is a kind nobody has ever checked works."""
    jam_kinds = {
        c.kind for c in kl.load_file(JAM) if c.kind not in (None, kl.EXCLUDE)
    }
    covered = {q["kind"] for q in eval_set["questions"]}
    assert jam_kinds - covered == set()


# --- corpus ------------------------------------------------------------------
def test_corpus_covers_all_five_service_docs():
    chunks = kl.load_corpus(KNOWLEDGE_DIR)
    services = {c.service for c in chunks}
    assert services == {
        "order-engine",
        "inbound-order",
        "order-validator-web-service",
        "rsm-ws",
        "jam-ws",
    }
