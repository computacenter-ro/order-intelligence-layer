"""Measure whether the documentation index actually finds the right chunk.

Run it (needs the encoder, so inside the ai-service container or a venv with
``requirements-ml.txt``)::

    python -m ai_service.scripts.eval_knowledge

Compare two chunk sizes in one run — this is what settles whether packing units
up to the full budget costs accuracy (rag-plan.md §10 open question 1)::

    python -m ai_service.scripts.eval_knowledge --budget 1000 --budget 400

Why this exists BEFORE the retrieval wiring (rag-plan.md §11 step 9): a change to
the corpus, the chunking rules or the threshold always *moves* retrieval, and
without a fixed question set there is no way to tell whether it moved forward.
"Looks about right" is the only evidence we would otherwise ever have.

The script deliberately reuses :class:`ai_service.ragindex.RagIndex` rather than
introducing a second store: the docs index is that class with a different cap and
no feedback blending (rag-plan.md §9). Nothing here writes to Redis or the DB.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from ai_service import knowledge_loader as kl
from ai_service import knowledge_routing as kr
from ai_service import settings
from ai_service.ragindex import RagIndex
from ai_service.semcache import load_encoder

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = REPO_ROOT / "ai_service" / "knowledge"
EVAL_FILE = REPO_ROOT / "tests" / "data" / "knowledge_eval.yaml"

# Retrieval floor for the docs index. 0.0 here ON PURPOSE: the point of this
# script is to see the true ranking, including near misses. A floor would hide
# "the right chunk was rank 2 with score 0.28" behind an empty result.
EVAL_MIN_SCORE = 0.0
TOP_K = 5


def build_index(chunks: list[kl.DocChunk], encoder) -> RagIndex:
    index = RagIndex(encoder, min_score=EVAL_MIN_SCORE, max_entries=len(chunks) + 1)
    for chunk in chunks:
        index.index(chunk.id, "doc", chunk.text, chunk.metadata)
    return index


def load_questions() -> list[dict[str, Any]]:
    data = yaml.safe_load(EVAL_FILE.read_text(encoding="utf-8"))
    return list(data.get("questions") or [])


def run_budget(
    budget: int, questions: list[dict[str, Any]], encoder, *, mode: str = "soft"
) -> dict[str, Any]:
    chunks = kl.load_corpus(KNOWLEDGE_DIR, budget=budget)
    known = {c.id for c in chunks}
    index = build_index(chunks, encoder)
    registry = kr.build_registry(KNOWLEDGE_DIR)

    # A typo in the eval file must fail loudly, not score as a retrieval miss.
    unknown = sorted({q["expect"] for q in questions} - known)
    if unknown:
        print(f"\n  !! {len(unknown)} expected id(s) do not exist at budget={budget}:")
        for bad in unknown:
            print(f"     {bad}")
        print("     (chunk ids are content-derived — re-resolve them after editing a doc)")

    rows: list[dict[str, Any]] = []
    for question in questions:
        if mode == "none":
            results = index.retrieve(question["q"], k=TOP_K)
        else:
            results = kr.retrieve_docs(
                index, question["q"], registry, k=TOP_K, kind_mode=mode
            )
        ids = [r["id"] for r in results]
        rank = ids.index(question["expect"]) + 1 if question["expect"] in ids else None
        rows.append(
            {
                "q": question["q"],
                "expect": question["expect"],
                "rank": rank,
                "top": ids[0] if ids else None,
                "top_score": results[0]["score"] if results else 0.0,
                "known": question["expect"] in known,
            }
        )

    scored = [r for r in rows if r["known"]]
    total = len(scored) or 1
    return {
        "budget": budget,
        "mode": mode,
        "chunks": len(chunks),
        "rows": rows,
        "top1": sum(1 for r in scored if r["rank"] == 1) / total,
        "top3": sum(1 for r in scored if r["rank"] and r["rank"] <= 3) / total,
        "top5": sum(1 for r in scored if r["rank"]) / total,
    }


MODE_LABEL = {
    "none": "similarity only",
    "off": "service filter",
    "soft": "service + kind (soft)",
    "hard": "service + kind (hard)",
}


def print_report(report: dict[str, Any], verbose: bool) -> None:
    print(
        f"\n=== budget {report['budget']} chars — {report['chunks']} chunks "
        f"— {MODE_LABEL[report['mode']]} ==="
    )
    for row in report["rows"]:
        if not row["known"]:
            mark, detail = "??", "expected id not in corpus"
        elif row["rank"] == 1:
            mark, detail = "OK", f"score {row['top_score']:.3f}"
        elif row["rank"]:
            mark, detail = f"#{row['rank']}", f"top was {row['top']}"
        else:
            mark, detail = "MISS", f"top was {row['top']}"
        if verbose or mark != "OK":
            print(f"  {mark:>4}  {row['q'][:62]:64s} {detail}")
    print(
        f"  --> top-1 {report['top1']:.0%}   top-3 {report['top3']:.0%}   "
        f"top-5 {report['top5']:.0%}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--budget",
        type=int,
        action="append",
        help="chunk size in characters; repeat to compare (default: the loader's)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show passes too")
    parser.add_argument(
        "--mode",
        action="append",
        choices=sorted(MODE_LABEL),
        help="retrieval mode; repeat to compare (default: soft)",
    )
    args = parser.parse_args(argv)
    budgets = args.budget or [kl.CHUNK_BUDGET_CHARS]

    model = settings.RAGINDEX_MODEL or settings.SEMCACHE_MODEL
    encoder = load_encoder(model)
    if encoder is None:
        print(
            f"No encoder ({model!r} unavailable). Install requirements-ml.txt, or run\n"
            "this inside the ai-service container where it is already installed:\n"
            "  docker compose exec ai-service python -m ai_service.scripts.eval_knowledge",
            file=sys.stderr,
        )
        return 2

    questions = load_questions()
    print(f"{len(questions)} questions, model {model}")
    modes = args.mode or ["soft"]
    reports = [
        run_budget(budget, questions, encoder, mode=mode)
        for budget in budgets
        for mode in modes
    ]
    for report in reports:
        print_report(report, args.verbose)

    if len(reports) > 1:
        print("\n=== comparison ===")
        for report in sorted(reports, key=lambda r: (-r["top1"], -r["top3"])):
            print(
                f"  budget {report['budget']:>5}  {report['chunks']:>4} chunks  "
                f"{MODE_LABEL[report['mode']]:>22}   "
                f"top-1 {report['top1']:.0%}   top-3 {report['top3']:.0%}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
