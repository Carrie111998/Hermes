"""Clients for the Korean government legal open APIs.

Three upstreams, one interface:

* **law.go.kr DRF** (``lawSearch.do`` / ``lawService.do``) — 법령, 판례,
  행정규칙, 자치법규, 법령해석례, 헌재결정례, 별표·서식. Authenticated with
  the ``OC`` id (the local part of the e-mail you registered with).
* **data.go.kr** — 헌법재판소 판례정보, 현행법령 목록 등. Authenticated with
  a service key.
* **easylaw.go.kr** — 생활법령 (SOAP).

Every response is cached in SQLite (these services have daily quotas and
answer the same question with the same bytes all day), every failure is
soft: a legal answer without a citation beats a 500 in the chat room.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .models import BODY_KEYS, ID_KEYS, TITLE_KEYS, LawDoc

log = logging.getLogger(__name__)

DRF_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
DRF_SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"
DATA_GO_KR_CC_BASE = "https://apis.data.go.kr/9750000/PrecedentInfomationService"
DATA_GO_KR_LAW_BASE = "https://apis.data.go.kr/1170000/law"
EASYLAW_SOAP_URL = "http://www.easylaw.go.kr/OPENAPI/soap/LifeLawSearchService"

# DRF target → the `kind` we tag results with.
TARGET_KINDS = {
    "law": "law",
    "eflaw": "law",
    "elaw": "law",
    "prec": "prec",
    "detc": "detc",
    "expc": "expc",
    "admrul": "admrul",
    "ordin": "ordin",
    "licbyl": "byeolpyo",
    "lsBylList": "byeolpyo",
}


class LawApiError(RuntimeError):
    pass


# ── payload → records ────────────────────────────────────────────────────
def xml_to_dict(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        return (element.text or "").strip()
    result: dict[str, Any] = {}
    for child in children:
        value = xml_to_dict(child)
        tag = child.tag
        if tag in result:
            existing = result[tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[tag] = [existing, value]
        else:
            result[tag] = value
    return result


def parse_payload(body: str) -> Any:
    """JSON if it parses, XML otherwise. Both services serve both."""
    text = (body or "").strip()
    if not text:
        return {}
    if text[0] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if text.startswith("<"):
        try:
            return xml_to_dict(ET.fromstring(text))  # noqa: S314 — government XML, no DTDs
        except ET.ParseError as exc:
            raise LawApiError(f"응답을 해석할 수 없습니다: {exc}") from exc
    raise LawApiError(f"예상치 못한 응답 형식: {text[:120]}")


_RECORD_KEYS = frozenset(TITLE_KEYS) | frozenset(ID_KEYS) | frozenset(BODY_KEYS)


def _looks_like_record(node: Mapping[str, Any]) -> bool:
    return any(key in node for key in _RECORD_KEYS)


def extract_records(payload: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Pull result rows out of whatever envelope the service used.

    The envelopes differ per target (``LawSearch.law``, ``PrecSearch.prec``,
    ``response.body.items.item``…) and have changed shape over the years,
    so we walk the tree and take the first nodes that look like records.
    """
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if _looks_like_record(node):
                found.append(node)
                return
            for value in node.values():
                walk(value)

    walk(payload)
    return found[:limit]


def find_error_message(payload: Any) -> str:
    """data.go.kr reports quota/key problems inside a 200 response."""
    if not isinstance(payload, dict):
        return ""
    for key in ("errMsg", "returnAuthMsg", "resultMsg", "errorMessage"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and value.strip().upper() not in {"NORMAL SERVICE.", "OK"}:
            return value.strip()
    for value in payload.values():
        if isinstance(value, dict):
            nested = find_error_message(value)
            if nested:
                return nested
    return ""


# ── the client ───────────────────────────────────────────────────────────
@dataclass
class CacheBackend:
    """Minimal protocol so tests can pass a dict-backed fake."""

    get: Any
    put: Any


class LawApiClient:
    def __init__(
        self,
        oc: str = "",
        service_key: str = "",
        *,
        timeout_s: float = 12.0,
        cache_ttl_s: int = 86400,
        cache: CacheBackend | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.oc = oc
        self.service_key = service_key
        self.timeout_s = timeout_s
        self.cache_ttl_s = cache_ttl_s
        self._cache = cache
        self._client = client
        self._owns_client = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_s,
                follow_redirects=True,
                headers={"User-Agent": "moa-legal-bot/1.0 (+kakao consultation assistant)"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        query = {k: v for k, v in params.items() if v not in (None, "")}
        cache_key = f"{url}?{urlencode(sorted(query.items()), doseq=True)}"
        if self._cache is not None:
            cached = self._cache.get(cache_key, self.cache_ttl_s)
            if cached is not None:
                return parse_payload(cached)

        client = await self._http()
        try:
            response = await client.get(url, params=query)
        except httpx.HTTPError as exc:
            raise LawApiError(f"법령 API 호출 실패: {exc}") from exc
        if response.status_code >= 400:
            raise LawApiError(f"법령 API {response.status_code}: {response.text[:160]}")

        payload = parse_payload(response.text)
        error = find_error_message(payload)
        if error:
            raise LawApiError(f"법령 API 오류: {error}")
        if self._cache is not None:
            self._cache.put(cache_key, response.text)
        return payload

    # ── law.go.kr DRF ────────────────────────────────────────────────────
    async def drf_search(
        self,
        target: str,
        query: str = "",
        *,
        display: int = 10,
        page: int = 1,
        **extra: Any,
    ) -> list[LawDoc]:
        if not self.oc:
            raise LawApiError("LAW_OC 가 설정되지 않았습니다 (law.go.kr 신청 아이디)")
        params = {
            "OC": self.oc,
            "target": target,
            "type": "JSON",
            "query": query,
            "display": display,
            "page": page,
            **extra,
        }
        payload = await self._get(DRF_SEARCH_URL, params)
        kind = TARGET_KINDS.get(target, target)
        return [LawDoc.from_record(kind, record) for record in extract_records(payload, display)]

    async def drf_service(self, target: str, **params: Any) -> LawDoc | None:
        if not self.oc:
            raise LawApiError("LAW_OC 가 설정되지 않았습니다 (law.go.kr 신청 아이디)")
        payload = await self._get(
            DRF_SERVICE_URL, {"OC": self.oc, "target": target, "type": "JSON", **params}
        )
        records = extract_records(payload, limit=1)
        if not records:
            return None
        return LawDoc.from_record(TARGET_KINDS.get(target, target), records[0])

    async def search_law(self, query: str, display: int = 5) -> list[LawDoc]:
        """현행법령 목록 검색."""
        return await self.drf_search("law", query, display=display)

    async def get_law(self, law_id: str = "", mst: str = "") -> LawDoc | None:
        """법령 본문 조회 — ID(법령ID) 또는 MST(법령마스터번호)."""
        if not law_id and not mst:
            raise LawApiError("law_id 또는 mst 중 하나는 필요합니다")
        return await self.drf_service("law", ID=law_id or None, MST=mst or None)

    async def search_precedent(
        self, query: str, *, court: str = "", case_no: str = "", display: int = 5
    ) -> list[LawDoc]:
        """판례 목록 검색. 본문은 판례일련번호로 다시 조회해야 한다."""
        extra: dict[str, Any] = {}
        if court:
            extra["curt"] = court
        if case_no:
            extra["nb"] = case_no
        return await self.drf_search("prec", query, display=display, **extra)

    async def get_precedent(self, prec_id: str) -> LawDoc | None:
        """판례 본문 조회 (판례일련번호)."""
        return await self.drf_service("prec", ID=prec_id)

    async def search_ordinance(self, query: str, display: int = 5) -> list[LawDoc]:
        """자치법규(조례·규칙) 목록 검색."""
        return await self.drf_search("ordin", query, display=display)

    async def search_admin_rule(self, query: str, display: int = 5) -> list[LawDoc]:
        """행정규칙(훈령·예규·고시) 목록 검색."""
        return await self.drf_search("admrul", query, display=display)

    async def search_interpretation(self, query: str, display: int = 5) -> list[LawDoc]:
        """법령해석례 검색."""
        return await self.drf_search("expc", query, display=display)

    async def search_forms(self, query: str, display: int = 5) -> list[LawDoc]:
        """법령 별표·서식 검색 (target=licbyl)."""
        return await self.drf_search("licbyl", query, display=display)

    async def search_constitutional_decision(self, query: str, display: int = 5) -> list[LawDoc]:
        """헌재결정례 (law.go.kr 쪽 target=detc)."""
        return await self.drf_search("detc", query, display=display)

    # ── data.go.kr ───────────────────────────────────────────────────────
    async def _data_go_kr(self, url: str, params: dict[str, Any]) -> Any:
        if not self.service_key:
            raise LawApiError("DATA_GO_KR_KEY 가 설정되지 않았습니다")
        # The portal hands out a URL-encoded key. httpx encodes params
        # again, which turns %2F into %252F and fails auth — so decode
        # once here if the key still looks percent-encoded.
        key = self.service_key
        if "%" in key and "/" not in key:
            from urllib.parse import unquote

            key = unquote(key)
        return await self._get(url, {"serviceKey": key, "type": "json", **params})

    async def cc_precedents(
        self, operation: str = "getKorPrcdntList", *, page: int = 1, rows: int = 10, **params: Any
    ) -> list[LawDoc]:
        """헌법재판소 판례정보 조회 서비스.

        operation 은 포털에 공개된 오퍼레이션 이름 그대로 넣는다
        (getOcprPrcdntList / getRealmMainPrcdntList / getKorPrcdntList /
        getEngPrcdntList / getOcprOutlineList 및 각 Detail).
        """
        payload = await self._data_go_kr(
            f"{DATA_GO_KR_CC_BASE}/{operation}", {"pageNo": page, "numOfRows": rows, **params}
        )
        return [LawDoc.from_record("cc_prec", record) for record in extract_records(payload, rows)]

    async def data_go_kr_law(self, operation: str, **params: Any) -> list[LawDoc]:
        """법제처 data.go.kr 계열 목록 API 패스스루.

        오퍼레이션 이름은 각 API 의 활용가이드 문서에 있는 값을 그대로 넣는다.
        엔드포인트마다 이름이 달라 하드코딩하지 않는다.
        """
        payload = await self._data_go_kr(f"{DATA_GO_KR_LAW_BASE}/{operation}", params)
        return [LawDoc.from_record("law", record) for record in extract_records(payload)]

    # ── easylaw.go.kr (생활법령, SOAP) ────────────────────────────────────
    async def life_law_search(self, query: str, rows: int = 5) -> list[LawDoc]:
        """생활법령 검색 (SOAP). 실패해도 예외 대신 빈 목록을 돌려준다."""
        if not self.service_key:
            raise LawApiError("DATA_GO_KR_KEY 가 설정되지 않았습니다")
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:ser="http://service.rest.lifelaw.moleg.go.kr">'
            "<soapenv:Header/><soapenv:Body>"
            "<ser:lifeLawSearch>"
            f"<ser:serviceKey>{self.service_key}</ser:serviceKey>"
            f"<ser:query>{query}</ser:query>"
            f"<ser:numOfRows>{rows}</ser:numOfRows>"
            "<ser:pageNo>1</ser:pageNo>"
            "</ser:lifeLawSearch>"
            "</soapenv:Body></soapenv:Envelope>"
        )
        client = await self._http()
        try:
            response = await client.post(
                EASYLAW_SOAP_URL,
                content=envelope.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LawApiError(f"생활법령 API 호출 실패: {exc}") from exc
        payload = parse_payload(response.text)
        return [LawDoc.from_record("life", record) for record in extract_records(payload, rows)]
