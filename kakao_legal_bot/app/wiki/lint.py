"""LINT — 낡은 자료가 현행 규정 행세를 하지 못하게 막는다.

법률 데이터베이스에서 가장 위험한 오류는 빠진 자료가 아니라 **낡은 자료**
입니다. 없으면 못 찾을 뿐이지만, 개정 전 조문을 그대로 인용하면 자신 있게
틀린 답이 나갑니다. 2018년에 쓴 교재가 2023년 개정을 알 리 없으니까요.

그래서 날짜로 잡을 수 있는 것은 여기서 결정적으로 잡습니다.

* 같은 조문을 다른 시행일로 말하는 법령 노트 → 오래된 쪽을 **연혁**으로 내림
* 조문 시행일보다 **먼저 쓰인** 책·주석서가 그 조문을 설명 → 재확인 표시
* 필수 날짜 누락, 날짜 형식 오류, 같은 판례가 두 노트에 중복

문장끼리 정말 모순인지는 읽어야 압니다. 그건 여기서 **검토 목록**으로만
뽑아 코덱스에게 넘깁니다 — 규칙으로 판정하는 척하지 않습니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .citation import parse_statute
from .note import CASE, STATUTE, WikiNote

ERROR = "error"
WARN = "warn"
INFO = "info"

MISSING_FIELD = "missing_field"
STALE_STATUTE = "stale_statute"
OUTDATED_SOURCE = "outdated_source"
DUPLICATE_CASE = "duplicate_case"
DANGLING_LINK = "dangling_link"
HISTORIC_CITATION = "historic_citation"
REVIEW_PAIR = "review_pair"

_LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}
_CODE_LABELS = {
    MISSING_FIELD: "필수 항목 누락",
    STALE_STATUTE: "연혁 조문 (개정 전)",
    OUTDATED_SOURCE: "개정 이후 확인 필요",
    DUPLICATE_CASE: "같은 판례가 두 노트에",
    DANGLING_LINK: "끊어진 링크",
    HISTORIC_CITATION: "구법 인용",
    REVIEW_PAIR: "내용 대조 필요",
}


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    path: str
    message: str
    hint: str = ""
    related: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return _CODE_LABELS.get(self.code, self.code)


@dataclass
class LintReport:
    findings: list[Finding] = field(default_factory=list)
    superseded: dict[str, str] = field(default_factory=dict)  # 노트 경로 → 갈음하는 노트

    @property
    def errors(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.level == ERROR]

    def by_code(self, code: str) -> list[Finding]:
        return [finding for finding in self.findings if finding.code == code]

    def sorted(self) -> list[Finding]:
        return sorted(
            self.findings, key=lambda f: (_LEVEL_ORDER.get(f.level, 9), f.code, f.path)
        )

    def to_markdown(self) -> str:
        if not self.findings:
            return "# LINT\n\n문제 없습니다.\n"
        lines = ["# LINT", ""]
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.level] = counts.get(finding.level, 0) + 1
        lines.append(
            "오류 {e}건 · 경고 {w}건 · 참고 {i}건".format(
                e=counts.get(ERROR, 0), w=counts.get(WARN, 0), i=counts.get(INFO, 0)
            )
        )
        lines.append("")
        current = ""
        for finding in self.sorted():
            if finding.code != current:
                current = finding.code
                lines.append(f"## {finding.label}")
                lines.append("")
            lines.append(f"- `{finding.path}` — {finding.message}")
            if finding.hint:
                lines.append(f"  - {finding.hint}")
        lines.append("")
        return "\n".join(lines)

    def to_worklist(self) -> list[dict[str, object]]:
        """코덱스에게 넘길 검토 목록 — 사람이 읽어야 판정되는 것들만."""
        return [
            {
                "path": finding.path,
                "code": finding.code,
                "message": finding.message,
                "related": list(finding.related),
            }
            for finding in self.findings
            if finding.code in {REVIEW_PAIR, OUTDATED_SOURCE}
        ]


def _statute_keys(note: WikiNote) -> list[str]:
    keys: list[str] = []
    for written in note.statutes:
        ref = parse_statute(written)
        key = ref.key if ref else written.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def lint(notes: list[WikiNote], known_titles: set[str] | None = None) -> LintReport:
    """노트 전체를 훑어 날짜로 판정 가능한 문제를 모은다."""
    report = LintReport()
    notes = list(notes)

    # ── ① 필수 항목 ──────────────────────────────────────────────────────
    for note in notes:
        for missing in note.missing_required():
            report.findings.append(
                Finding(
                    level=ERROR,
                    code=MISSING_FIELD,
                    path=note.path or note.title,
                    message=f"{missing} 이(가) 없습니다.",
                    hint="날짜가 없으면 개정 전후를 가릴 수 없어 검색에서 뒤로 밀립니다.",
                )
            )

    # ── ② 같은 조문, 다른 시행일 → 오래된 쪽은 연혁 ──────────────────────
    by_statute: dict[str, list[WikiNote]] = {}
    for note in notes:
        if note.kind != STATUTE:
            continue
        for key in _statute_keys(note):
            by_statute.setdefault(key, []).append(note)

    newest_effective: dict[str, tuple[str, str]] = {}  # 조문 → (시행일, 노트 경로)
    for key, group in by_statute.items():
        dated = [note for note in group if note.effective_on]
        if not dated:
            continue
        winner = max(dated, key=lambda note: note.effective_on)
        newest_effective[key] = (winner.effective_on, winner.path or winner.title)
        for note in dated:
            if note is winner or note.effective_on == winner.effective_on:
                continue
            path = note.path or note.title
            report.superseded[path] = winner.path or winner.title
            report.findings.append(
                Finding(
                    level=WARN,
                    code=STALE_STATUTE,
                    path=path,
                    message=(
                        f"{key} 를 시행일 {note.effective_on} 기준으로 설명합니다. "
                        f"현행은 {winner.effective_on} 입니다."
                    ),
                    hint="연혁조문으로만 의미가 있습니다. 검색에서는 뒤로 내립니다.",
                    related=(winner.path or winner.title,),
                )
            )

    # ── ③ 개정보다 먼저 쓰인 책이 그 조문을 설명 ─────────────────────────
    for note in notes:
        if note.kind == STATUTE or not note.written_on:
            continue
        for key in _statute_keys(note):
            effective = newest_effective.get(key)
            if effective and note.written_on < effective[0]:
                report.findings.append(
                    Finding(
                        level=WARN,
                        code=OUTDATED_SOURCE,
                        path=note.path or note.title,
                        message=(
                            f"{key} 는 {effective[0]} 에 시행되었는데 이 자료는 "
                            f"{note.written_on} 자료입니다."
                        ),
                        hint="개정 내용이 반영되지 않았을 수 있습니다. 대조해 주세요.",
                        related=(effective[1], key),
                    )
                )

    # ── ④ 같은 판례가 두 노트에 ──────────────────────────────────────────
    by_case: dict[str, list[WikiNote]] = {}
    for note in notes:
        if note.kind == CASE and note.case_no:
            by_case.setdefault(note.case_no, []).append(note)
    for case_no, group in by_case.items():
        if len(group) < 2:
            continue
        paths = [note.path or note.title for note in group]
        for path in paths[1:]:
            report.findings.append(
                Finding(
                    level=WARN,
                    code=DUPLICATE_CASE,
                    path=path,
                    message=f"{case_no} 노트가 {len(group)}개 있습니다.",
                    hint="하나로 합치세요. 둘 다 남으면 검색 결과가 반씩 갈립니다.",
                    related=tuple(paths),
                )
            )

    # ── ⑤ 끊어진 링크 ────────────────────────────────────────────────────
    # 여러 문서가 함께 쓰는 낱말은 허브노트가 받아 주므로 끊어진 것이 아닙니다.
    # 딱 한 문서에서만, 그것도 같은 이름의 노트 없이 나오는 낱말이 문제입니다.
    if known_titles is not None:
        seen_in: dict[str, int] = {}
        for note in notes:
            for keyword in note.keywords:
                seen_in[keyword] = seen_in.get(keyword, 0) + 1
        for note in notes:
            for keyword in note.keywords:
                if keyword in known_titles or seen_in.get(keyword, 0) > 1:
                    continue
                if any(char.isdigit() for char in keyword):
                    continue  # 조문·판례
                report.findings.append(
                    Finding(
                        level=INFO,
                        code=DANGLING_LINK,
                        path=note.path or note.title,
                        message=f"[[{keyword}]] 이(가) 이 문서에만 나오고 해당 노트도 없습니다.",
                        hint="오타이거나, 아직 안 쓴 문서입니다.",
                    )
                )

    # ── ⑥ 사람이 읽어야 하는 것 — 코덱스 검토 목록 ───────────────────────
    report.findings.extend(_review_pairs(notes))
    return report


def _review_pairs(notes: list[WikiNote], gap_years: int = 5) -> list[Finding]:
    """같은 조문을 다루는데 자료 연도가 많이 벌어진 짝.

    규칙으로 모순이라고 단정할 수는 없습니다. 다만 "이 둘은 서로 다른 말을
    하고 있을 가능성이 높으니 읽어 보라"고 짚어 줄 수는 있습니다.
    """
    buckets: dict[str, list[WikiNote]] = {}
    for note in notes:
        if note.kind == STATUTE:
            continue
        for key in _statute_keys(note):
            buckets.setdefault(key, []).append(note)

    findings: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for key, group in buckets.items():
        dated = [note for note in group if note.as_of]
        if len(dated) < 2:
            continue
        dated.sort(key=lambda note: note.as_of)
        oldest, newest = dated[0], dated[-1]
        if int(newest.as_of[:4]) - int(oldest.as_of[:4]) < gap_years:
            continue
        marker = (key, oldest.path, newest.path)
        if marker in seen:
            continue
        seen.add(marker)
        findings.append(
            Finding(
                level=INFO,
                code=REVIEW_PAIR,
                path=oldest.path or oldest.title,
                message=(
                    f"{key} 를 {oldest.as_of} 자료와 {newest.as_of} 자료가 함께 다룹니다."
                ),
                hint="서술이 달라졌는지 대조하고, 다르면 최신 자료를 기준으로 정리하세요.",
                related=(newest.path or newest.title, key),
            )
        )
    return findings


def apply_supersession(notes: list[WikiNote], report: LintReport) -> list[WikiNote]:
    """연혁으로 밀린 노트에 ``superseded_by`` 를 적어 둔다.

    지우지 않습니다. 연혁조문은 '그때는 어땠는가'를 묻는 사건에서 필요하고,
    검색에서 뒤로 밀리기만 하면 충분합니다.
    """
    changed: list[WikiNote] = []
    for note in notes:
        target = report.superseded.get(note.path or note.title, "")
        if target and note.superseded_by != target:
            note.superseded_by = target
            changed.append(note)
    return changed
