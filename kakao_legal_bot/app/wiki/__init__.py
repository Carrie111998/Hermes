"""LLM WIKI — RAW / WIKI / 그래프의 세 층.

    raw/    원문 .md. 손대지 않습니다. 무엇이 근거였는지의 최종 기준.
    wiki/   원문을 요약·구조화한 노트. frontmatter에 조문·판례·키워드·날짜.
    graph   노트와 엔티티(조문·판례·키워드)의 연결. 허브노트와 이웃 검색.

검색은 세 갈래를 겹쳐 씁니다 — FTS5 키워드, 임베딩, 그리고 **그래프**.
앞의 둘은 "비슷한 문장"을 찾고, 그래프는 "같은 조문·같은 판례를 말하는 문서"를
찾습니다. 법률 자료에서는 뒤쪽이 결정적일 때가 많습니다.
"""

from __future__ import annotations

from .citation import (
    CaseRef,
    Citations,
    StatuteRef,
    extract_citations,
    normalise_law_name,
    parse_cases,
    parse_date,
    parse_statutes,
)

__all__ = [
    "CaseRef",
    "Citations",
    "StatuteRef",
    "extract_citations",
    "normalise_law_name",
    "parse_cases",
    "parse_date",
    "parse_statutes",
]
