"""Advisory, cross-tenant lead-research quality and cost reporting for admins."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..db import json_load, now


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _int(value: Any) -> int:
    return int(_number(value))


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


class ResearchQualityService:
    """Build one read-only report from durable research accounting tables.

    This is deliberately advisory.  It reads the same immutable decisions and
    counters an operator would inspect manually; it never mutates a profile,
    label, source, campaign, or score.
    """

    def __init__(self, db) -> None:
        self.db = db

    def _overall_metrics(self) -> tuple[list[dict], dict[str, int]]:
        rows = []
        totals: dict[str, int] = defaultdict(int)
        for row in self.db.all(
            "SELECT company_id,campaign_id,metrics FROM campaign_metrics "
            "WHERE dimension='overall' AND dimension_value='all'"
        ):
            metrics = json_load(row["metrics"], {})
            rows.append({"company_id": row["company_id"], "campaign_id": row["campaign_id"], **metrics})
            for key, value in metrics.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] += int(value)
        return rows, dict(totals)

    def _profiles(self) -> tuple[dict, list[dict]]:
        rows = self.db.all(
            "SELECT id,company_id,status,profile_json FROM company_profile_versions ORDER BY company_id,version"
        )
        thin = []
        for row in rows:
            if row["status"] != "confirmed":
                continue
            profile = json_load(row["profile_json"], {})
            if not profile.get("products") or not profile.get("playbook_versions"):
                thin.append({
                    "code": "thin_profile",
                    "company_id": row["company_id"],
                    "profile_version_id": row["id"],
                    "message": "Confirmed research profile has no product range or sector playbook.",
                })
        return {
            "versions": len(rows),
            "confirmed": sum(row["status"] == "confirmed" for row in rows),
            "thin": len(thin),
        }, thin

    def _facts(self) -> tuple[dict, list[dict]]:
        usage = self.db.all(
            "SELECT shared_fact_id,COUNT(DISTINCT company_id) AS consumers "
            "FROM research_fact_consumers GROUP BY shared_fact_id ORDER BY shared_fact_id"
        )
        warnings = [{
            "code": "high_fact_reuse",
            "fact_id": row["shared_fact_id"],
            "consumers": row["consumers"],
            "message": f"A shared fact affects {row['consumers']} customer workspaces.",
        } for row in usage if row["consumers"] >= 2]
        return {
            "shared_facts": self.db.one("SELECT COUNT(*) AS n FROM shared_facts")["n"],
            "tenant_facts": self.db.one("SELECT COUNT(*) AS n FROM tenant_facts")["n"],
            "reused_facts": sum(row["consumers"] >= 2 for row in usage),
            "max_consumers": max((row["consumers"] for row in usage), default=0),
            "consumer_links": sum(row["consumers"] for row in usage),
        }, warnings

    def _sources(self) -> tuple[list[dict], list[dict], int]:
        definitions = {
            (row["company_id"], row["source_id"]): row
            for row in self.db.all(
                "SELECT company_id,source_id,health,updated_at FROM dataset_definitions"
            )
        }
        grouped: dict[str, dict] = {}
        provider_errors = 0
        for row in self.db.all(
            "SELECT company_id,source_id,status,metrics,error_category FROM campaign_partitions"
        ):
            metrics = json_load(row["metrics"], {})
            item = grouped.setdefault(row["source_id"], {
                "source_id": row["source_id"], "requests": 0, "failures": 0,
                "cache_hits": 0, "partitions": 0, "status": "active",
            })
            item["requests"] += _int(metrics.get("provider_requests"))
            item["cache_hits"] += _int(metrics.get("reused_candidates"))
            item["partitions"] += 1
            errors = len(metrics.get("errors") or [])
            if row["status"] in {"failed", "partial", "skipped"}:
                item["failures"] += max(1, errors)
                provider_errors += max(1, errors)
        warnings = []
        for (_, source_id), row in definitions.items():
            item = grouped.setdefault(source_id, {
                "source_id": source_id, "requests": 0, "failures": 0,
                "cache_hits": 0, "partitions": 0, "status": row["health"],
            })
            if row["health"] != "active":
                item["status"] = row["health"]
                warnings.append({
                    "code": "source_change", "source_id": source_id,
                    "company_id": row["company_id"], "status": row["health"],
                    "message": f"Source health changed to {row['health']}.",
                })
        return sorted(grouped.values(), key=lambda item: item["source_id"]), warnings, provider_errors

    def _agentic(self) -> tuple[dict, dict]:
        totals = {
            "companies": 0, "pages": 0, "tokens": 0, "requests": 0,
            "elapsed_seconds": 0.0, "budget_stops": 0, "cost": 0.0,
        }
        budget_reasons = {"page_limit", "request_limit", "time_limit", "token_limit"}
        for row in self.db.all(
            "SELECT status,output,cost,started_at,completed_at FROM agent_runs "
            "WHERE run_type='lead_research_gap'"
        ):
            output = json_load(row["output"], {})
            totals["companies"] += 1
            totals["pages"] += len(output.get("pages") or [])
            totals["tokens"] += _int(output.get("tokens_used"))
            totals["requests"] += _int(output.get("requests_started"))
            totals["cost"] += _number(row["cost"])
            if row["started_at"] and row["completed_at"]:
                totals["elapsed_seconds"] += max(0, row["completed_at"] - row["started_at"])
            if output.get("stop_reason") in budget_reasons:
                totals["budget_stops"] += 1
        totals["elapsed_seconds"] = round(totals["elapsed_seconds"], 3)
        totals["cost"] = round(totals["cost"], 6)
        return {
            key: totals[key] for key in (
                "companies", "pages", "elapsed_seconds", "budget_stops"
            )
        }, totals

    def _outcomes(self) -> dict:
        results = self.db.all(
            "SELECT id,lead_id,data FROM research_results WHERE lead_id IS NOT NULL"
        )
        lead_band = {}
        result_lead = {}
        for row in results:
            data = json_load(row["data"], {})
            score = data.get("score") or data
            band = score.get("priority_band") or "Rejected"
            lead_band[row["lead_id"]] = band
            result_lead[row["id"]] = row["lead_id"]
        message_outcomes: dict[str, dict] = defaultdict(
            lambda: {"sent": False, "replied": False, "bounced": False}
        )
        for row in self.db.all(
            "SELECT lead_id,sent_at,replied_at,bounced_at FROM outreach_messages "
            "WHERE lead_id IS NOT NULL"
        ):
            outcome = message_outcomes[row["lead_id"]]
            outcome["sent"] = outcome["sent"] or bool(row["sent_at"])
            outcome["replied"] = outcome["replied"] or bool(row["replied_at"])
            outcome["bounced"] = outcome["bounced"] or bool(row["bounced_at"])

        def summarize(groups: dict[str, set[str]], key_name: str) -> list[dict]:
            output = []
            for key, lead_ids in sorted(groups.items()):
                contacted = sum(message_outcomes[lead_id]["sent"] for lead_id in lead_ids)
                replied = sum(message_outcomes[lead_id]["replied"] for lead_id in lead_ids)
                bounced = sum(message_outcomes[lead_id]["bounced"] for lead_id in lead_ids)
                output.append({
                    key_name: key, "leads": len(lead_ids), "contacted": contacted,
                    "replied": replied, "bounced": bounced,
                    "reply_rate": _rate(replied, contacted),
                    "bounce_rate": _rate(bounced, contacted),
                })
            return output

        by_band: dict[str, set[str]] = defaultdict(set)
        for lead_id, band in lead_band.items():
            by_band[band].add(lead_id)
        by_label: dict[str, set[str]] = defaultdict(set)
        for row in self.db.all(
            "SELECT result_id,label_id,value FROM research_label_assignments "
            "WHERE effective_until IS NULL"
        ):
            lead_id = result_lead.get(row["result_id"])
            if lead_id:
                by_label[f"{row['label_id']}={row['value']}"] .add(lead_id)
        return {"by_band": summarize(by_band, "band"), "by_label": summarize(by_label, "label")}

    def _score_warnings(self) -> list[dict]:
        warnings = []
        for row in self.db.all(
            "SELECT id,company_id,evidence_confidence,snapshot_json FROM research_results"
        ):
            snapshot = json_load(row["snapshot_json"], {})
            score = snapshot.get("score") or {}
            known = score.get("known_weight")
            if row["evidence_confidence"] < .4 or (known is not None and known < 50):
                warnings.append({
                    "code": "thin_evidence", "result_id": row["id"],
                    "company_id": row["company_id"],
                    "message": "A lead decision is based on thin evidence coverage.",
                })
        previous: dict[tuple[str, str], int] = {}
        for row in self.db.all(
            "SELECT company_id,organization_id,snapshot_json,created_at "
            "FROM research_score_snapshots ORDER BY company_id,organization_id,created_at,id"
        ):
            score = json_load(row["snapshot_json"], {}).get("score") or {}
            current = score.get("fit_score")
            key = (row["company_id"], row["organization_id"])
            if isinstance(current, (int, float)) and key in previous and abs(current - previous[key]) >= 25:
                warnings.append({
                    "code": "sharp_score_change", "company_id": row["company_id"],
                    "organization_id": row["organization_id"],
                    "change": int(current - previous[key]),
                    "message": "A company fit score changed by at least 25 points.",
                })
            if isinstance(current, (int, float)):
                previous[key] = int(current)
        return warnings

    def report(self) -> dict:
        metric_rows, metrics = self._overall_metrics()
        profiles, profile_warnings = self._profiles()
        facts, fact_warnings = self._facts()
        sources, source_warnings, provider_errors = self._sources()
        agentic, agentic_totals = self._agentic()
        attempts = self.db.all(
            "SELECT status,request_count FROM research_search_attempts"
        )
        labels_history = self.db.one("SELECT COUNT(*) AS n FROM research_label_assignments")["n"]
        corrections = self.db.all("SELECT applied FROM research_fact_corrections")
        contact_rows = self.db.all("SELECT data,verification_tier FROM contacts")
        contacts_derived = sum(
            row["verification_tier"] == "yellow"
            or (json_load(row["data"], {}).get("address_source") == "derived_pattern")
            or (json_load(row["data"], {}).get("verification_tier") == "yellow")
            for row in contact_rows
        )
        exclusions = {
            "excluded_by_range": metrics.get("candidate_supply_excluded_by_range", 0),
            "cheap_verification_no_scope_signal": metrics.get(
                "candidate_supply_cheap_verification_no_scope_signal", 0
            ),
            "closed_company": metrics.get("excluded_closed", 0),
            "ineligible": max(0, metrics.get("stage_identified", 0) - metrics.get("eligible_companies", 0)),
            "rejected": self.db.one(
                "SELECT COUNT(*) AS n FROM research_results WHERE verdict='reject'"
            )["n"],
        }
        costs = {
            "requests": metrics.get("provider_requests", 0) + agentic_totals["requests"],
            "retries": sum(max(0, _int(row["request_count"]) - 1) for row in attempts),
            "fresh_cache_hits": metrics.get("reused_bundles", 0),
            "negative_cache_hits": sum(row["status"] in {"empty", "failed"} for row in attempts),
            "failures": sum(row["status"] == "failed" for row in attempts),
            "tokens": agentic_totals["tokens"],
            "cost": agentic_totals["cost"],
        }
        warnings = [
            *profile_warnings, *fact_warnings, *source_warnings, *self._score_warnings(),
        ]
        warnings.sort(key=lambda item: (
            item["code"], item.get("company_id", ""), item.get("fact_id", ""),
            item.get("source_id", ""), item.get("result_id", ""),
        ))
        return {
            "generated_at": now(),
            "warnings": warnings,
            "candidates": {
                "supplied": metrics.get("candidate_supply_supplied", metrics.get("raw_records", 0)),
                "collapsed_rows": metrics.get("candidate_supply_duplicates_collapsed", 0),
                "campaigns": len(metric_rows),
            },
            "exclusions": exclusions,
            "facts": facts,
            "profiles": profiles,
            "labels": {
                "history": labels_history,
                "active": self.db.one(
                    "SELECT COUNT(*) AS n FROM research_label_assignments WHERE effective_until IS NULL"
                )["n"],
            },
            "corrections": {
                "previews": sum(not bool(row["applied"]) for row in corrections),
                "applied": sum(bool(row["applied"]) for row in corrections),
            },
            "costs": costs,
            "agentic": agentic,
            "contacts": {"derived": contacts_derived},
            "operations": {
                "cancellations": self.db.one(
                    "SELECT COUNT(*) AS n FROM research_campaigns WHERE status='cancelled'"
                )["n"],
                "provider_errors": provider_errors,
            },
            "sources": sources,
            "outcomes": self._outcomes(),
        }
