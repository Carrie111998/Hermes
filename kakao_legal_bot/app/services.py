"""Wiring. One object holds every long-lived dependency.

Built once at startup and stashed on ``app.state``; tests build one with
fakes swapped in and never touch the network.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .agent import LegalAgent
from .config import Settings, get_settings
from .db import Database
from .iris import IrisClient
from .lawapi.client import CacheBackend, LawApiClient
from .llm import LlmClient
from .rag.store import RagStore
from .sender import Sender

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    db: Database
    iris: IrisClient
    sender: Sender
    agent: LegalAgent
    rag: RagStore | None = None
    law: LawApiClient | None = None
    llm: LlmClient | None = None
    # One semaphore for the whole process: LLM calls are the expensive
    # thing and an unbounded fan-out during a busy hour is how you blow
    # through a rate limit and start failing every room at once.
    semaphore: asyncio.Semaphore | None = None

    async def aclose(self) -> None:
        await self.iris.aclose()
        if self.law is not None:
            await self.law.aclose()
        if self.llm is not None:
            await self.llm.aclose()
        if self.rag is not None:
            self.rag.close()
        self.db.close()


def _make_embedder(settings: Settings) -> Any:
    """Query-side embedding function, or None when embeddings are off."""
    if not settings.rag_embeddings or not settings.openai_api_key:
        return None

    async def embed(text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{settings.openai_base_url}/embeddings",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"model": settings.rag_embedding_model, "input": text[:8000]},
            )
            response.raise_for_status()
            return list(response.json()["data"][0]["embedding"])

    return embed


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    db = Database(settings.db_path)
    rag = RagStore(settings.data_dir / "rag.sqlite3")

    law: LawApiClient | None = None
    if settings.law_api_enabled and (settings.law_oc or settings.data_go_kr_key):
        law = LawApiClient(
            oc=settings.law_oc,
            service_key=settings.data_go_kr_key,
            timeout_s=settings.law_api_timeout_s,
            cache_ttl_s=settings.law_cache_ttl_s,
            cache=CacheBackend(get=db.cache_get, put=db.cache_put),
        )
    elif settings.law_api_enabled:
        log.warning("LAW_OC/DATA_GO_KR_KEY unset — 법령·판례 검색 도구 없이 동작합니다")

    api_key, base_url = settings.llm_credentials
    llm = LlmClient(
        provider=settings.llm_provider,
        api_key=api_key,
        base_url=base_url,
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        max_tool_rounds=settings.llm_max_tool_rounds,
        # Must cover the *whole* budget including the extension — an HTTP
        # timeout at 90s would make the promised 3 extra minutes a lie.
        timeout_s=max(settings.total_answer_budget_s, 30.0),
    )

    iris = IrisClient(settings)
    agent = LegalAgent(settings, llm, rag=rag, law=law, embed_query=_make_embedder(settings))

    return Services(
        settings=settings,
        db=db,
        iris=iris,
        sender=Sender(settings, db, iris),
        agent=agent,
        rag=rag,
        law=law,
        llm=llm,
        semaphore=asyncio.Semaphore(max(1, settings.global_concurrency)),
    )
