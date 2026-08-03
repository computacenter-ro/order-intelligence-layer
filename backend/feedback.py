"""[5] Core Backend — thumbs up/down on assistant answers, and the ranking boost.

Agents can rate a chat answer; those ratings then nudge retrieval so records that
produced good answers rank a little higher. Everything here is PURE and
unit-tested — the DB access lives in ``backend/api.py``, the ranking blend in
``ai_service/ragindex.py``.

Three biases make naive "sort by likes" actively worse than no feedback at all.
Each has a specific countermeasure below.

**1. Sparse feedback — most answers are never rated.**
A record with one like would outrank a record with none, but zero almost always
means "nobody clicked", not "bad". Countermeasure: a Bayesian prior. With
``PRIOR_LIKES = PRIOR_DISLIKES = 1``, an unvoted record scores exactly
:data:`NEUTRAL` (0.5) — the same as a record with a perfectly even split. Silence
is neutral, never negative.

**2. Small-sample noise.**
1/1 (100%) looks better than 8/10 (80%) but is far weaker evidence. The same
prior pulls small samples toward neutral: 1 like → 0.67, not 1.0. On top of that
:data:`MIN_VOTES` suppresses the signal entirely below a handful of votes, so one
stray click cannot move anything.

**3. Rich-get-richer.**
Boost what is liked → it ranks higher → gets seen more → gets liked more. Left
alone this freezes early accidents into permanent ranking, and a record that was
never surfaced can never earn a vote. Countermeasures: the boost is a *nudge*
blended at a low weight (``RAGINDEX_FEEDBACK_WEIGHT``, default 0.15) so relevance
still dominates and no amount of votes can promote an irrelevant record; and
:func:`decay_weight` ages votes out, so the signal tracks current quality rather
than compounding forever.

A fourth, subtler bias: **a vote rates the ANSWER, not any single record.** A
downvote may mean the LLM worded it badly, not that retrieval was wrong. So votes
are stored per answer (one ``chat_feedback`` row) and attributed to records only
as a derivation — :func:`rank_weights` splits the vote by citation rank, since the
top-cited record influenced the answer most. Storing per-record would bake this
one rule into the data and make it unchangeable later.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

# --- tunables ----------------------------------------------------------------

# Beta prior. Symmetric, so an unvoted record sits at exactly 0.5.
#
# 2/2 rather than the textbook 1/1, chosen by measurement: under Beta(1,1) a
# record with 3 likes scored 0.800 and BEAT one with 8 likes / 2 dislikes (0.750)
# — a perfect-but-tiny sample outranking real evidence, which is the small-sample
# bias this prior exists to prevent. Beta(2,2) is the weakest prior where more
# evidence wins that comparison (both 0.714). Beta(3,3) over-flattens instead:
# a genuinely bad 0/5 record climbs to 0.273, muting a signal worth hearing.
PRIOR_LIKES = 2.0
PRIOR_DISLIKES = 2.0

# The score of a record with no usable feedback. Deliberately the midpoint: an
# unvoted record must be neither rewarded nor punished, or the boost would become
# a popularity tax on everything nobody happened to click.
NEUTRAL = 0.5

# Below this many RATINGS the record scores NEUTRAL regardless of which way they
# went. Guards against one click moving the ranking.
#
# Counted as RAW answers, never as rank-weighted units: rank-weighting shrinks a
# rank-2 citation to 0.5 per vote, so a weighted floor silently demanded ~6 real
# votes at rank 2 and 15 at rank 5 — meaning only the top result could ever be
# boosted. "Three people rated it" is the intent; keep the floor on the count.
MIN_VOTES = 3.0

# Votes older than this contribute nothing; in between they fade smoothly. A like
# from months ago describes an older index and an older model.
HALF_LIFE_DAYS = 30.0
MAX_AGE_DAYS = 180.0


# --- attribution -------------------------------------------------------------
def rank_weights(n: int) -> list[float]:
    """Credit for each of ``n`` cited records from one vote: **equal, 1.0 each**.

    A vote rates the ANSWER, and every cited record contributed to it, so each gets
    the same credit.

    This deliberately replaced ``1/rank`` weighting, which was measured to defeat
    the whole feature. Because credit was proportional to retrieval position, the
    resulting boost came out monotonically aligned with cosine — a *copy* of the
    order it was supposed to be able to override:

        id         cosine   boost(1/rank)   final(w=0.15)
        91d38735   0.6263   0.7500          0.6449
        207894fb   0.6206   0.6667          0.6275
        c27511b7   0.6134   0.6250          0.6151
        9a8075d3   0.5923   0.6000          0.5935
        04518a4b   0.5805   0.5833          0.5809

    Same order in every column: five upvotes changed nothing, because voting for an
    answer reinforced whatever was already first. Rich-get-richer, moved from the
    floor into the attribution.

    Equal credit makes the boost depend on WHICH records appear in liked answers
    rather than where they happened to rank, which is what lets it reorder. The
    known cost: a 5th-place, barely-relevant citation earns as much as the record
    that actually answered the question. The relevance floor still gates admission,
    so the damage is bounded to ordering within already-relevant results.

    Kept as a function (not inlined) so the attribution rule stays one swappable
    place — the stored data is per-answer precisely to allow this to change.
    """
    return [1.0] * max(0, n)


def decay_weight(created_at: datetime, now: datetime | None = None) -> float:
    """Recency weight in [0, 1] — exponential half-life, hard zero past the cap.

    Counters rich-get-richer: without decay, an early burst of votes would
    dominate ranking forever and newer records could never catch up.
    """
    now = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = (now - created_at).total_seconds() / 86400.0
    if age_days < 0:
        return 1.0  # clock skew: treat a "future" vote as fresh, never negative
    if age_days >= MAX_AGE_DAYS:
        return 0.0
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


# --- scoring -----------------------------------------------------------------
def bayesian_score(likes: float, dislikes: float, votes: float | None = None) -> float:
    """Smoothed like-ratio in (0, 1). ``NEUTRAL`` when there is no real evidence.

    Posterior mean of a Beta(2,2) prior: ``(likes + 2) / (likes + dislikes + 4)``.
    Unvoted → 0.5; 3/3 → 0.714; 8/10 → 0.714 (evidence ties a perfect small
    sample, never loses to it); 10/10 → 0.857; 0/5 → 0.222.

    ``votes`` is the RAW number of answers that rated this record; the floor is
    applied to it, while ``likes``/``dislikes`` (rank- and decay-weighted) set the
    score. Measured bug this fixes: gating on the WEIGHTED total meant rank-2
    citations earned only 0.5 per vote, so six real votes summed to 2.83 and were
    discarded as "not enough evidence" — while the rank-1 record cleared on the
    same six. Only the top result could ever be boosted, which entrenches rank 1:
    precisely the rich-get-richer failure the design set out to avoid. A rank-5
    citation would have needed 15 votes to clear a floor meant to mean "3 people".

    ``votes=None`` falls back to the weighted total, for callers that have no raw
    count (and for the pure-math tests).
    """
    total = likes + dislikes
    evidence = total if votes is None else votes
    if evidence < MIN_VOTES:
        # Not enough evidence to say anything. Returning the raw ratio here is the
        # classic mistake: it would let a single like outrank a well-tested record.
        return NEUTRAL
    return (likes + PRIOR_LIKES) / (total + PRIOR_LIKES + PRIOR_DISLIKES)


def accumulate(rows, now: datetime | None = None) -> dict[str, dict[str, float]]:
    """Fold feedback rows into per-record weighted like/dislike totals.

    ``rows`` is any iterable of objects with ``vote`` (+1/-1), ``record_ids``
    (cited ids, IN RANK ORDER) and ``created_at``. Each row contributes
    ``rank_weight × decay_weight`` to every record it cited.

    Returns ``{record_id: {"likes": w, "dislikes": w, "votes": n}}``, where
    ``votes`` is the raw number of answers citing that record — kept separate from
    the weights precisely so a UI can show an honest integer.
    """
    totals: dict[str, dict[str, float]] = {}
    for row in rows:
        ids = list(row.record_ids or [])
        if not ids:
            continue
        decay = decay_weight(row.created_at, now)
        if decay <= 0.0:
            continue  # too old to count
        for record_id, rank_w in zip(ids, rank_weights(len(ids))):
            slot = totals.setdefault(
                str(record_id), {"likes": 0.0, "dislikes": 0.0, "votes": 0.0}
            )
            weight = rank_w * decay
            if row.vote > 0:
                slot["likes"] += weight
            else:
                slot["dislikes"] += weight
            slot["votes"] += 1.0
    return totals


def boosts_from(rows, now: datetime | None = None) -> dict[str, float]:
    """Per-record boost in (0, 1) for :meth:`RagIndex.retrieve`'s ``boosts`` arg.

    ``NEUTRAL`` for anything without enough evidence — and records absent from the
    returned dict are treated as NEUTRAL by the index too, so an unvoted record is
    never disadvantaged relative to a voted-but-mediocre one.
    """
    return {
        # votes= is the RAW answer count, so the floor means "enough people rated
        # this" rather than "enough weighted units" — see bayesian_score.
        record_id: bayesian_score(t["likes"], t["dislikes"], t["votes"])
        for record_id, t in accumulate(rows, now).items()
    }


def display_counts(rows, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Honest integer tallies for the UI — raw answer counts, no weighting.

    Deliberately separate from :func:`boosts_from`: the ranking uses fractional
    rank/decay weights, but telling an agent "1.83 people found this helpful" is
    nonsense. This counts answers, not influence.
    """
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        for record_id in list(row.record_ids or []):
            slot = counts.setdefault(str(record_id), {"likes": 0, "dislikes": 0})
            slot["likes" if row.vote > 0 else "dislikes"] += 1
    return counts
