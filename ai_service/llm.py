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
"""
from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from ai_service import settings


class LLMError(Exception):
    """Raised for any provider-side failure during an LLM call.

    The circuit breaker counts these; the pipeline turns them into fallback
    alerts. Node functions wrap provider exceptions in this type so the breaker
    has a single thing to catch.
    """


def _build(deployment: str) -> BaseChatModel | None:
    """Construct an Azure AI Foundry chat model for ``deployment``, or None.

    Returns ``None`` when creds/deployment are missing — never raises at build
    time, so importing/constructing the pipeline is always safe. Import of the
    Azure SDK is deferred into this function so the module imports even if the
    optional provider package is unavailable.
    """
    if not settings.llm_configured() or not deployment:
        return None
    from azure.core.credentials import AzureKeyCredential
    from langchain_azure_ai.chat_models import AzureAIChatCompletionsModel

    return AzureAIChatCompletionsModel(
        endpoint=settings.service_endpoint(),
        credential=AzureKeyCredential(settings.AZURE_AI_FOUNDRY_API_KEY),
        api_version=settings.AZURE_AI_FOUNDRY_API_VERSION,
        model_name=deployment,
    )


def explainer_model() -> BaseChatModel | None:
    """The fast/cheap model for plain-English explanations (LLM call 1)."""
    return _build(settings.AZURE_DEPLOYMENT_EXPLAINER)


def router_model() -> BaseChatModel | None:
    """The fast/cheap model for department routing (LLM call 2)."""
    return _build(settings.AZURE_DEPLOYMENT_ROUTER)


def summary_model() -> BaseChatModel | None:
    """The stronger model for journey summaries (api.py, slice 5)."""
    return _build(settings.AZURE_DEPLOYMENT_SUMMARY)


def chat_model() -> BaseChatModel | None:
    """The model for grounded chat answers (``POST /chat``).

    Falls back to the SUMMARY deployment when ``AZURE_AI_FOUNDRY_DEPLOYMENT_CHAT``
    is unset: chat composition is the same shape of job as a journey summary —
    read some context, write a short grounded narrative — so the summary
    deployment is the right default rather than a second thing to configure.
    Returns ``None`` with no creds, exactly like the other three factories, and
    ``/chat`` then serves its retrieval-only answer.
    """
    return _build(settings.AZURE_DEPLOYMENT_CHAT or settings.AZURE_DEPLOYMENT_SUMMARY)
