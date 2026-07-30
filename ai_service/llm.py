"""[3] AI Service — the ONE place all LLM/provider wiring lives.

CLAUDE.md: "All LLM/provider wiring stays in one module (Azure AI Foundry
today)." Nothing else in the service imports langchain-azure-ai; the rest of the
pipeline only ever sees a LangChain ``BaseChatModel`` (or ``None``), so swapping
providers — or running with no provider at all — is a change confined here.

Three logical models (CLAUDE.md [3] "LLM config"): a cheap *explainer*, a cheap
*router*, and a stronger *summary* model — each a separate Azure deployment.
When credentials are absent (``settings.llm_configured()`` is False) every
factory returns ``None``; callers treat ``None`` exactly like a provider outage
and take the fallback path. This is what lets the whole service run creds-free.

**Tracing.** Each factory tags its model with the logical role it plays, so runs
arrive in LangSmith already labelled ``explainer`` / ``router`` / ``summary`` /
``chat`` instead of as four indistinguishable calls to the same endpoint. The
tags are attached here via ``with_config`` for the same reason the provider SDK
is only imported here: observability is provider/plumbing concern, and the nodes
should keep receiving "a thing you can ``ainvoke``" and nothing more.
"""
from __future__ import annotations

from langchain_core.runnables import Runnable

from ai_service import settings


class LLMError(Exception):
    """Raised for any provider-side failure during an LLM call.

    The circuit breaker counts these; the pipeline turns them into fallback
    alerts. Node functions wrap provider exceptions in this type so the breaker
    has a single thing to catch.
    """


def _build(deployment: str, tags: list[str] | None = None) -> Runnable | None:
    """Construct an Azure AI Foundry chat model for ``deployment``, or None.

    Returns ``None`` when creds/deployment are missing — never raises at build
    time, so importing/constructing the pipeline is always safe. Import of the
    Azure SDK is deferred into this function so the module imports even if the
    optional provider package is unavailable.

    ``tags`` are attached with ``with_config`` so every run is labelled in
    LangSmith with the logical model that made it. The return type is ``Runnable``
    rather than ``BaseChatModel`` because ``with_config`` yields a
    ``RunnableBinding`` wrapping the model — duck-type compatible (``ainvoke``
    returns the same message object, so ``.content`` still works), but not a
    subclass. Callers only ever ``await model.ainvoke(...)``, which is exactly the
    ``Runnable`` contract.
    """
    if not settings.llm_configured() or not deployment:
        return None
    from azure.core.credentials import AzureKeyCredential
    from langchain_azure_ai.chat_models import AzureAIChatCompletionsModel

    model = AzureAIChatCompletionsModel(
        endpoint=settings.service_endpoint(),
        credential=AzureKeyCredential(settings.AZURE_AI_FOUNDRY_API_KEY),
        api_version=settings.AZURE_AI_FOUNDRY_API_VERSION,
        model_name=deployment,
    )
    # Guarded, not unconditional: `with_config(tags=None)` is a pydantic
    # ValidationError (the config's `tags` field must be a list), so an untagged
    # build has to return the bare model. Every factory below passes tags, but the
    # parameter is optional and the next caller shouldn't hit that.
    if tags is None:
        return model
    return model.with_config(tags=tags)


def explainer_model() -> Runnable | None:
    """The fast/cheap model for plain-English explanations (LLM call 1)."""
    return _build(settings.AZURE_DEPLOYMENT_EXPLAINER, tags=["explainer"])


def router_model() -> Runnable | None:
    """The fast/cheap model for department routing (LLM call 2)."""
    return _build(settings.AZURE_DEPLOYMENT_ROUTER, tags=["router"])


def summary_model() -> Runnable | None:
    """The stronger model for journey summaries (api.py, slice 5)."""
    return _build(settings.AZURE_DEPLOYMENT_SUMMARY, tags=["summary"])


def chat_model() -> Runnable | None:
    """The model for grounded chat answers (``POST /chat``).

    Falls back to the SUMMARY deployment when ``AZURE_AI_FOUNDRY_DEPLOYMENT_CHAT``
    is unset: chat composition is the same shape of job as a journey summary —
    read some context, write a short grounded narrative — so the summary
    deployment is the right default rather than a second thing to configure.
    Returns ``None`` with no creds, exactly like the other three factories, and
    ``/chat`` then serves its retrieval-only answer.

    Tagged ``chat`` even when it falls back to the SUMMARY deployment — the tag
    names the job, not the deployment, which is the distinction that makes the
    LangSmith split useful when both roles share one deployment.
    """
    return _build(
        settings.AZURE_DEPLOYMENT_CHAT or settings.AZURE_DEPLOYMENT_SUMMARY,
        tags=["chat"],
    )
