"""[3] AI Service — work out which service a question is about, before searching.

Retrieval over the docs corpus is three steps (rag-plan.md §6). This module is
step 2 — the narrowing:

1. exact match on a pasted log message (not built yet)
2. **narrow to the service the question names**  ← here
3. rank what survives by cosine

Why the narrowing is not optional. Measured on the 17-question evaluation set with
step 3 alone, over all 274 chunks of all five docs: **top-1 29%**. The failures were
not subtle — "can I find out who gave this user the admin privilege?" (a jam-ws
question) was answered by ``order-engine#never-say-who-did-something``, and a single
trap about the three spellings of the word "JAM" won three unrelated questions
because it is the chunk that talks about the letters J-A-M. Similarity has no notion
of "wrong service"; only a filter does.

Detection is **plain string matching, no LLM and no embeddings** — a closed set of
service names and their aliases. Aliases come from two places and the split is
deliberate (see ``knowledge/service-map.yaml``): a documented service's aliases live
in its own frontmatter (one source of truth), and ``service-map.yaml`` carries
aliases only for services with no doc file, plus every mock ``cc-*`` app_name.

**Routing is a fast path, not a gate (rag-plan.md D7).** No service named, or two
named at once, means search wider — never refuse. A bot that says "I only handle
these six question shapes" is the failure mode that kills adoption.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ai_service import knowledge_loader as kl

__all__ = [
    "ServiceEntry",
    "Registry",
    "Detection",
    "build_registry",
    "detect_services",
    "detect_kinds",
    "retrieve_docs",
]

SERVICE_MAP_FILE = "service-map.yaml"

# Question phrasing -> the section kind that should answer it. ORDERED, and the
# order carries real weight; each comment below marks a case that would otherwise
# be routed wrongly.
#
# Unlike a service name, question phrasing is genuinely ambiguous, so a WRONG kind
# is a live risk here in a way it is not for services. That is why the default
# retrieval mode is "soft": kind-matched chunks rank first but unmatched ones still
# fill the remaining slots, so a mis-detected kind costs ranking, never the answer.
_QUESTION_KIND_RULES: list[tuple[re.Pattern[str], str]] = [
    # Must precede the HTTP-code rule: "401 Not authorized, whose problem is it?"
    # is an escalation question that merely quotes a status code.
    (
        re.compile(
            r"who (do|should) i|whose problem|escalat|raise (it|this|a ticket)|"
            r"hand (it )?over|who owns",
            re.I,
        ),
        "escalation",
    ),
    # Must precede `concept`: "jam returned 404 ... what does that mean?" would
    # otherwise match "what does X mean" and be routed to a definition.
    (
        re.compile(r"\b[45]\d{2}\b|blocked|rejected|why (was|were|did).*(fail|stop|block)", re.I),
        "rule",
    ),
    (
        re.compile(
            r"is (that|this|it) (a )?(real )?(problem|bad|normal|expected)|"
            r"should i worry|anything to worry|are those errors",
            re.I,
        ),
        "not_an_error",
    ),
    (
        re.compile(
            r"where else can i look|not in the log|logs? shows? nothing|"
            r"nothing in the logs?|outside the log",
            re.I,
        ),
        "evidence_outside_logs",
    ),
    (re.compile(r"which log lines|what log lines|what does it log|what lines does", re.I), "journey_stage"),
    (re.compile(r"what can i search|search on|correlation id|what ids?\b", re.I), "identifiers"),
    (re.compile(r"who (granted|gave)|can i (trust|find out who)|is it safe to assume", re.I), "trap"),
    (re.compile(r"\bskipp?(ed|ing)?\b|why did.*not (run|happen|get called)", re.I), "why_skipped"),
    (re.compile(r"\bqueues?\b|topolog|rabbit|kafka|messaging|dead.?letter", re.I), "topology"),
    (re.compile(r"button|screen|\bui\b|admin page|click|front.?end", re.I), "user_actions"),
    (re.compile(r"\bstatus(es)?\b|writes? to the database|writes? (any )?rows", re.I), "statuses"),
    (re.compile(r"endpoint|how (does|do).*(work|flow)|what are the steps|what happens when", re.I), "flow"),
    (re.compile(r"^what is (a|an|the)\b|what does .*\bmean\b|difference between", re.I), "concept"),
    (re.compile(r"what does .*\bdo(es)?\b|what is .* (for|responsible for)|what.s it for", re.I), "what_it_does"),
]


@dataclass
class ServiceEntry:
    """One service the assistant can be asked about, documented or not."""

    name: str  # the doc name ("jam-ws") or the display name when undocumented
    aliases: list[str] = field(default_factory=list)
    mock_app_name: str | None = None
    documented: bool = False


@dataclass
class Registry:
    """Every service the assistant knows of, keyed by name."""

    services: dict[str, ServiceEntry] = field(default_factory=dict)

    @property
    def documented(self) -> list[str]:
        return [s.name for s in self.services.values() if s.documented]

    def all_terms(self) -> list[tuple[str, str]]:
        """``(term, service_name)`` pairs, LONGEST FIRST.

        Longest-first matters: "SAP submission" (Outbound OSW) must win over "SAP"
        (SAP/BTP), and "Order Engine 2.0" over "Order Engine".
        """
        terms: list[tuple[str, str]] = []
        for entry in self.services.values():
            candidates = [entry.name, *entry.aliases]
            if entry.mock_app_name:
                candidates.append(entry.mock_app_name)
            for term in candidates:
                if term and term.strip():
                    terms.append((term.strip(), entry.name))
        terms.sort(key=lambda t: -len(t[0]))
        return terms


@dataclass
class Detection:
    """What a question named. Either list may be empty; both may be non-empty."""

    documented: list[str] = field(default_factory=list)
    undocumented: list[str] = field(default_factory=list)

    @property
    def any_service(self) -> bool:
        return bool(self.documented or self.undocumented)


def build_registry(knowledge_dir: str | Path) -> Registry:
    """Merge doc frontmatter aliases with ``service-map.yaml`` into one registry."""
    knowledge_dir = Path(knowledge_dir)
    registry = Registry()

    # 1. Documented services — aliases from their own frontmatter (source of truth).
    for path in sorted(knowledge_dir.glob("*.md")):
        frontmatter, _ = kl.split_frontmatter(path.read_text(encoding="utf-8"))
        if frontmatter is None:
            continue  # answering-policy.md — not a service doc
        name = str(frontmatter.get("service") or path.stem).strip()
        raw = frontmatter.get("aliases") or []
        aliases = [str(a) for a in raw] if isinstance(raw, list) else []
        registry.services[name] = ServiceEntry(
            name=name, aliases=aliases, documented=True
        )

    # 2. The map adds mock app_names, and the services that have no doc at all.
    map_path = knowledge_dir / SERVICE_MAP_FILE
    if not map_path.exists():
        return registry
    try:
        data = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return registry  # a broken map degrades to frontmatter-only, never a crash

    for entry in data.get("services") or []:
        doc = entry.get("doc")
        app_name = entry.get("mock_app_name")
        aliases = [str(a) for a in (entry.get("aliases") or [])]
        if doc and doc in registry.services:
            registry.services[doc].mock_app_name = app_name
        elif app_name:
            # Emitted but undocumented (SPT, Checker, ...). Knowing it EXISTS is
            # what makes "no documented knowledge of that service" deterministic
            # rather than a fallback (rag-plan.md §10 open question 7).
            registry.services[app_name] = ServiceEntry(
                name=app_name, aliases=aliases, mock_app_name=app_name, documented=False
            )

    for key in ("folded_in", "not_simulated"):
        for entry in data.get(key) or []:
            name = str(entry.get("name") or "").strip()
            if not name or name in registry.services:
                continue
            registry.services[name] = ServiceEntry(
                name=name,
                aliases=[str(a) for a in (entry.get("aliases") or [])],
                documented=False,
            )
    return registry


def detect_services(question: str, registry: Registry) -> Detection:
    """Which services a question names. Whole-word, case-insensitive.

    Whole-word matching is load-bearing: ``oe`` is an alias of order-engine, and a
    substring match would fire on "does", "some", "note" — every other question.
    """
    documented: list[str] = []
    undocumented: list[str] = []
    for term, service in registry.all_terms():
        entry = registry.services[service]
        bucket = documented if entry.documented else undocumented
        if service in bucket:
            continue
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", question, re.IGNORECASE):
            bucket.append(service)
    return Detection(documented=documented, undocumented=undocumented)


def detect_kinds(question: str) -> list[str]:
    """Which section kind(s) a question is asking for. Empty when unrecognised."""
    kinds: list[str] = []
    for pattern, kind in _QUESTION_KIND_RULES:
        if kind not in kinds and pattern.search(question):
            kinds.append(kind)
    return kinds


def _merge(hits_lists: list[list[dict]], k: int) -> list[dict]:
    """Best-score-wins merge of several top-k lists, deterministic on ties."""
    merged: dict[str, dict] = {}
    for hits in hits_lists:
        for hit in hits:
            existing = merged.get(hit["id"])
            if existing is None or hit["score"] > existing["score"]:
                merged[hit["id"]] = hit
    return sorted(merged.values(), key=lambda r: (-r["score"], r["id"]))[:k]


def retrieve_docs(
    index,
    question: str,
    registry: Registry,
    k: int = 5,
    *,
    kind_mode: str = "soft",
) -> list[dict]:
    """Narrow by service and question kind, then rank by cosine.

    One ``retrieve`` per (service, kind) rather than a single ``IN``-style filter,
    because :class:`~ai_service.ragindex.RagIndex` filters on metadata EQUALITY by
    design ("a cheap pre-filter, not a query language") and widening that contract
    would touch the incident index too. Merging a handful of top-k lists is cheap
    and leaves the shared code alone.

    ``kind_mode``:

    * ``"soft"`` (default) — kind-matched chunks are ranked first, then the
      service-filtered remainder fills the leftover slots. A mis-detected kind
      costs ranking, never the answer.
    * ``"hard"`` — kind-matched only. Sharper when detection is right, and
      silently unanswerable when it is wrong.
    * ``"off"`` — service filtering only.
    """
    detection = detect_services(question, registry)
    services = detection.documented or [None]
    kinds = detect_kinds(question) if kind_mode != "off" else []

    def _filters(service: str | None, kind: str | None) -> dict | None:
        f: dict[str, str] = {}
        if service:
            f["service"] = service
        if kind:
            f["kind"] = kind
        return f or None

    # Service-only results: the fallback pool, and the whole answer when no kind
    # was recognised. No service named either means a plain wide search (D7).
    broad = _merge(
        [index.retrieve(question, k=k, filters=_filters(s, None)) for s in services], k
    )
    if not kinds:
        return broad

    narrow = _merge(
        [
            index.retrieve(question, k=k, filters=_filters(s, kind))
            for s in services
            for kind in kinds
        ],
        k,
    )
    if kind_mode == "hard":
        return narrow

    seen = {hit["id"] for hit in narrow}
    return (narrow + [hit for hit in broad if hit["id"] not in seen])[:k]
