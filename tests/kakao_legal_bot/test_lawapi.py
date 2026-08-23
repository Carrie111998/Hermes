"""law.go.kr / data.go.kr clients.

No network: httpx.MockTransport serves fixtures shaped like the real
responses (JSON for DRF, XML for the data.go.kr services).
"""

from __future__ import annotations

import json

import httpx
import pytest

from kakao_legal_bot.app.lawapi.client import (
    CacheBackend,
    LawApiClient,
    LawApiError,
    extract_records,
    find_error_message,
    parse_payload,
)
from kakao_legal_bot.app.lawapi.models import LawDoc

PREC_SEARCH_JSON = {
    "PrecSearch": {
        "totalCnt": "2",
        "prec": [
            {
                "판례일련번호": "228541",
                "사건명": "임대차보증금반환",
                "사건번호": "2018다255648",
                "선고일자": "20190314",
                "법원명": "대법원",
                "판례상세링크": "/DRF/lawService.do?OC=test&target=prec&ID=228541",
            },
            {
                "판례일련번호": "228542",
                "사건명": "손해배상(기)",
                "사건번호": "2020다12345",
                "선고일자": "20210701",
                "법원명": "서울고등법원",
            },
        ],
    }
}

LAW_SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<LawSearch>
  <totalCnt>1</totalCnt>
  <law>
    <법령일련번호>001234</법령일련번호>
    <법령명한글>주택임대차보호법</법령명한글>
    <공포일자>20230704</공포일자>
    <시행일자>20231019</시행일자>
    <소관부처명>법무부</소관부처명>
    <법령상세링크>/DRF/lawService.do?target=law&amp;ID=001234</법령상세링크>
  </law>
</LawSearch>"""

DATA_GO_KR_ERROR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse><cmmMsgHeader>
<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>
</cmmMsgHeader></OpenAPI_ServiceResponse>"""


def make_client(handler, **kwargs) -> LawApiClient:
    transport = httpx.MockTransport(handler)
    return LawApiClient(
        oc="test-oc",
        service_key="test-key",
        client=httpx.AsyncClient(transport=transport),
        **kwargs,
    )


# ── parsing ──────────────────────────────────────────────────────────────
def test_parse_payload_reads_json_and_xml():
    assert parse_payload('{"a": 1}') == {"a": 1}
    assert parse_payload("<root><a>1</a></root>") == {"a": "1"}
    assert parse_payload("") == {}


def test_parse_payload_rejects_garbage():
    with pytest.raises(LawApiError):
        parse_payload("not xml, not json")


def test_extract_records_digs_through_the_envelope():
    records = extract_records(PREC_SEARCH_JSON)
    assert len(records) == 2
    assert records[0]["사건번호"] == "2018다255648"


def test_extract_records_handles_a_single_xml_record():
    records = extract_records(parse_payload(LAW_SEARCH_XML))
    assert len(records) == 1
    assert records[0]["법령명한글"] == "주택임대차보호법"


def test_find_error_message_spots_a_bad_service_key():
    payload = parse_payload(DATA_GO_KR_ERROR_XML)
    assert "SERVICE_KEY" in find_error_message(payload)


def test_normal_service_is_not_an_error():
    assert find_error_message({"resultMsg": "NORMAL SERVICE."}) == ""


# ── model ────────────────────────────────────────────────────────────────
def test_precedent_citation_reads_like_a_lawyer_wrote_it():
    doc = LawDoc.from_record("prec", PREC_SEARCH_JSON["PrecSearch"]["prec"][0])
    assert doc.doc_id == "228541"
    assert doc.citation == "대법원 2019. 3. 14. 2018다255648 판결 (임대차보증금반환)"


def test_law_citation_includes_the_effective_date():
    doc = LawDoc.from_record("law", extract_records(parse_payload(LAW_SEARCH_XML))[0])
    assert doc.title == "주택임대차보호법"
    assert "[시행 2023. 10. 19.]" in doc.citation


# ── requests ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_precedent_search_sends_the_documented_parameters():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, text=json.dumps(PREC_SEARCH_JSON, ensure_ascii=False))

    client = make_client(handler)
    docs = await client.search_precedent("담보권", court="대법원", display=2)
    await client.aclose()

    assert seen["target"] == "prec"
    assert seen["OC"] == "test-oc"
    assert seen["query"] == "담보권"
    assert seen["curt"] == "대법원"
    assert len(docs) == 2
    assert docs[0].kind == "prec"


@pytest.mark.asyncio
async def test_law_search_accepts_an_xml_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LAW_SEARCH_XML)

    client = make_client(handler)
    docs = await client.search_law("주택임대차")
    await client.aclose()
    assert docs[0].title == "주택임대차보호법"


@pytest.mark.asyncio
async def test_responses_are_cached_so_the_daily_quota_survives():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=json.dumps(PREC_SEARCH_JSON, ensure_ascii=False))

    store: dict[str, str] = {}
    cache = CacheBackend(
        get=lambda key, ttl: store.get(key), put=lambda key, body: store.__setitem__(key, body)
    )
    client = make_client(handler, cache=cache)
    await client.search_precedent("담보권")
    await client.search_precedent("담보권")
    await client.aclose()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_http_error_becomes_a_law_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = make_client(handler)
    with pytest.raises(LawApiError):
        await client.search_law("민법")
    await client.aclose()


@pytest.mark.asyncio
async def test_service_key_error_inside_a_200_is_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=DATA_GO_KR_ERROR_XML)

    client = make_client(handler)
    with pytest.raises(LawApiError, match="SERVICE_KEY"):
        await client.cc_precedents()
    await client.aclose()


@pytest.mark.asyncio
async def test_percent_encoded_service_keys_are_decoded_once():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, text="{}")

    transport = httpx.MockTransport(handler)
    client = LawApiClient(
        oc="oc",
        # This is the shape the portal shows you — encoded.
        service_key="abc%2Fdef%3D%3D",
        client=httpx.AsyncClient(transport=transport),
    )
    await client.cc_precedents()
    await client.aclose()
    assert seen["serviceKey"] == "abc/def=="


@pytest.mark.asyncio
async def test_missing_oc_is_reported_clearly():
    client = LawApiClient(oc="", service_key="")
    with pytest.raises(LawApiError, match="LAW_OC"):
        await client.search_law("민법")
    await client.aclose()
