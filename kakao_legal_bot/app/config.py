"""Runtime configuration for the KakaoTalk legal-consultation bot.

Everything is env-driven so the same image runs locally and on Railway.
Nothing here reads a secret at import time beyond ``os.environ`` — call
``get_settings()`` (cached) and pass the object around.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _env_list(name: str, default: str = "") -> list[str]:
    raw = _env(name, default)
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    """Resolved configuration. Immutable — build a new one to change it."""

    # ── Storage ──────────────────────────────────────────────────────────
    # Railway: mount a volume at /data and set DATA_DIR=/data so the sqlite
    # file survives redeploys. Without a volume the container filesystem is
    # ephemeral and every deploy wipes the conversation history.
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    db_filename: str = field(default_factory=lambda: _env("DB_FILENAME", "moa.sqlite3"))
    history_turns: int = field(default_factory=lambda: _env_int("HISTORY_TURNS", 24))
    history_retention_days: int = field(
        default_factory=lambda: _env_int("HISTORY_RETENTION_DAYS", 90)
    )

    # ── Iris (KakaoTalk bridge running on the rooted emulator) ───────────
    iris_base_url: str = field(default_factory=lambda: _env("IRIS_BASE_URL").rstrip("/"))
    iris_webhook_secret: str = field(default_factory=lambda: _env("IRIS_WEBHOOK_SECRET"))
    # "direct"  → this server POSTs to Iris (needs Iris reachable from the
    #             internet: Cloudflare Tunnel / Tailscale / ngrok).
    # "poll"    → this server only queues replies; the relay script next to
    #             the emulator polls /outbox and delivers them. No inbound
    #             port on the home network required.
    # "hybrid"  → try direct, fall back to the outbox queue on failure.
    iris_send_mode: str = field(default_factory=lambda: _env("IRIS_SEND_MODE", "hybrid").lower())
    iris_timeout_s: float = field(default_factory=lambda: _env_float("IRIS_TIMEOUT_S", 8.0))
    outbox_token: str = field(default_factory=lambda: _env("OUTBOX_TOKEN"))
    kakao_max_chars: int = field(default_factory=lambda: _env_int("KAKAO_MAX_CHARS", 900))

    # ── Trigger ──────────────────────────────────────────────────────────
    bot_name: str = field(default_factory=lambda: _env("BOT_NAME", "모아"))
    bot_aliases: list[str] = field(
        default_factory=lambda: _env_list("BOT_ALIASES", "모아,moa,MOA,@모아")
    )
    # In a 1:1 consultation room every message is meant for the bot, so
    # name-calling is optional there. Group rooms always require the name.
    auto_answer_direct_rooms: bool = field(
        default_factory=lambda: _env_bool("AUTO_ANSWER_DIRECT_ROOMS", True)
    )
    ignore_senders: list[str] = field(default_factory=lambda: _env_list("IGNORE_SENDERS"))
    min_question_chars: int = field(default_factory=lambda: _env_int("MIN_QUESTION_CHARS", 2))

    # ── The 5-second rule ────────────────────────────────────────────────
    # KakaoTalk drops a bot turn that takes too long, so we always put
    # *something* in the room fast. If the real answer lands before this
    # deadline we skip the placeholder entirely (no double-message spam).
    ack_deadline_ms: int = field(default_factory=lambda: _env_int("ACK_DEADLINE_MS", 3500))
    ack_text: str = field(
        default_factory=lambda: _env(
            "ACK_TEXT",
            "네, 질문 확인했습니다 🔎 관련 법령·판례를 찾아보는 중이라 잠시만 기다려 주세요.",
        )
    )
    # A real legal answer can outrun the first budget — several law-API
    # round trips plus generation. Rather than throwing that work away at
    # 90s, the bot says how much longer it needs and keeps going; people
    # asking a legal question wait minutes without minding.
    answer_timeout_s: float = field(default_factory=lambda: _env_float("ANSWER_TIMEOUT_S", 90.0))
    answer_extension_s: float = field(
        default_factory=lambda: _env_float("ANSWER_EXTENSION_S", 180.0)
    )
    patience_text: str = field(
        default_factory=lambda: _env(
            "PATIENCE_TEXT",
            "답변을 생성하느라 시간이 걸리고 있습니다. "
            "{minutes}분내로 답변드리도록 하겠습니다. 잠시만 기다려주세요.",
        )
    )

    # ── Rate limiting ────────────────────────────────────────────────────
    room_cooldown_s: float = field(default_factory=lambda: _env_float("ROOM_COOLDOWN_S", 2.0))
    room_daily_cap: int = field(default_factory=lambda: _env_int("ROOM_DAILY_CAP", 60))
    global_concurrency: int = field(default_factory=lambda: _env_int("GLOBAL_CONCURRENCY", 4))

    # ── LLM ──────────────────────────────────────────────────────────────
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "anthropic").lower())
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_base_url: str = field(
        default_factory=lambda: _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    )
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_base_url: str = field(
        default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    )
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-sonnet-5"))
    llm_draft_model: str = field(default_factory=lambda: _env("LLM_DRAFT_MODEL", ""))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 2000))
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.2))
    llm_max_tool_rounds: int = field(default_factory=lambda: _env_int("LLM_MAX_TOOL_ROUNDS", 4))
    persona_path: Path = field(
        default_factory=lambda: Path(_env("PERSONA_PATH", "./kakao_legal_bot/persona.md"))
    )

    # ── RAG ──────────────────────────────────────────────────────────────
    corpus_dir: Path = field(default_factory=lambda: Path(_env("CORPUS_DIR", "./corpus")))
    rag_top_k: int = field(default_factory=lambda: _env_int("RAG_TOP_K", 6))
    rag_chunk_chars: int = field(default_factory=lambda: _env_int("RAG_CHUNK_CHARS", 900))
    rag_chunk_overlap: int = field(default_factory=lambda: _env_int("RAG_CHUNK_OVERLAP", 150))
    rag_embeddings: bool = field(default_factory=lambda: _env_bool("RAG_EMBEDDINGS", False))
    rag_embedding_model: str = field(
        default_factory=lambda: _env("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
    )

    # ── Korean law open APIs ─────────────────────────────────────────────
    # law.go.kr DRF uses the "OC" id (the part before @ of the e-mail you
    # registered with). data.go.kr uses a decoded service key.
    law_oc: str = field(default_factory=lambda: _env("LAW_OC"))
    data_go_kr_key: str = field(default_factory=lambda: _env("DATA_GO_KR_KEY"))
    law_api_timeout_s: float = field(default_factory=lambda: _env_float("LAW_API_TIMEOUT_S", 12.0))
    law_cache_ttl_s: int = field(default_factory=lambda: _env_int("LAW_CACHE_TTL_S", 86400))
    law_api_enabled: bool = field(default_factory=lambda: _env_bool("LAW_API_ENABLED", True))

    # ── Lawyer / escalation ──────────────────────────────────────────────
    lawyer_name: str = field(default_factory=lambda: _env("LAWYER_NAME", "담당 변호사"))
    lawyer_room_id: str = field(default_factory=lambda: _env("LAWYER_ROOM_ID"))
    lawyer_email: str = field(default_factory=lambda: _env("LAWYER_EMAIL"))
    lawyer_kakao_ids: list[str] = field(default_factory=lambda: _env_list("LAWYER_KAKAO_IDS"))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL").rstrip("/"))
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN"))

    # ── Outbound e-mail (final documents go to the client by e-mail) ─────
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _env_int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_from: str = field(default_factory=lambda: _env("SMTP_FROM"))
    smtp_starttls: bool = field(default_factory=lambda: _env_bool("SMTP_STARTTLS", True))
    smtp_ssl: bool = field(default_factory=lambda: _env_bool("SMTP_SSL", False))

    # ── Misc ─────────────────────────────────────────────────────────────
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    # Hashing salt for pseudonymising sender ids in the audit log. Change it
    # and old rows stop correlating — that is the point.
    pseudonym_salt: str = field(default_factory=lambda: _env("PSEUDONYM_SALT", "moa-default-salt"))
    store_raw_sender: bool = field(default_factory=lambda: _env_bool("STORE_RAW_SENDER", False))

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def draft_model(self) -> str:
        return self.llm_draft_model or self.llm_model

    @property
    def total_answer_budget_s(self) -> float:
        """Hard ceiling: the first budget plus the extension we promised."""
        return max(self.answer_timeout_s, 0.0) + max(self.answer_extension_s, 0.0)

    def patience_message(self) -> str:
        """"…{minutes}분내로 답변드리겠습니다" with the real number filled in."""
        minutes = max(1, round(max(self.answer_extension_s, 0.0) / 60))
        try:
            return self.patience_text.format(minutes=minutes)
        except (KeyError, IndexError, ValueError):
            # A custom PATIENCE_TEXT with stray braces must not break the
            # one message whose whole job is to keep the client waiting.
            return self.patience_text

    def missing_required(self) -> list[str]:
        """Config that must be present for the bot to do anything useful."""
        missing: list[str] = []
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if self.llm_provider == "openai" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if self.iris_send_mode in {"direct", "hybrid"} and not self.iris_base_url:
            missing.append("IRIS_BASE_URL")
        if self.iris_send_mode in {"poll", "hybrid"} and not self.outbox_token:
            missing.append("OUTBOX_TOKEN")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests mutate os.environ; this drops the memoised Settings."""
    get_settings.cache_clear()
