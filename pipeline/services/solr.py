"""cc-solr-service emitter block (CLAUDE.md [1]) — catalogue-line matching.

SOLR is the LAST stop on **Inbound's pre-creation enrichment leg**. NOTE ON
PROVENANCE: no Computacenter document mentions SOLR at all — its position here
is INFERRED (catalogue-line matching is demonstrably Inbound's job), per the
realignment spec. Do not cite this placement as documented fact.

Its logs are phase 1 — eventId only; the order (and its ids) does not exist
yet. Its ``serve`` block is the satellite's server-side log, the same shape as
``rsm.py``'s.

**Success path only, by design.** No scenario fails at SOLR — there is no
``fail_at="solr"`` in ``shared/scenarios.py`` and none should be invented here.
A flow either reaches SOLR (and it serves) or dies earlier, in which case this
block never runs.
"""
from __future__ import annotations

from pipeline.services.blocklib import emit_line, phase1_ids
from pipeline.services.profiles import profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("solr")
_LOG = "c.c.solr.service.ProductSearchService"
_LOG_INDEX = "c.c.solr.service.SolrIndexService"


@register("solr", "serve")
async def serve(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx
    ids = phase1_ids(ctx)
    product_ids = ", ".join(line.productId for line in ctx.lines)
    n = len(ctx.lines)

    await emit_line(
        emit, _PROF, logger=_LOG, level="INFO",
        message=(
            f"Resolving {n} product id(s) for account {ctx.accountNumber}: "
            f"[{product_ids}]"
        ),
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG_INDEX, level="DEBUG",
        message=(
            f"Querying product index core=products_{ctx.country.lower()} "
            f"q=productId:({product_ids.replace(', ', ' OR ')})"
        ),
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG, level="DEBUG",
        message=f"Resolved {n} of {n} product id(s) from the search index",
        ids=ids,
    )
    return True
