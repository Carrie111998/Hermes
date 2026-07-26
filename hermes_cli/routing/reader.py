"""Thread-safe, version-invalidating reader for active routing doctrine."""

from __future__ import annotations

import json
import threading
from typing import Any, Optional

from hermes_cli.routing import bootstrap, schema


_SPECIFICITY_LABELS = {
    4: "exact",
    3: "lane+rung",
    2: "lane",
    1: "default",
}


class DoctrineReader:
    """Resolve one route while rechecking the active version every call."""

    def __init__(self, db_path=None):
        self._db_path = db_path
        self._lock = threading.RLock()
        self._thread_state = threading.local()
        self._bootstrapped = False
        self._cached_version: int | None = None
        self._cached_rules: list[dict[str, Any]] = []

    def _connection(self):
        conn = getattr(self._thread_state, "connection", None)
        if conn is None:
            conn = schema.connect(self._db_path)
            self._thread_state.connection = conn
        return conn

    def _ensure_bootstrapped(self) -> None:
        if not self._bootstrapped:
            bootstrap.bootstrap_if_needed(self._db_path)
            self._bootstrapped = True

    def current_version(self) -> int:
        with self._lock:
            self._ensure_bootstrapped()
            conn = self._connection()
            row = conn.execute(
                "SELECT active_version FROM routing_doctrine_meta "
                "WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("routing doctrine has no active version")
        return int(row["active_version"])

    def _reload_if_stale(self) -> None:
        version = self.current_version()
        if version == self._cached_version:
            return
        rows = self._connection().execute(
            """
            SELECT *
              FROM routing_doctrine
             WHERE version = ?
             ORDER BY id ASC
            """,
            (version,),
        ).fetchall()
        if not rows:
            raise RuntimeError(
                f"active routing doctrine version {version} has no rules"
            )
        decoded = []
        for row in rows:
            item = dict(row)
            item["fallbacks"] = json.loads(item["fallback_chain_json"])
            item["forbid_paths"] = json.loads(item["forbid_paths_json"])
            decoded.append(item)
        self._cached_rules = decoded
        self._cached_version = version

    @staticmethod
    def _is_forbidden(
        rule: dict[str, Any],
        *,
        lane: str,
        rung: str,
    ) -> bool:
        return any(
            str(item.get("lane")) == lane
            and str(item.get("rung")) == rung
            for item in rule["forbid_paths"]
        )

    @staticmethod
    def _specificity(
        rule: dict[str, Any],
        *,
        lane: str,
        rung: str,
        complexity: str,
    ) -> int:
        rule_lane = str(rule["lane"])
        rule_rung = str(rule["rung"])
        rule_complexity = str(rule["complexity"])
        if (
            rule_lane == lane
            and rule_rung == rung
            and rule_complexity == complexity
        ):
            return 4
        if (
            rule_lane == lane
            and rule_rung == rung
            and rule_complexity == "default"
        ):
            return 3
        if (
            rule_lane == lane
            and rule_rung == "default"
            and rule_complexity == "default"
        ):
            return 2
        if (
            rule_lane == "default"
            and rule_rung == "default"
            and rule_complexity == "default"
        ):
            return 1
        return 0

    def choose(
        self,
        *,
        lane: str,
        rung: str,
        complexity: str,
        failure_history: Optional[list] = None,
    ) -> dict[str, Any]:
        del failure_history
        normalized_lane = str(lane).strip()
        normalized_rung = str(rung).strip()
        normalized_complexity = str(complexity).strip()
        if not normalized_lane or not normalized_rung or not normalized_complexity:
            raise ValueError("lane, rung and complexity must be non-empty")

        with self._lock:
            self._reload_if_stale()
            candidates = []
            for rule in self._cached_rules:
                if self._is_forbidden(
                    rule,
                    lane=normalized_lane,
                    rung=normalized_rung,
                ):
                    continue
                specificity = self._specificity(
                    rule,
                    lane=normalized_lane,
                    rung=normalized_rung,
                    complexity=normalized_complexity,
                )
                if specificity:
                    candidates.append((specificity, rule))
            if not candidates:
                raise LookupError(
                    "no routing doctrine rule matched "
                    f"{normalized_lane}/{normalized_rung}/"
                    f"{normalized_complexity}"
                )
            specificity, winner = min(
                candidates,
                key=lambda item: (
                    -item[0],
                    -int(item[1]["priority"]),
                    int(item[1]["id"]),
                ),
            )
            return {
                "provider": str(winner["primary_provider"]),
                "model": str(winner["primary_model"]),
                "fallbacks": list(winner["fallbacks"]),
                "doctrine_version": int(self._cached_version),
                "matched_rule_id": int(winner["id"]),
                "match_specificity": _SPECIFICITY_LABELS[specificity],
            }


__all__ = ["DoctrineReader"]
