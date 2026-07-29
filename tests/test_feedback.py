"""Tests for backend/feedback.py — thumbs up/down scoring and the ranking boost.

Everything here is pure: no DB, no network. The point of these tests is the BIAS
handling, because "sort by likes" fails in three specific ways and each
countermeasure needs pinning:

  1. sparse feedback  — most answers are never rated, so silence must be NEUTRAL
  2. small-sample noise — 3/3 must not outrank 8/10
  3. rich-get-richer  — old votes decay; the blend is a nudge, not a re-rank

Plus the attribution question: a vote rates an ANSWER, and every cited record gets
EQUAL credit — rank-weighting was measured to make the boost a copy of the cosine
order, so it could never reorder anything (see rank_weights).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.feedback import (
    MAX_AGE_DAYS,
    MIN_VOTES,
    NEUTRAL,
    accumulate,
    bayesian_score,
    boosts_from,
    decay_weight,
    display_counts,
    rank_weights,
)

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


def _row(vote: int, ids: list[str], age_days: float = 0.0):
    """A stand-in for a ChatFeedback row (only three attributes are read)."""
    return SimpleNamespace(
        vote=vote, record_ids=ids, created_at=NOW - timedelta(days=age_days)
    )


# =============================================================================
# bias 1 — sparse feedback: silence must be neutral, never negative
# =============================================================================
def test_no_votes_scores_exactly_neutral():
    """THE most important property. Most records are never voted on; if silence
    scored 0 the boost would become a popularity tax on everything nobody
    happened to click."""
    assert bayesian_score(0, 0) == NEUTRAL


def test_a_record_absent_from_boosts_is_treated_as_neutral_by_the_index():
    """The index defaults a missing id to NEUTRAL_BOOST, and the two constants
    must agree or unvoted records would be silently penalised."""
    from ai_service.ragindex import NEUTRAL_BOOST

    assert NEUTRAL_BOOST == NEUTRAL


@pytest.mark.parametrize("likes", [1, 2])
def test_a_couple_of_votes_cannot_move_anything(likes):
    """Below MIN_VOTES the score stays neutral, so one enthusiast cannot promote a
    record on their own."""
    assert likes < MIN_VOTES
    assert bayesian_score(likes, 0) == NEUTRAL


# =============================================================================
# bias 2 — small-sample noise
# =============================================================================
def test_more_evidence_is_never_beaten_by_a_perfect_tiny_sample():
    """Regression on the prior strength. Under Beta(1,1) a 3/3 record scored 0.800
    and BEAT 8 likes / 2 dislikes (0.750) — a perfect-but-tiny sample outranking
    real evidence. Beta(2,2) is the weakest prior that fixes it."""
    assert bayesian_score(8, 2) >= bayesian_score(3, 0)


def test_scores_are_ordered_sensibly():
    strong_good = bayesian_score(10, 0)
    mixed = bayesian_score(8, 2)
    bad = bayesian_score(0, 5)
    assert strong_good > mixed > NEUTRAL > bad


def test_a_genuinely_disliked_record_scores_below_neutral():
    """The prior must smooth, not mute: a clearly bad record has to be able to sink
    below neutral or the downvote accomplishes nothing."""
    assert bayesian_score(0, 5) < 0.3


def test_score_stays_within_bounds():
    for likes, dislikes in [(0, 0), (100, 0), (0, 100), (7, 7)]:
        assert 0.0 < bayesian_score(likes, dislikes) < 1.0


# =============================================================================
# bias 3 — rich-get-richer: decay
# =============================================================================
def test_a_fresh_vote_counts_fully():
    assert decay_weight(NOW, NOW) == pytest.approx(1.0)


def test_votes_fade_with_age():
    fresh = decay_weight(NOW - timedelta(days=1), NOW)
    older = decay_weight(NOW - timedelta(days=60), NOW)
    assert fresh > older > 0.0


def test_ancient_votes_count_for_nothing():
    """Without a hard cap, an early burst of votes would dominate ranking forever
    and newer records could never catch up."""
    assert decay_weight(NOW - timedelta(days=MAX_AGE_DAYS + 1), NOW) == 0.0


def test_a_future_timestamp_is_treated_as_fresh_not_negative():
    """Clock skew must not produce a negative or >1 weight."""
    assert decay_weight(NOW + timedelta(days=5), NOW) == 1.0


def test_naive_timestamps_are_assumed_utc():
    naive = (NOW - timedelta(days=1)).replace(tzinfo=None)
    assert 0.0 < decay_weight(naive, NOW) <= 1.0


# =============================================================================
# attribution — a vote rates the ANSWER, every cited record gets equal credit
# =============================================================================
def test_every_citation_gets_equal_credit():
    """Equal, NOT 1/rank. Rank-weighting was measured to make the boost a copy of
    the cosine order — see rank_weights' docstring for the numbers — so voting
    reinforced whatever was already first and could never reorder anything."""
    assert rank_weights(3) == [1.0, 1.0, 1.0]


def test_credit_is_independent_of_retrieval_position():
    """The property that makes reordering possible: a 5th-place citation earns the
    same as a 1st-place one, so the boost reflects WHICH records appear in liked
    answers rather than where they already ranked."""
    weights = rank_weights(5)
    assert len(set(weights)) == 1


def test_rank_weights_handles_degenerate_input():
    assert rank_weights(0) == []
    assert rank_weights(1) == [1.0]


def test_every_cited_record_earns_equal_signal():
    """Attributing to the top source alone would mean ranks 2..k can never be
    promoted; attributing by rank made the boost mirror the cosine order. Equal
    credit is what leaves the boost free to disagree with retrieval."""
    totals = accumulate([_row(1, ["a", "b", "c"])], NOW)
    assert set(totals) == {"a", "b", "c"}
    assert totals["a"]["likes"] == totals["b"]["likes"] == totals["c"]["likes"] == 1.0


def test_a_dislike_lands_on_the_dislike_side():
    totals = accumulate([_row(-1, ["a"])], NOW)
    assert totals["a"]["dislikes"] == pytest.approx(1.0)
    assert totals["a"]["likes"] == 0.0


def test_rows_with_no_citations_are_skipped():
    assert accumulate([_row(1, [])], NOW) == {}


def test_expired_rows_are_skipped_entirely():
    assert accumulate([_row(1, ["a"], age_days=MAX_AGE_DAYS + 5)], NOW) == {}


def test_votes_accumulate_across_rows():
    rows = [_row(1, ["a"]), _row(1, ["a"]), _row(-1, ["a"])]
    totals = accumulate(rows, NOW)
    assert totals["a"]["likes"] == pytest.approx(2.0)
    assert totals["a"]["dislikes"] == pytest.approx(1.0)
    assert totals["a"]["votes"] == 3.0


# =============================================================================
# boosts_from — the value handed to the index
# =============================================================================
def test_boosts_are_neutral_until_there_is_enough_evidence():
    assert boosts_from([_row(1, ["a"])], NOW)["a"] == NEUTRAL


def test_a_well_liked_record_boosts_above_neutral():
    rows = [_row(1, ["a"]) for _ in range(6)]
    assert boosts_from(rows, NOW)["a"] > NEUTRAL


def test_a_disliked_record_boosts_below_neutral():
    rows = [_row(-1, ["a"]) for _ in range(6)]
    assert boosts_from(rows, NOW)["a"] < NEUTRAL


def test_no_rows_means_no_boosts():
    assert boosts_from([], NOW) == {}


def test_the_floor_counts_ratings_not_weighted_units():
    """Regression on a measured bug. The floor was compared against the WEIGHTED
    total, but rank-weighting shrinks a rank-2 citation to 0.5 per vote — so six
    real votes summed to 2.83 and were discarded as "not enough evidence", while
    the rank-1 record cleared on the same six. Only the top result could ever be
    boosted, entrenching rank 1: exactly the rich-get-richer failure the design
    exists to prevent. A rank-5 citation would have needed 15 votes.

    Observed live: 6 upvotes on a 5-source answer boosted ONLY the first record.
    """
    ids = ["r1", "r2", "r3", "r4", "r5"]
    rows = [_row(1, ids) for _ in range(6)]
    boosts = boosts_from(rows, NOW)

    # Every cited record clears the floor — six people rated all of them.
    assert all(boosts[i] > NEUTRAL for i in ids), boosts
    # ...and all equally, since credit no longer depends on citation position.
    assert len({round(boosts[i], 9) for i in ids}) == 1


def test_a_low_ranked_citation_is_not_silently_ignored():
    """The specific victim of the old behaviour: rank 5 earns 0.2 per vote, so its
    weighted total (1.2 from six votes) sat far below a floor meant to mean three
    people."""
    rows = [_row(1, ["top", "b", "c", "d", "last"]) for _ in range(6)]
    assert boosts_from(rows, NOW)["last"] > NEUTRAL


def test_the_floor_still_blocks_too_few_raters():
    """The fix must not remove the guard: two ratings is still not enough."""
    rows = [_row(1, ["a", "b"]) for _ in range(2)]
    boosts = boosts_from(rows, NOW)
    assert boosts["a"] == NEUTRAL and boosts["b"] == NEUTRAL


# =============================================================================
# display_counts — honest integers for the UI
# =============================================================================
def test_display_counts_are_integers_not_weights():
    """Ranking uses fractional rank/decay weights, but telling an agent "1.83 people
    found this helpful" is nonsense. Display counts answers, not influence."""
    counts = display_counts([_row(1, ["a", "b"]), _row(-1, ["a"])], NOW)
    assert counts["a"] == {"likes": 1, "dislikes": 1}
    assert counts["b"] == {"likes": 1, "dislikes": 0}
    assert all(isinstance(v, int) for c in counts.values() for v in c.values())


def test_display_counts_ignore_rank_position():
    """A second-place citation still counts as one answer for display, even though
    it contributes only half a vote to ranking."""
    counts = display_counts([_row(1, ["top", "second"])], NOW)
    assert counts["top"]["likes"] == counts["second"]["likes"] == 1
