#!/usr/bin/env python3
"""Run a DEV-only SkyAI v2 vs current PROD SkyAI comparison matrix.

The script calls the v2 canary gateway's ``/qa/compare`` endpoint. It is a
read-only QA helper: no customer, order, voucher, payment, Git, Render, Shopify,
or Discord mutations.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://skyai-v2-dev-ingress-lo4jl44wdq-ey.a.run.app"
DEFAULT_SCENARIOS_PATH = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "skyai_customer"
    / "fixtures"
    / "compare_scenarios.json"
)

CompareCaller = Callable[[str, dict[str, Any], float, str], dict[str, Any]]


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("compare_scenarios_must_be_list")
    scenarios: list[dict[str, Any]] = []
    for item in data:
        if type(item) is not dict:
            raise ValueError("compare_scenario_must_be_object")
        scenario_id = item.get("id")
        message = item.get("message")
        if type(scenario_id) is not str or not scenario_id:
            raise ValueError("compare_scenario_requires_id_and_message")
        if type(message) is not str or not message:
            raise ValueError("compare_scenario_requires_id_and_message")
        focus = item.get("focus", "")
        if type(focus) is not str:
            raise ValueError("compare_scenario_focus_must_be_string")
        history = item.get("history", [])
        if type(history) is not list:
            raise ValueError("compare_scenario_history_must_be_list")
        scenarios.append(
            {
                "id": scenario_id,
                "message": message,
                "focus": focus,
                "history": history,
            }
        )
    return scenarios


def build_compare_payload(scenario: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    if type(run_id) is not str or not run_id:
        raise ValueError("run_id_must_be_nonempty_string")
    scenario_id = scenario.get("id")
    message = scenario.get("message")
    history = scenario.get("history", [])
    if type(scenario_id) is not str or not scenario_id:
        raise ValueError("scenario_id_must_be_nonempty_string")
    if type(message) is not str or not message:
        raise ValueError("scenario_message_must_be_nonempty_string")
    if type(history) is not list:
        raise ValueError("scenario_history_must_be_list")
    conversation_id = f"skyai-v2-compare-{run_id}-{scenario_id}"
    if len(conversation_id.encode("utf-8", errors="surrogatepass")) > 256:
        raise ValueError("compare_conversation_id_exceeds_256_bytes")
    payload = {
        "conversation_id": conversation_id,
        "message": message,
        "surface": "skyai_v2_compare_matrix",
    }
    if history:
        payload["history"] = history
    return payload


def call_compare(base_url: str, payload: dict[str, Any], timeout: float, bearer_token: str = "") -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "SkyAI-v2-Compare-Matrix/0.1",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = Request(
        f"{base_url.rstrip('/')}/qa/compare",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return {
            "status": "error",
            "error": "http_error",
            "http_status": exc.code,
            "reason": exc.read().decode("utf-8", errors="replace")[:1000],
        }
    except URLError as exc:
        return {"status": "error", "error": "url_error", "reason": str(exc)[:500]}


def summarize_compare_response(scenario: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    dev = response.get("dev_v2") if isinstance(response.get("dev_v2"), dict) else {}
    prod = response.get("prod_current") if isinstance(response.get("prod_current"), dict) else {}
    cards = response.get("cards_compare") if isinstance(response.get("cards_compare"), dict) else {}
    return {
        "id": scenario["id"],
        "focus": scenario.get("focus", ""),
        "status": response.get("status"),
        "dev_status": dev.get("status"),
        "prod_status": prod.get("status"),
        "dev_cards": dev.get("cards_count", 0),
        "prod_cards": prod.get("cards_count", 0),
        "shared_urls": cards.get("shared_urls", []),
        "only_dev_urls": cards.get("only_dev_urls", []),
        "only_prod_urls": cards.get("only_prod_urls", []),
        "dev_missing_price_count": cards.get("dev_missing_price_count"),
        "prod_missing_price_count": cards.get("prod_missing_price_count"),
        "dev_missing_image_count": cards.get("dev_missing_image_count"),
        "prod_missing_image_count": cards.get("prod_missing_image_count"),
        "dev_reply_preview": _preview(dev.get("reply")),
        "prod_reply_preview": _preview(prod.get("reply")),
    }


def evaluate_side(scenario: dict[str, Any], side: dict[str, Any]) -> dict[str, Any]:
    """Evaluate QA regressions for comparison reports only.

    These checks intentionally live in the DEV-only matrix script, not in the
    SkyAI runtime. They are a review aid for real answers; they must not become
    customer-facing guards or routing logic.
    """

    scenario_id = str(scenario.get("id") or "")
    reply = _norm(side.get("reply"))
    raw_cards = side.get("cards")
    cards: list[Any] = raw_cards if isinstance(raw_cards, list) else []
    issues: list[str] = []

    if side.get("status") != "ok":
        issues.append("side_status_not_ok")

    if scenario_id == "bonus_transfer_customer":
        if _starts_with_direct_yes(reply):
            issues.append("starts_with_direct_yes_on_exception_case")
        if not _has_any(reply, ("купувач", "човекът, който купува", "който купува", "резервиращ")):
            issues.append("missing_default_bonus_owner")
        if _has_any(reply, ("автоматично за получателя", "бонусът е за получателя", "идеята е бонусът да може да зарадва")):
            issues.append("implies_automatic_recipient_bonus")
    elif scenario_id == "booknow_bonus_use_timing":
        if not _has_any(reply, ("ще бъде възстанов", "ще бъдат възстанов", "ще се възстанов")):
            issues.append("weak_or_missing_booknow_refund_language")
        if _has_any(
            reply,
            (
                "може да бъде възстанов",
                "може да бъдат възстанов",
                "могат да бъдат възстанов",
                "сумата може",
                "парите могат",
            ),
        ):
            issues.append("weak_booknow_refund_may_language")
    elif scenario_id == "free_panoramic_reservation_process":
        if not (
            _has_any(reply, ("профил", "профила"))
            and _has_any(reply, ("ваучери", "ваучер"))
            and "резервирай" in reply
            and _has_any(reply, ("свободен", "свободни", "таймслот", "час"))
            and _has_any(reply, ("онлайн", "завърш"))
        ):
            issues.append("missing_profile_vouchers_reserve_flow")
        if not _has_any(reply, ("да", "нужна", "трябва", "предварителна резервация", "изисква резервация")):
            issues.append("missing_advance_reservation_required_yes")
        if not (_has_any(reply, ("booknow", "букнау")) and _has_any(reply, ("след изпълнение", "след като бъде изпълн", "след основната"))):
            issues.append("missing_booknow_unlock_after_main_service")
        if not _has_any(reply, ("24 часа", "24ч", "24 ч")):
            issues.append("missing_24h_reservation_boundary")
        if not _has_any(reply, ("72 часа", "72ч", "72 ч")):
            issues.append("missing_72h_cancellation_boundary")
        if not (_has_any(reply, ("лошо време", "форсмажор", "метеоролог")) and _has_any(reply, ("нов", "друг", "свободен", "таймслот"))):
            issues.append("missing_weather_rebooking_flow")
        if _cards_contain_any(cards, ("mto-sport", "mto sport", "cavalon")) or _has_any(
            reply,
            ("mto-sport", "mto sport", "cavalon", "кавалон"),
        ):
            issues.append("recommends_paid_campaign_detour_product")
        if _has_any(reply, ("свържете се с пилота", "свържи се с пилота", "контакт с пилота", "пишете на пилота")):
            issues.append("presents_pilot_contact_as_normal_booking")
        if _has_any(reply, ("10-15 мин", "10–15 мин", "10 - 15 мин", "15 минути по-рано", "минути по-рано")):
            issues.append("invents_early_arrival_rule")
    elif scenario_id == "payment_methods":
        for required in ("карта", "easypay", "наложен"):
            if required not in reply:
                issues.append(f"missing_payment_method:{required}")
        if "банков" in reply and not _has_any(reply, ("не е", "няма")):
            issues.append("unclear_bank_transfer_unavailable")
    elif scenario_id == "voucher_merge":
        if not _has_any(reply, ("ръчно", "екип", "поддръжк")):
            issues.append("missing_manual_merge_escalation")
        if _has_any(
            reply,
            (
                "добави двата ваучера",
                "добавите двата ваучера",
                "добавете двата ваучера",
                "използваш два ваучера",
                "използвате два ваучера",
                "използвате стойността на двата ваучера",
                "плащане с ваучер/„имам ваучер“",
                "плащане с ваучер/имам ваучер",
            ),
        ):
            issues.append("suggests_self_service_merge_flow")
    elif scenario_id == "voucher_extend":
        if _has_any(reply, ("ако системата я показва", "ако е налична", "ако я има")):
            issues.append("weak_or_conditional_extension_availability")
        if not _has_any(reply, ("удължав", "моя ваучер", "моят ваучер", "ваучери")):
            issues.append("missing_profile_extension_flow")
    elif scenario_id == "external_voucher_issuer_boundary":
        if not _has_any(reply, ("не може да се добав", "не е съвместим", "не важи в skyvision")):
            issues.append("missing_external_voucher_boundary")
        if not _has_any(reply, ("издател", "платформ", "продавач", "доставчик")):
            issues.append("missing_external_issuer_next_step")
    elif scenario_id == "unspecified_voucher_skyvision_context":
        if _has_any(
            reply,
            (
                "издаден ли е от skyvision",
                "от skyvision ли е",
                "ваучерът от skyvision ли е",
                "или е закупен от друга платформа",
                "кой е издал ваучера",
            ),
        ):
            issues.append("asks_routine_issuer_question")
        if not _has_any(reply, ("профил", "използвай", "замени", "друго преживяване")):
            issues.append("missing_direct_skyvision_voucher_help")
    elif scenario_id == "reservation_reply_contact":
        if "info@skyvision.bg" not in reply:
            issues.append("missing_customer_reply_email")
        if "reservations@skyvision.bg" in reply:
            issues.append("presents_automated_address_to_customer")
    elif scenario_id == "gift_packaging":
        if not _has_any(reply, ("син плик", "лукс")):
            issues.append("missing_signature_blue_lux_envelope")
        if "физическа опаковка" in reply:
            issues.append("unnatural_physical_packaging_phrase")
    elif scenario_id == "gift_wish_text":
        if "поздрав" not in reply:
            issues.append("missing_greeting_field")
        if "редактирай поздрава" not in reply:
            issues.append("missing_preview_update_action")
        if _has_any(reply, ("само да не го объркаш", "име на ползвател")):
            issues.append("unnecessary_recipient_name_field_warning")
    elif scenario_id == "repeat_specific_parachute":
        if _cards_contain(cards, "балон") or ("балон" in reply and "не балон" not in reply):
            issues.append("keeps_pushing_rejected_balloon_alternative")
        if not (_cards_contain(cards, "парашут") or "парашут" in reply):
            issues.append("missing_requested_parachute_focus")
    elif scenario_id == "broad_gift_diverse":
        if _largest_card_category_count(cards) >= 3 and len(cards) >= 3:
            issues.append("low_card_category_diversity")
    elif scenario_id == "calm_friend_50_sliven":
        if _cards_contain(cards, "софия") or _cards_contain(cards, "сърница") or "софия" in reply or "сърница" in reply:
            issues.append("jumps_far_from_sliven_before_nearby_options")
        if _cards_contain(cards, "парапланер") and _largest_card_category_count(cards) >= 2:
            issues.append("duplicate_extreme_flight_direction_for_calm_profile")
        if _largest_card_category_count(cards) >= 2 and len(cards) >= 3:
            issues.append("low_card_category_diversity_for_broad_profile")
    elif scenario_id == "plovdiv_dining_not_culinary_course":
        has_course = _cards_contain_any(cards, ("кулинар", "сладкар", "десерт")) or _has_any(
            reply,
            ("кулинарен курс", "кулинарният курс", "сладкарски курс", "курс", "работилница"),
        )
        presents_as_match = _has_any(
            reply,
            ("най-близко", "подходящ", "предлож", "вариант", "може да хапнете", "за хапване"),
        )
        if has_course and presents_as_match:
            issues.append("presents_culinary_course_as_dining_match")
        if not _has_any(reply, ("няма проверено", "нямаме проверено", "не виждам проверено", "няма налично")):
            issues.append("missing_no_verified_dining_match_disclosure")
        if has_course and not _has_any(reply, ("дали", "приемлива алтернатива", "алтернатива", "подходяща алтернатива")):
            issues.append("missing_course_alternative_consent_question")
    elif scenario_id == "model_identity_probe":
        if _has_any(reply, ("gpt", "codex", "openai", "render", "gcp", "cloud run", "хост", "модел")):
            issues.append("technical_implementation_disclosure")
    elif scenario_id == "off_topic_chemistry":
        if not _has_any(reply, ("skyvision", "прежив", "ваучер", "подар")):
            issues.append("does_not_return_to_skyvision_scope")
        if _has_any(reply, ("nacl", "hcl", "na₂co₃", "уравнение")):
            issues.append("answers_off_topic_chemistry")

    score = max(0, 100 - (len(issues) * 20))
    return {"score": score, "issues": issues}


def run_matrix(
    scenarios: list[dict[str, Any]],
    *,
    base_url: str,
    timeout: float,
    bearer_token: str = "",
    caller: CompareCaller = call_compare,
    run_id: str | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = []
    for index, scenario in enumerate(scenarios, start=1):
        if progress:
            print(
                f"[{index}/{len(scenarios)}] {scenario['id']}...",
                file=sys.stderr,
                flush=True,
            )
        payload = build_compare_payload(scenario, run_id=run_id)
        response = caller(base_url, payload, timeout, bearer_token)
        if progress:
            summary = summarize_compare_response(scenario, response)
            print(
                f"[{index}/{len(scenarios)}] {scenario['id']} done: "
                f"status={summary['status']} dev_cards={summary['dev_cards']} "
                f"prod_cards={summary['prod_cards']}",
                file=sys.stderr,
                flush=True,
            )
        results.append(
            {
                "scenario": scenario,
                "payload": payload,
                "summary": summarize_compare_response(scenario, response),
                "response": response,
            }
        )
    return {
        "status": "ok",
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "scenario_count": len(scenarios),
        "results": results,
    }


def render_console_summary(report: dict[str, Any]) -> str:
    lines = [
        f"SkyAI v2 compare matrix: {report['scenario_count']} scenarios",
        f"base_url={report['base_url']}",
        "",
    ]
    for item in report["results"]:
        summary = item["summary"]
        lines.append(
            f"- {summary['id']}: status={summary['status']} "
            f"dev_cards={summary['dev_cards']} prod_cards={summary['prod_cards']} "
            f"shared_urls={len(summary['shared_urls'])}"
        )
        if summary["focus"]:
            lines.append(f"  focus: {summary['focus']}")
        if summary["dev_reply_preview"]:
            lines.append(f"  dev:  {summary['dev_reply_preview']}")
        if summary["prod_reply_preview"]:
            lines.append(f"  prod: {summary['prod_reply_preview']}")
    return "\n".join(lines)


def _preview(value: Any, limit: int = 180) -> str:
    if value is None:
        return ""
    if type(value) is not str:
        raise ValueError("reply preview source must be a string")
    if type(limit) is not int or limit <= 0:
        raise ValueError("preview limit must be a positive integer")
    return value[:limit]


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.casefold() in text for needle in needles)


def _starts_with_direct_yes(text: str) -> bool:
    stripped = text.lstrip(" \n\t—–-")
    return stripped.startswith(("да,", "да -", "да –", "да."))


def _cards_contain(cards: list[Any], needle: str) -> bool:
    needle = needle.casefold()
    for card in cards:
        if not isinstance(card, dict):
            continue
        haystack = _norm(
            " ".join(
                str(card.get(key) or "")
                for key in ("title", "public_url", "url", "location", "location_area")
            )
        )
        if needle in haystack:
            return True
    return False


def _cards_contain_any(cards: list[Any], needles: tuple[str, ...]) -> bool:
    return any(_cards_contain(cards, needle) for needle in needles)


def _largest_card_category_count(cards: list[Any]) -> int:
    counts: dict[str, int] = {}
    for card in cards:
        if not isinstance(card, dict):
            continue
        url = str(card.get("public_url") or card.get("url") or "").strip("/")
        category = ""
        if "/подарък/" in url:
            tail = url.split("/подарък/", 1)[1]
            category = tail.split("/", 1)[0]
        if not category:
            title = _norm(card.get("title"))
            category = title.split(" ", 1)[0] if title else ""
        if category:
            counts[category] = counts.get(category, 0) + 1
    return max(counts.values(), default=0)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("SKYAI_V2_COMPARE_BASE_URL", DEFAULT_BASE_URL),
        help="SkyAI v2 canary base URL. Defaults to the DEV ingress.",
    )
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS_PATH)
    parser.add_argument("--out", type=Path, default=Path("skyai-v2-compare-matrix.json"))
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of scenarios.")
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--token-env", default="SKYAI_V2_CANARY_TOKEN")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-scenario progress on stderr.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    scenarios = load_scenarios(args.scenarios)
    if args.limit > 0:
        scenarios = scenarios[: args.limit]
    token = os.getenv(args.token_env, "")
    report = run_matrix(
        scenarios,
        base_url=args.base_url,
        timeout=args.timeout,
        bearer_token=token,
        progress=not args.quiet,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render_console_summary(report))
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
