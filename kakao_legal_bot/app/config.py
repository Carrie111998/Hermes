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
from urllib.parse import quote


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


# Sensible model per provider so `LLM_PROVIDER=gemini` alone does the right
# thing — forgetting LLM_MODEL should not send a Claude id to Google.
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-3.7-flash",
    "openrouter": "google/gemini-3.7-flash",
}

# Where each provider's credentials and endpoint live.
PROVIDER_CREDENTIALS = {
    "anthropic": ("anthropic_api_key", "anthropic_base_url", "ANTHROPIC_API_KEY"),
    "openai": ("openai_api_key", "openai_base_url", "OPENAI_API_KEY"),
    "gemini": ("gemini_api_key", "gemini_base_url", "GEMINI_API_KEY"),
    "openrouter": ("openrouter_api_key", "openrouter_base_url", "OPENROUTER_API_KEY"),
}


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
    # OpenRouter speaks the OpenAI wire format, so it reuses that code path.
    # Only the max-tokens field name and the optional attribution headers
    # differ — see llm.py.
    openrouter_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    openrouter_base_url: str = field(
        default_factory=lambda: _env(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        ).rstrip("/")
    )
    openrouter_referer: str = field(default_factory=lambda: _env("OPENROUTER_REFERER"))
    openrouter_title: str = field(default_factory=lambda: _env("OPENROUTER_TITLE", "moa-legal-bot"))
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_base_url: str = field(
        default_factory=lambda: _env(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
    )
    # Empty means "use the provider's default" — see `llm_model` below. Set
    # LLM_MODEL to pin an exact id.
    llm_model_override: str = field(default_factory=lambda: _env("LLM_MODEL"))
    llm_draft_model: str = field(default_factory=lambda: _env("LLM_DRAFT_MODEL", ""))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 2000))
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.2))
    llm_max_tool_rounds: int = field(default_factory=lambda: _env_int("LLM_MAX_TOOL_ROUNDS", 4))
    # Who writes document drafts:
    #   "llm"    — this server, via the same API key as the chat replies.
    #   "worker" — queued for the Codex worker on the lawyer's own PC, which
    #              uses their ChatGPT subscription. Nobody is waiting on a
    #              draft, so moving it off the server costs nothing and keeps
    #              a subscription credential out of the unattended box.
    draft_generator: str = field(default_factory=lambda: _env("DRAFT_GENERATOR", "llm").lower())
    draft_worker_token: str = field(default_factory=lambda: _env("DRAFT_WORKER_TOKEN"))
    draft_job_timeout_s: float = field(
        default_factory=lambda: _env_float("DRAFT_JOB_TIMEOUT_S", 1800.0)
    )
    draft_max_attempts: int = field(default_factory=lambda: _env_int("DRAFT_MAX_ATTEMPTS", 3))
    persona_path: Path = field(
        default_factory=lambda: Path(_env("PERSONA_PATH", "./kakao_legal_bot/persona.md"))
    )
    # 문서작성 인테이크 절차. 페르소나와 함께 캐시되는 고정 블록입니다.
    intake_playbook_path: Path = field(
        default_factory=lambda: Path(
            _env("INTAKE_PLAYBOOK_PATH", "./kakao_legal_bot/intake_playbook.md")
        )
    )

    # ── RAG ──────────────────────────────────────────────────────────────
    corpus_dir: Path = field(default_factory=lambda: Path(_env("CORPUS_DIR", "./corpus")))
    # One SQLite index per corpus lives here. Splitting them is what keeps
    # search at ~85ms instead of ~2s once the library passes a gigabyte.
    rag_dir_override: Path | None = field(
        default_factory=lambda: Path(_env("RAG_DIR")) if _env("RAG_DIR") else None
    )
    # 위키 그래프 — 같은 조문·판례를 말하는 문서끼리의 연결. 없으면 그래프
    # 검색 도구가 붙지 않을 뿐, 나머지는 그대로 돕니다.
    wiki_graph_override: Path | None = field(
        default_factory=lambda: Path(_env("WIKI_GRAPH")) if _env("WIKI_GRAPH") else None
    )
    wiki_related_limit: int = field(default_factory=lambda: _env_int("WIKI_RELATED_LIMIT", 6))
    wiki_vault: Path = field(default_factory=lambda: Path(_env("WIKI_VAULT", "./vault")))
    # 개정 법령·최근 판례를 주기적으로 확인해 변호사에게 알립니다. 서버는
    # **찾아서 알리는 데까지만** 하고, 글을 고치는 것은 PC의 코덱스 몫입니다.
    law_sync_enabled: bool = field(default_factory=lambda: _env_bool("LAW_SYNC_ENABLED", False))
    law_sync_interval_h: int = field(default_factory=lambda: _env_int("LAW_SYNC_INTERVAL_H", 24))
    law_sync_top_laws: int = field(default_factory=lambda: _env_int("LAW_SYNC_TOP_LAWS", 30))
    law_sync_precedent_days: int = field(
        default_factory=lambda: _env_int("LAW_SYNC_PRECEDENT_DAYS", 7)
    )
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
    # Three alerts on a room's *first* question — 접수 / 90초 경과 / 답변 완료 —
    # so the lawyer knows who applied and can follow the consultation. Later
    # questions in the same room stay silent; the lawyer already knows.
    lawyer_first_turn_alerts: bool = field(
        default_factory=lambda: _env_bool("LAWYER_FIRST_TURN_ALERTS", True)
    )
    lawyer_alert_preview_chars: int = field(
        default_factory=lambda: _env_int("LAWYER_ALERT_PREVIEW_CHARS", 300)
    )
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL").rstrip("/"))
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN"))
    # How a finished draft reaches the lawyer. E-mail is for the *client*;
    # the lawyer reads drafts on a phone, in the same app the consultation
    # happens in.  full | link | both | off
    draft_delivery: str = field(default_factory=lambda: _env("DRAFT_DELIVERY", "both").lower())
    # Above this, the body is not pasted into KakaoTalk — a 20-message wall
    # is unreadable and the link is better. The notice + link still go.
    draft_kakao_max_chars: int = field(
        default_factory=lambda: _env_int("DRAFT_KAKAO_MAX_CHARS", 4000)
    )

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
    def rag_dir(self) -> Path:
        return self.rag_dir_override or (self.data_dir / "rag")

    @property
    def wiki_graph_path(self) -> Path:
        return self.wiki_graph_override or (self.data_dir / "wiki.sqlite3")

    def rag_path(self, collection: str) -> Path:
        """Where one collection's index lives (``books`` → ``data/rag/books.sqlite3``)."""
        safe = "".join(ch for ch in (collection or "corpus") if ch.isalnum() or ch in "-_") or "corpus"
        return self.rag_dir / f"{safe}.sqlite3"

    @property
    def llm_model(self) -> str:
        return self.llm_model_override or DEFAULT_MODELS.get(self.llm_provider, "")

    @property
    def draft_model(self) -> str:
        return self.llm_draft_model or self.llm_model

    @property
    def llm_credentials(self) -> tuple[str, str]:
        """``(api_key, base_url)`` for the configured provider."""
        key_field, url_field, _ = PROVIDER_CREDENTIALS.get(
            self.llm_provider, PROVIDER_CREDENTIALS["anthropic"]
        )
        return getattr(self, key_field), getattr(self, url_field)

    def admin_url(self, path: str = "") -> str:
        """A one-tap link to the lawyer's own pages, token included.

        The link is only ever sent to the lawyer's own KakaoTalk room. Making
        them type a token on a phone between hearings means the dashboard
        does not get opened, and an unread queue is worse than a link in a
        private chat. Rotate ``ADMIN_TOKEN`` if that room is ever exposed.
        """
        if not self.public_base_url or not self.admin_token:
            return ""
        separator = "&" if "?" in path else "?"
        return f"{self.public_base_url}/admin{path}{separator}token={quote(self.admin_token)}"

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
        if self.llm_provider not in PROVIDER_CREDENTIALS:
            missing.append(f"LLM_PROVIDER (알 수 없는 값: {self.llm_provider})")
        else:
            key, _url = self.llm_credentials
            if not key:
                missing.append(PROVIDER_CREDENTIALS[self.llm_provider][2])
            if not self.llm_model:
                missing.append("LLM_MODEL")
        if self.iris_send_mode in {"direct", "hybrid"} and not self.iris_base_url:
            missing.append("IRIS_BASE_URL")
        if self.iris_send_mode in {"poll", "hybrid"} and not self.outbox_token:
            missing.append("OUTBOX_TOKEN")
        if self.draft_generator == "worker" and not self.draft_worker_token:
            missing.append("DRAFT_WORKER_TOKEN")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests mutate os.environ; this drops the memoised Settings."""
    get_settings.cache_clear()
