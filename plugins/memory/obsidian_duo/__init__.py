"""Hermes memory-provider adapter for the Obsidian Memory Duo broker."""

from __future__ import annotations

from pathlib import Path
import json

from agent.memory_provider import MemoryProvider

from .config import ObsidianDuoConfig
from .broker import EmbeddedMemoryBroker
from .client import EmbeddedMemoryBrokerClient
from .contracts import (
    Authority,
    MemoryCandidate,
    MemoryEvent,
    RetrievalRequest,
    Verification,
)
from agent.memory_provenance import (
    EXPLICIT_FORGET,
    EXPLICIT_REMEMBER,
    EXPLICIT_UPDATE,
)
from .inference import MemoryInference
from .policy import MemoryPolicy
from .retrieval import MemoryRetriever
from .store import SqliteMemoryStore
from .sync import CommandSyncAdapter, NoopSyncAdapter
from .vault import ObsidianVault


MEMORY_DUO_SCHEMA = {
    "name": "memory_duo",
    "description": (
        "Search Hermes deep Obsidian memory, propose a durable memory candidate, "
        "or inspect memory status. Proposals are policy-reviewed; this tool cannot "
        "force a direct durable commit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "propose", "status"]},
            "query": {"type": "string"},
            "content": {"type": "string"},
            "memory_type": {"type": "string"},
            "project": {"type": "string"},
        },
        "required": ["action"],
    },
}


class ObsidianDuoMemoryProvider(MemoryProvider):
    def __init__(self, llm=None):
        self._llm = llm
        self._broker = None
        self._hermes_home = None

    @property
    def name(self) -> str:
        return "obsidian_duo"

    def is_available(self) -> bool:
        return ObsidianDuoConfig.find_config() is not None

    def initialize(self, session_id: str, **kwargs) -> None:
        self._hermes_home = Path(kwargs["hermes_home"])
        config = ObsidianDuoConfig.load(self._hermes_home)
        self._config = config
        store = SqliteMemoryStore(self._hermes_home / "obsidian_duo" / "memory.db")
        vault = ObsidianVault(Path(config.vault_path), config.managed_folder)
        retriever = MemoryRetriever(store)
        sync_adapter = (
            CommandSyncAdapter(config.sync_command, config.sync_debounce_seconds)
            if config.sync_mode == "command" and config.sync_command
            else NoopSyncAdapter()
        )
        broker = EmbeddedMemoryBroker(
            config=config,
            store=store,
            vault=vault,
            policy=MemoryPolicy(),
            retriever=retriever,
            inference=(
                MemoryInference(self._llm)
                if self._llm and config.inference_mode != "disabled"
                else None
            ),
            sync_adapter=sync_adapter,
        )
        broker.start()
        self._broker = EmbeddedMemoryBrokerClient(broker)

    def get_tool_schemas(self):
        return [MEMORY_DUO_SCHEMA]

    def get_config_schema(self):
        return [
            {"key": "vault_path", "description": "Obsidian vault path", "required": True},
            {"key": "managed_folder", "description": "Managed memory folder", "default": "Hermes Memory"},
        ]

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._broker or not query or query.strip().lower() in {"thanks", "thank you", "ok", "okay"}:
            return ""
        packet = self._broker.retrieve(RetrievalRequest(
            query=query,
            session_id=session_id,
            max_memories=self._config.recall_max_memories,
            max_tokens=self._config.recall_max_tokens,
        ))
        if packet.no_verified_memory:
            return ""
        lines = [
            f"[{memory.memory_id}] authority={memory.authority.value} verification={memory.verification.value} "
            f"confidence={memory.confidence:.2f} {memory.content}"
            for memory in packet.memories
        ]
        if packet.conflicts:
            lines.append("Unresolved conflicts: " + ", ".join(packet.conflicts))
        return "\n".join(lines)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if self._broker:
            self._broker.observe(MemoryEvent("turn", user_content + "\n" + assistant_content, session_id=session_id))

    def on_session_end(self, messages):
        if self._broker:
            self._broker.observe(MemoryEvent("session_end", session_id=""))

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        if self._broker:
            self._broker.observe(MemoryEvent("session_switch", session_id=new_session_id))

    def on_pre_compress(self, messages) -> str:
        if self._broker:
            self._broker.flush("pre_compress", 2.0)
        return ""

    def on_memory_write(self, action, target, content, metadata=None) -> None:
        if self._broker:
            metadata = dict(metadata or {})
            intent = str(metadata.get("user_memory_intent") or "none")
            host_confirmed = (
                metadata.get("host_confirmed_user_memory") is True
                and intent in {EXPLICIT_REMEMBER, EXPLICIT_UPDATE, EXPLICIT_FORGET}
            )
            authority = Authority.USER if host_confirmed else Authority.AGENT
            verification = Verification.USER_CONFIRMED if authority is Authority.USER else Verification.UNVERIFIED
            requested_type = str(metadata.get("memory_type") or "").strip().lower()
            if requested_type not in ObsidianVault.MEMORY_TYPE_FOLDERS:
                requested_type = "preference" if target == "user" else "fact"
            provenance = {
                "source_session_id": metadata.get("session_id", ""),
                "task_id": metadata.get("task_id", ""),
                "project_id": metadata.get("project_id", ""),
                "mission_id": metadata.get("mission_id", ""),
                "agent_id": metadata.get("agent_id", ""),
            }
            candidate_metadata = {**metadata, **provenance, "event_kind": "builtin_memory_write"}
            if host_confirmed and intent in {EXPLICIT_UPDATE, EXPLICIT_FORGET}:
                candidate_metadata["event_kind"] = "user_correction"

            old_text = str(metadata.get("old_text") or "")
            if host_confirmed and action == "replace":
                matches = self._broker.find_active_by_content(old_text, requested_type) if old_text else []
                if len(matches) == 1:
                    candidate_metadata["contradicts"] = matches[0].memory_id
                else:
                    candidate_metadata["event_kind"] = "user_correction_pending"
                    candidate_metadata["pending_reason"] = (
                        "no unique exact active memory matched old_text"
                    )
                    host_confirmed = False
            if host_confirmed and action == "remove":
                matches = self._broker.find_active_by_content(old_text, requested_type) if old_text else []
                if len(matches) == 1:
                    self._broker.archive_memory(matches[0].memory_id, reason="explicit user forget")
                    return
                candidate_metadata["event_kind"] = "user_correction_pending"
                candidate_metadata["pending_reason"] = (
                    "no unique exact active memory matched old_text"
                )
                host_confirmed = False

            pending_content = content or old_text
            self._broker.propose(MemoryCandidate(
                content=pending_content,
                memory_type=requested_type,
                authority=authority,
                verification=verification,
                metadata=candidate_metadata,
            ), host_confirmed=host_confirmed)

    def on_delegation(self, task: str, result: str, **kwargs) -> None:
        if self._broker:
            child_session_id = kwargs.get("child_session_id", "")
            self._broker.propose(MemoryCandidate(
                content=f"Task: {task}\nResult: {result}",
                metadata={
                    "event_kind": "delegation_result",
                    "child_session_id": child_session_id,
                },
            ))

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._broker:
            return json.dumps({"error": "Memory Duo is not initialized"})
        action = args.get("action")
        if action == "search":
            packet = self._broker.retrieve(RetrievalRequest(
                query=str(args.get("query", "")),
                scope=str(args.get("project") or "global"),
            ))
            return json.dumps({"memories": [memory.__dict__ for memory in packet.memories], "conflicts": packet.conflicts, "no_verified_memory": packet.no_verified_memory}, default=str)
        if action == "propose":
            decision = self._broker.propose(MemoryCandidate(
                content=str(args.get("content", "")),
                memory_type=str(args.get("memory_type") or "fact"),
                scope=str(args.get("project") or "global"),
                authority=Authority.USER,
                verification=Verification.USER_CONFIRMED,
                metadata={"event_kind": "explicit_remember"},
            ))
            return json.dumps(decision.__dict__)
        if action == "status":
            return json.dumps(self._broker.status().__dict__)
        return json.dumps({"error": "action must be search, propose, or status"})

    def backup_paths(self):
        if not self._hermes_home:
            return []
        managed_root = (Path(ObsidianDuoConfig.load(self._hermes_home).vault_path) / ObsidianDuoConfig.load(self._hermes_home).managed_folder).resolve()
        try:
            managed_root.relative_to(self._hermes_home.resolve())
        except ValueError:
            return [managed_root]
        return []

    def shutdown(self) -> None:
        if self._broker:
            self._broker.shutdown(5.0)
            self._broker = None


def register(ctx):
    ctx.register_memory_provider(ObsidianDuoMemoryProvider(llm=ctx.llm))


__all__ = ["ObsidianDuoMemoryProvider", "register"]
