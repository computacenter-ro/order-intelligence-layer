"""[3] AI Service — loader for the system-documentation corpus (docs RAG).

Turns ``ai_service/knowledge/*.md`` into embeddable chunks. **No encoder, no
Redis, no network anywhere in this module** — it is pure text processing, so it
unit-tests without ``requirements-ml.txt`` installed and the whole corpus can be
inspected offline. Embedding and retrieval live elsewhere; this module only
decides *what a chunk is*.

Why a chunk is a bounded section, not a whole section
-----------------------------------------------------
Two constraints, and the second is the one that matters:

1. ``all-MiniLM-L6-v2`` truncates input at ~256 word-piece tokens. 13 of the 21
   sections in ``jam-ws.md`` exceed that; §11 ("Blind spots and traps") is ~1114
   tokens, so roughly three quarters of it would never be embedded at all —
   silently, with no error and every structural test still passing.
2. **One chunk becomes one vector, i.e. one "meaning."** §11 is 17 mutually
   unrelated warnings. Averaged into a single vector they mean nothing in
   particular, so the blob matches every jam-ws question weakly and no jam-ws
   question well. A bigger encoder would fix (1) and leave (2) untouched, which
   is why the fix is chunking rather than a model swap (a swap would also
   invalidate every vector already persisted in ``alerts.embedding`` and
   de-tune ``SEMCACHE_THRESHOLD`` / ``INCIDENT_COSINE_THRESHOLD`` /
   ``RAGINDEX_MIN_SCORE``, all of which were chosen for THIS model).

So a section is split at its own natural repeated unit — table row, list item,
bold sub-block, paragraph — and units are packed up to :data:`CHUNK_BUDGET_CHARS`.
Never mid-sentence. Every piece is prefixed ``[service · heading]`` so a fragment
still says where it came from, and a table row is always re-issued **with its
header**, because a row torn from its header means nothing.

Chunk ids are content-derived, never positional
-----------------------------------------------
``jam-ws#blind-spots-and-traps--role-matching-is-exact-and`` rather than
``jam-ws#11-4``. Ordinals shift the moment anyone inserts a trap or the budget
changes, which would silently re-point every citation already shown to a user and
invalidate the evaluation set. A slug derived from the chunk's own opening words
only changes when that content changes.

D9 (``rag-plan.md``): ``answering-policy.md`` is the assistant's system prompt,
not indexed knowledge. It is the only file in the corpus with **no yaml
frontmatter block**, and that absence — not a hardcoded filename — is this
loader's test for "not a service doc".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "CHUNK_BUDGET_CHARS",
    "EXCLUDE",
    "DocChunk",
    "Section",
    "split_frontmatter",
    "split_sections",
    "slugify",
    "kind_for",
    "chunk_section",
    "load_file",
    "load_corpus",
]


# ~250 word-piece tokens at the ~4-chars-per-token rule of thumb. Deliberately in
# CHARACTERS, not tokens: counting tokens would require the sentence-transformers
# tokenizer, and this module must stay importable (and testable) without the
# optional ML dependency. The margin below 256 absorbs the estimate's error.
CHUNK_BUDGET_CHARS = 1000

# Returned by :func:`kind_for` for a section that must NOT be indexed. Distinct
# from ``None``, which means "indexed but unlabelled" — an unknown heading keeps
# its content searchable (D7: you may lose precision, never the answer).
EXCLUDE = "__exclude__"

# Every section that survives exclusion is support-level: §7 (log anatomy) and
# §13 (provenance) are the engineer/meta sections and both are excluded outright.
# So this is a correct constant today rather than a placeholder — it becomes a
# real derivation only if template change §4.4 (split "finding" from "code
# reason") ever lands.
AUDIENCE_SUPPORT = "support"

# Ordered, first match wins, matched against a NORMALIZED heading (numbering and
# markdown emphasis stripped, lowercased). Matched on heading TEXT and never on
# the section number, because the numbering drifts across files today
# (order-engine's §9.2 is "Regenerate", everyone else's is queue topology) and
# will drift again when the template is revised.
_KIND_RULES: list[tuple[re.Pattern[str], str]] = [
    # --- not indexed ---------------------------------------------------------
    # Engineer-level AND describes a plain-text logback format the simulation
    # never emits (rag-plan.md D10). Its 7.x children inherit the exclusion.
    (re.compile(r"log anatomy"), EXCLUDE),
    # About the document, not about the service.
    (re.compile(r"provenance"), EXCLUDE),
    # --- indexed -------------------------------------------------------------
    (re.compile(r"\bnot\b.*responsible"), "what_it_does"),
    (re.compile(r"^what .*\bdoes\b"), "what_it_does"),
    (re.compile(r"does not cover"), "concept"),
    (re.compile(r"key concepts?"), "concept"),
    (re.compile(r"why a step gets skipped"), "why_skipped"),
    (re.compile(r"^how .*flows?\b"), "flow"),
    (re.compile(r"rules that stop"), "rule"),
    (re.compile(r"what users can do"), "user_actions"),
    (re.compile(r"^statuses\b"), "statuses"),
    (re.compile(r"identifiers"), "identifiers"),
    (re.compile(r"look like errors"), "not_an_error"),
    (re.compile(r"\bqueue\b|topology"), "topology"),
    (re.compile(r"journey stages?"), "journey_stage"),
    (re.compile(r"evidence that is not"), "evidence_outside_logs"),
    (re.compile(r"blind spots|\btraps?\b"), "trap"),
    (re.compile(r"escalation"), "escalation"),
]

_FRONTMATTER_RE = re.compile(r"\A\s*```yaml\r?\n(?P<body>.*?)\r?\n```[ \t]*\r?\n", re.S)
_HEADING_RE = re.compile(r"^(?P<hashes>#{2,3})[ \t]+(?P<title>\S.*?)[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*```")
_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+\S")
_BOLD_BLOCK_RE = re.compile(r"^\*\*\S")
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|[\s:|-]+\|[ \t]*$")


# --- data ---------------------------------------------------------------------
@dataclass
class Section:
    """One ``##``/``###`` section of a service doc, with its parent if nested."""

    level: int
    heading: str
    body: str
    parent: "Section | None" = None

    @property
    def slug(self) -> str:
        return slugify(self.heading)


@dataclass
class DocChunk:
    """One embeddable piece of documentation.

    ``text`` is what gets embedded and shown; it always opens with the
    ``[service · heading]`` prefix line. ``parent_text`` is the FULL section this
    piece came from — retrieval matches the precise chunk, but a caller may hand
    the surrounding section to the LLM when the answer needs the context. That is
    the useful half of "summarise each section", obtained without an LLM and
    without losing the exact wording.
    """

    id: str
    service: str
    heading: str
    section_id: str
    kind: str | None
    audience: str
    text: str
    parent_text: str
    source: str
    aliases: list[str] = field(default_factory=list)

    @property
    def metadata(self) -> dict[str, Any]:
        """Free-form metadata for the index's pre-filter (see ragindex D6/§9)."""
        return {
            "service": self.service,
            "kind": self.kind or "",
            "audience": self.audience,
            "heading": self.heading,
            "section_id": self.section_id,
            "source": self.source,
        }


# --- frontmatter --------------------------------------------------------------
def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """``(frontmatter, body)``; frontmatter is ``None`` when the file has none.

    ``None`` means "not a service doc" and the caller must skip the file
    entirely — that is how ``answering-policy.md`` stays out of the index (D9)
    without this module knowing its name. Unparseable YAML degrades to ``None``
    for the same reason a broken record is skipped elsewhere: a corrupt file must
    not be indexed as though it were fine.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    try:
        data = yaml.safe_load(match.group("body"))
    except yaml.YAMLError:
        return None, text
    if not isinstance(data, dict):
        return None, text
    return data, text[match.end() :]


# --- sections -----------------------------------------------------------------
def split_sections(body: str) -> list[Section]:
    """Split a doc body into ``##``/``###`` sections, outermost first.

    Fence-aware: a ``#`` inside a fenced code block is content, not a heading.
    Content before the first ``##`` (the ``# service`` title) is dropped — it
    carries no information the frontmatter does not already have.
    """
    sections: list[Section] = []
    current: Section | None = None
    last_level2: Section | None = None
    lines: list[str] = []
    in_fence = False

    def flush() -> None:
        if current is not None:
            current.body = "\n".join(lines).strip("\n")

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING_RE.match(line)
        if heading:
            flush()
            level = len(heading.group("hashes"))
            current = Section(
                level=level,
                heading=heading.group("title"),
                body="",
                parent=last_level2 if level > 2 else None,
            )
            if level == 2:
                last_level2 = current
            sections.append(current)
            lines = []
        elif current is not None:
            lines.append(line)
    flush()
    return sections


def slugify(text: str) -> str:
    """A stable, readable id fragment: drop numbering/markdown, kebab-case."""
    text = re.sub(r"^\s*\d+(\.\d+)*\.?\s*", "", text.strip())
    text = re.sub(r"[*_`~]", "", text)
    text = re.sub(r"[^0-9a-zA-Z]+", "-", text.lower())
    return text.strip("-")


def _cap_slug(slug: str, max_len: int = 44) -> str:
    """Shorten a slug on a word boundary — never mid-word.

    Ids are shown to users as citations, so ``...case-sensitiv`` reads like a
    bug. Dropping the whole final word is both readable and still deterministic.
    """
    if len(slug) <= max_len:
        return slug.strip("-")
    cut = slug.rfind("-", 0, max_len + 1)
    return (slug[:cut] if cut > 0 else slug[:max_len]).strip("-")


def _normalize_heading(heading: str) -> str:
    heading = re.sub(r"^\s*\d+(\.\d+)*\.?\s*", "", heading.strip().lower())
    heading = re.sub(r"[*_`~]", "", heading)
    return re.sub(r"\s+", " ", heading).strip()


def kind_for(section: Section) -> str | None:
    """The section's ``kind``, :data:`EXCLUDE`, or ``None`` for "unlabelled".

    A nested section with no rule of its own **inherits its parent's verdict** —
    which is what keeps §7.1/7.2/7.3 out of the index. Their own headings
    ("Retries", "Retention and file names") match nothing, so without inheritance
    they would be indexed while their §7 parent was excluded.
    """
    normalized = _normalize_heading(section.heading)
    for pattern, kind in _KIND_RULES:
        if pattern.search(normalized):
            return kind
    if section.parent is not None:
        return kind_for(section.parent)
    return None


# --- units: the natural repeated pieces inside one section --------------------
@dataclass
class _Unit:
    text: str
    lead: str  # short label used to derive this chunk's id


def _lead_words(text: str, max_words: int = 7) -> str:
    text = re.sub(r"[*_`~\[\]()]", " ", text)
    text = re.sub(r"^\s*(?:[-*+]|\d+(?:\.\d+)*\.?)\s*", "", text.strip())
    words = [w for w in re.split(r"\s+", text) if w]
    return " ".join(words[:max_words])


def _table_units(lines: list[str]) -> list[_Unit]:
    """One unit per body row, each re-issued WITH the table's header rows."""
    header = lines[:2]
    units: list[_Unit] = []
    for row in lines[2:]:
        if not row.strip():
            continue
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        first = next((c for c in cells if c), row)
        units.append(_Unit("\n".join(header + [row]), _lead_words(first)))
    return units or [_Unit("\n".join(lines), _lead_words(lines[0]))]


def _list_units(lines: list[str]) -> list[_Unit]:
    """One unit per list item, continuation lines attached to their item."""
    units: list[_Unit] = []
    buffer: list[str] = []
    for line in lines:
        if _LIST_ITEM_RE.match(line) and buffer:
            units.append(_Unit("\n".join(buffer), _lead_words(buffer[0])))
            buffer = [line]
        else:
            buffer.append(line)
    if buffer:
        units.append(_Unit("\n".join(buffer), _lead_words(buffer[0])))
    return units


def _blocks(body: str) -> list[list[str]]:
    """Blank-line separated blocks; a fenced code block stays one block."""
    blocks: list[list[str]] = []
    current: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            current.append(line)
            continue
        if not line.strip() and not in_fence:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _units(body: str) -> list[_Unit]:
    """Split a section body into its natural repeated units."""
    units: list[_Unit] = []
    for block in _blocks(body):
        stripped = [l for l in block if l.strip()]
        if not stripped:
            continue
        is_table = (
            len(stripped) >= 3
            and stripped[0].lstrip().startswith("|")
            and _TABLE_SEP_RE.match(stripped[1]) is not None
        )
        if is_table:
            units.extend(_table_units(stripped))
        elif _LIST_ITEM_RE.match(stripped[0]) and len(_list_units(block)) > 1:
            units.extend(_list_units(block))
        elif _BOLD_BLOCK_RE.match(stripped[0]):
            units.append(_Unit("\n".join(block), _lead_words(stripped[0])))
        else:
            units.append(_Unit("\n".join(block), _lead_words(stripped[0])))
    return units


def _hard_split(text: str, budget: int) -> list[str]:
    """Last-resort split of a single oversized unit — sentences, then words.

    Only reached when one table row or list item alone exceeds the budget. Cuts
    at a sentence boundary where it can and at a word boundary where it cannot;
    never mid-word, and never silently truncates.
    """
    if len(text) <= budget:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        candidate = f"{current} {sentence}".strip() if current else sentence
        if current and len(candidate) > budget:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)

    final: list[str] = []
    for piece in pieces:
        while len(piece) > budget:
            cut = piece.rfind(" ", 0, budget)
            if cut <= 0:
                cut = budget
            final.append(piece[:cut].rstrip())
            piece = piece[cut:].lstrip()
        if piece:
            final.append(piece)
    return final


def _pack(units: list[_Unit], budget: int) -> list[_Unit]:
    """Greedily merge consecutive units while they fit inside ``budget``."""
    packed: list[_Unit] = []
    current: _Unit | None = None
    for unit in units:
        if len(unit.text) > budget:
            if current is not None:
                packed.append(current)
                current = None
            for i, piece in enumerate(_hard_split(unit.text, budget)):
                packed.append(_Unit(piece, unit.lead if i == 0 else _lead_words(piece)))
            continue
        if current is None:
            current = _Unit(unit.text, unit.lead)
        elif len(current.text) + 2 + len(unit.text) <= budget:
            current = _Unit(f"{current.text}\n\n{unit.text}", current.lead)
        else:
            packed.append(current)
            current = _Unit(unit.text, unit.lead)
    if current is not None:
        packed.append(current)
    return packed


# --- chunking -----------------------------------------------------------------
def chunk_section(
    section: Section,
    service: str,
    *,
    aliases: list[str] | None = None,
    source: str = "",
    budget: int = CHUNK_BUDGET_CHARS,
) -> list[DocChunk]:
    """Chunk one section. Empty list when the section is excluded or blank."""
    kind = kind_for(section)
    if kind is EXCLUDE or not section.body.strip():
        return []

    prefix = f"[{service} · {section.heading}]"
    room = budget - len(prefix) - 1
    pieces = _pack(_units(section.body), max(room, 1))
    if not pieces:
        return []

    section_slug = section.slug
    single = len(pieces) == 1
    chunks: list[DocChunk] = []
    for piece in pieces:
        lead = _cap_slug(slugify(piece.lead))
        chunk_id = f"{service}#{section_slug}"
        if not single and lead:
            chunk_id = f"{chunk_id}--{lead}"
        chunks.append(
            DocChunk(
                id=chunk_id,
                service=service,
                heading=section.heading,
                section_id=f"{service}#{section_slug}",
                kind=kind,
                audience=AUDIENCE_SUPPORT,
                text=f"{prefix}\n{piece.text}",
                parent_text=section.body,
                source=source,
                aliases=list(aliases or []),
            )
        )
    return _dedupe_ids(chunks)


def _dedupe_ids(chunks: list[DocChunk]) -> list[DocChunk]:
    """Suffix collisions deterministically so ids stay unique AND stable."""
    seen: dict[str, int] = {}
    for chunk in chunks:
        count = seen.get(chunk.id, 0) + 1
        seen[chunk.id] = count
        if count > 1:
            chunk.id = f"{chunk.id}-{count}"
    return chunks


def load_file(path: str | Path, budget: int = CHUNK_BUDGET_CHARS) -> list[DocChunk]:
    """Chunk one service doc. Empty list when the file is not a service doc."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)
    if frontmatter is None:
        return []  # no frontmatter ⇒ not a service doc (D9)
    service = str(frontmatter.get("service") or path.stem).strip()
    raw_aliases = frontmatter.get("aliases") or []
    aliases = [str(a) for a in raw_aliases] if isinstance(raw_aliases, list) else []

    chunks: list[DocChunk] = []
    for section in split_sections(body):
        chunks.extend(
            chunk_section(
                section,
                service,
                aliases=aliases,
                source=path.name,
                budget=budget,
            )
        )
    return _dedupe_ids(chunks)


def load_corpus(
    directory: str | Path, budget: int = CHUNK_BUDGET_CHARS
) -> list[DocChunk]:
    """Chunk every service doc in ``directory`` (sorted, so ids are stable)."""
    directory = Path(directory)
    chunks: list[DocChunk] = []
    for path in sorted(directory.glob("*.md")):
        chunks.extend(load_file(path, budget=budget))
    return chunks
