"""Tests for [3] docs-retrieval routing (ai_service/knowledge_routing.py).

Routing is the narrowing step: work out which service and which kind of section a
question wants, BEFORE ranking by similarity. Pure string matching — no encoder
needed, so these run in CI.

Measured effect on the 17-question evaluation set (274 chunks, budget 1000):

    similarity only          top-1 29%   top-3 53%
    + service filter         top-1 29%   top-3 53%
    + question-kind filter   top-1 41%   top-3 82%

The service filter scoring flat is not a bug — 16 of the 17 questions name JAM and
similarity already landed in the right document. It earns its keep on the class of
question the set under-samples (a question naming another service, or none), and it
is what stops an order-engine chunk answering a jam-ws question.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_service import knowledge_routing as kr

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "ai_service" / "knowledge"


@pytest.fixture(scope="module")
def registry() -> kr.Registry:
    return kr.build_registry(KNOWLEDGE_DIR)


# --- the registry -------------------------------------------------------------
def test_registry_knows_every_documented_service(registry):
    assert sorted(registry.documented) == [
        "inbound-order",
        "jam-ws",
        "order-engine",
        "order-validator-web-service",
        "rsm-ws",
    ]


def test_documented_services_carry_their_mock_app_name(registry):
    """The everyday path starts from a cc-* name on the dashboard."""
    assert registry.services["jam-ws"].mock_app_name == "cc-jam-service"
    assert registry.services["order-engine"].mock_app_name == "cc-order-engine"


def test_registry_includes_undocumented_and_unsimulated_services(registry):
    """Knowing a service EXISTS is what makes "no documentation" deterministic."""
    assert "cc-spt-service" in registry.services
    assert registry.services["cc-spt-service"].documented is False
    assert "SOLR" in registry.services          # folded_in
    assert "Salesforce" in registry.services    # not_simulated


def test_undocumented_services_point_at_their_second_hand_doc(registry):
    """cc-checker-service has no file, but order-engine.md §4.3 is the Order
    Engine's account of the margin check."""
    assert registry.services["cc-checker-service"].see_also == "order-engine"


def test_folded_in_components_inherit_the_doc_of_their_emitter(registry):
    """SOLR and Avalara have no app_name of their own — their lines come out of
    another service, whose doc is therefore the one describing them. No explicit
    see_also needed: `emitted_by` already is one."""
    assert registry.services["SOLR"].see_also == "order-engine"
    assert registry.services["Avalara"].see_also == "order-validator-web-service"


def test_a_genuinely_undocumented_service_has_no_see_also(registry):
    """Nothing in the corpus describes SPT — that absence must stay honest."""
    assert registry.services["cc-spt-service"].see_also is None
    assert registry.services["SAP / BTP"].see_also is None


def test_registry_terms_are_longest_first(registry):
    """"SAP submission" (OSW) must beat "SAP" (SAP/BTP)."""
    lengths = [len(term) for term, _ in registry.all_terms()]
    assert lengths == sorted(lengths, reverse=True)


# --- service detection --------------------------------------------------------
@pytest.mark.parametrize(
    "question,expected",
    [
        ("what does JAM do?", ["jam-ws"]),
        ("what does jam-ws write?", ["jam-ws"]),
        ("what is Java Authorisation Management for?", ["jam-ws"]),
        ("cc-jam-service threw an error", ["jam-ws"]),
        ("what does the rsm web service do?", ["rsm-ws"]),
        ("the Order Engine timed out", ["order-engine"]),
    ],
)
def test_detects_the_named_service(question, expected, registry):
    assert kr.detect_services(question, registry).documented == expected


def test_detects_several_services_at_once(registry):
    found = kr.detect_services("the order engine called jam and got 403", registry)
    assert set(found.documented) == {"order-engine", "jam-ws"}


def test_undocumented_service_is_detected_separately(registry):
    found = kr.detect_services("what does SPT do?", registry)
    assert found.documented == []
    assert found.undocumented == ["cc-spt-service"]
    assert found.any_service is True


def test_no_service_named_detects_nothing(registry):
    found = kr.detect_services("why did the order fail?", registry)
    assert found.documented == [] and found.undocumented == []
    assert found.any_service is False


def test_short_alias_does_not_match_inside_a_word(registry):
    """`oe` is an alias of order-engine — a substring match would fire on
    "does", "some", "note", i.e. on nearly every question ever asked."""
    for question in ("does the order fail?", "some note about this", "shoe size"):
        assert "order-engine" not in kr.detect_services(question, registry).documented


# --- question-kind detection --------------------------------------------------
@pytest.mark.parametrize(
    "question,expected",
    [
        ("what does JAM do?", "what_it_does"),
        ("what is a per-user override?", "concept"),
        ("what endpoints does it expose?", "flow"),
        ("why would jam skip the azure ad lookup?", "why_skipped"),
        ("what does jam-ws write to the database?", "statuses"),
        ("does JAM read from a queue?", "topology"),
        ("what can I search on to find this user?", "identifiers"),
        ("which log lines does it produce?", "journey_stage"),
        ("is that a real problem?", "not_an_error"),
        ("the jam log file shows nothing, where else can I look?", "evidence_outside_logs"),
        ("can I find out who gave this user admin?", "trap"),
        ("the Search button does nothing when I click it", "user_actions"),
    ],
)
def test_detects_the_question_kind(question, expected):
    assert expected in kr.detect_kinds(question)


def test_escalation_beats_a_quoted_status_code():
    """"401 Not authorized, whose problem is it?" is an escalation question that
    merely quotes a code — the escalation rule must be checked first."""
    kinds = kr.detect_kinds("jam is returning 401 Not authorized, whose problem is it?")
    assert kinds[0] == "escalation"


def test_status_code_beats_a_definition_question():
    """"404 ... what does that mean?" is about the RULE, not a definition."""
    kinds = kr.detect_kinds("jam returned 404 for a username, what does that mean?")
    assert kinds[0] == "rule"


def test_unrecognised_phrasing_detects_no_kind():
    """D7: no rule matching must mean "search wider", never "refuse"."""
    assert kr.detect_kinds("the user lost all their permissions yesterday") == []


# --- retrieval assembly (fake index, no encoder) ------------------------------
class _FakeIndex:
    """Records the filters it was asked for and returns canned hits."""

    def __init__(self, records: list[dict]):
        self.records = records
        self.calls: list[dict | None] = []

    def retrieve(self, query, k=5, filters=None, **kw):
        self.calls.append(filters)
        hits = [
            r
            for r in self.records
            if not filters or all(str(r["metadata"].get(a)) == str(b) for a, b in filters.items())
        ]
        return [dict(h) for h in hits][:k]


def _rec(rid, service, kind, score):
    return {"id": rid, "score": score, "metadata": {"service": service, "kind": kind}}


def test_named_service_filters_out_other_services(registry):
    index = _FakeIndex(
        [
            _rec("order-engine#a", "order-engine", "trap", 0.9),
            _rec("jam-ws#b", "jam-ws", "trap", 0.4),
        ]
    )
    hits = kr.retrieve_docs(index, "can I trust this in jam-ws?", registry, k=5)
    assert [h["id"] for h in hits] == ["jam-ws#b"]


def test_no_service_named_searches_everything(registry):
    """D7: an unnamed service costs precision, never the answer."""
    index = _FakeIndex([_rec("order-engine#a", "order-engine", "trap", 0.9)])
    hits = kr.retrieve_docs(index, "why did it fail?", registry, k=5)
    assert [h["id"] for h in hits] == ["order-engine#a"]
    # No call may narrow by service — the kind filter is free to apply.
    assert all("service" not in (f or {}) for f in index.calls)


def test_soft_mode_ranks_kind_matches_first_but_keeps_the_rest(registry):
    """A mis-detected kind must cost ranking, never the answer."""
    index = _FakeIndex(
        [
            _rec("jam-ws#trap", "jam-ws", "trap", 0.95),
            _rec("jam-ws#what", "jam-ws", "what_it_does", 0.20),
        ]
    )
    hits = kr.retrieve_docs(index, "what does jam-ws do?", registry, k=5, kind_mode="soft")
    ids = [h["id"] for h in hits]
    assert ids[0] == "jam-ws#what"     # kind match wins despite a far lower score
    assert "jam-ws#trap" in ids        # ...and the other one is still reachable


def test_hard_mode_drops_everything_outside_the_kind(registry):
    index = _FakeIndex(
        [
            _rec("jam-ws#trap", "jam-ws", "trap", 0.95),
            _rec("jam-ws#what", "jam-ws", "what_it_does", 0.20),
        ]
    )
    hits = kr.retrieve_docs(index, "what does jam-ws do?", registry, k=5, kind_mode="hard")
    assert [h["id"] for h in hits] == ["jam-ws#what"]


def test_kind_mode_off_ignores_the_question_kind(registry):
    index = _FakeIndex(
        [
            _rec("jam-ws#trap", "jam-ws", "trap", 0.95),
            _rec("jam-ws#what", "jam-ws", "what_it_does", 0.20),
        ]
    )
    hits = kr.retrieve_docs(index, "what does jam-ws do?", registry, k=5, kind_mode="off")
    assert [h["id"] for h in hits] == ["jam-ws#trap", "jam-ws#what"]


def test_undocumented_service_searches_its_see_also_doc_only(registry):
    """A cc-checker-service question must NOT fall through to all five docs.

    The observed failure: a margin-check alert cited `rsm-ws#key-concepts--
    availablequantity` and an order-validator "concepts this document does not
    cover" section — both pulled in purely because the question said "means" and
    "concepts" sections are full of that word.
    """
    index = _FakeIndex(
        [
            _rec("order-engine#margin", "order-engine", "rule", 0.50),
            _rec("rsm-ws#concepts", "rsm-ws", "concept", 0.95),
        ]
    )
    hits = kr.retrieve_docs(
        index,
        "Regarding this incident: ERROR cc-checker-service margin check FAILED\n\n"
        "Question: what does this mean?",
        registry,
        k=5,
    )
    assert [h["id"] for h in hits] == ["order-engine#margin"]


def test_undocumented_service_with_no_doc_returns_nothing(registry):
    """"I have no documentation for SPT" must be deterministic, not a lucky miss.

    Deliberately NOT the D7 fallback: D7 widens an UNRECOGNISED question. Here the
    question was recognised, and we positively know there is no documentation.
    """
    index = _FakeIndex([_rec("rsm-ws#anything", "rsm-ws", "concept", 0.99)])
    assert kr.retrieve_docs(index, "what does SPT do?", registry, k=5) == []


def test_a_documented_service_still_wins_over_an_undocumented_one(registry):
    """Naming both must narrow to the documented one, not to a see_also."""
    index = _FakeIndex(
        [
            _rec("jam-ws#a", "jam-ws", "trap", 0.4),
            _rec("order-engine#b", "order-engine", "trap", 0.9),
        ]
    )
    hits = kr.retrieve_docs(index, "jam-ws called SPT and it timed out", registry, k=5)
    assert [h["id"] for h in hits] == ["jam-ws#a"]


def test_folded_in_component_routes_to_its_emitter(registry):
    index = _FakeIndex(
        [
            _rec("order-engine#solr", "order-engine", "flow", 0.4),
            _rec("rsm-ws#x", "rsm-ws", "flow", 0.9),
        ]
    )
    hits = kr.retrieve_docs(index, "where are the SOLR logs?", registry, k=5)
    assert [h["id"] for h in hits] == ["order-engine#solr"]


def test_results_are_capped_at_k(registry):
    index = _FakeIndex([_rec(f"jam-ws#{i}", "jam-ws", "trap", 0.9 - i / 100) for i in range(9)])
    assert len(kr.retrieve_docs(index, "can I trust jam-ws?", registry, k=3)) == 3


def test_a_missing_service_map_degrades_to_frontmatter_only(tmp_path):
    """A broken or absent map must lose mock app_names, never crash the loader."""
    (tmp_path / "jam-ws.md").write_text(
        '```yaml\nservice: "jam-ws"\naliases: ["JAM"]\n```\n\n# jam-ws\n\n## 1. What this service does\n\nhi\n',
        encoding="utf-8",
    )
    registry = kr.build_registry(tmp_path)
    assert registry.documented == ["jam-ws"]
    assert registry.services["jam-ws"].mock_app_name is None
