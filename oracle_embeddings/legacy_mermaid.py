"""Mermaid sequence diagram 생성 — Phase A (linear, LLM 불필요).

``legacy_analyzer.trace_chain_events`` 가 반환한 event 리스트를 Mermaid
``sequenceDiagram`` 텍스트로 렌더. 참가자 (participant) 자동 alias 로
긴 FQCN 을 짧게 만들고, 동일한 namespace/RFC 는 공유.

참가자 종류:
- ``User``          — 엔드포인트 트리거 (actor)
- Controller class  — endpoint 의 root_class
- 각 service impl   — cross-class call 대상
- ``Mapper``        — MyBatis XML 전체 (namespace 별 note 로 구분)
- ``SAP``           — RFC system
- ``DB``            — Oracle. SQL 호출 직후 `Mapper->>DB: INSERT TB_X` 로
                      테이블 가시화

Phase B (alt/loop block) 은 이 모듈 확장 지점으로 남김. 현재는 모든
event 가 linear 로 emit.
"""
from __future__ import annotations

from typing import Dict, List


_MERMAID_RESERVED = {"Note", "Participant", "Actor", "End", "Loop", "Alt", "Else"}


def _short_alias(fqcn: str) -> str:
    """``com.x.OrderServiceImpl`` → ``OrderServiceImpl``."""
    if not fqcn:
        return "Unknown"
    return fqcn.rsplit(".", 1)[-1]


def _escape_label(text: str) -> str:
    """Mermaid label 에 들어가도 안전하도록 기호 치환."""
    if not text:
        return ""
    # Mermaid 는 ``:`` 는 구분자, ``"`` 는 label 경계. 제거.
    return (text.replace(":", " ").replace('"', "'")
                .replace("\n", " ").replace("\r", " ").strip())


def _participant_id(fqcn_or_role: str, used_aliases: Dict[str, str]) -> str:
    """Mermaid participant alias 를 반환 (필요 시 신규 등록).

    충돌 회피: 같은 simple name 을 가진 서로 다른 FQCN 이 있으면 두 번째
    부터 ``_2``, ``_3`` 접미사.
    """
    if fqcn_or_role in used_aliases:
        return used_aliases[fqcn_or_role]
    alias = _short_alias(fqcn_or_role) or "X"
    # Mermaid identifier 규칙: alphanumeric + underscore
    alias = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in alias)
    # Reserved keyword 회피
    if alias in _MERMAID_RESERVED:
        alias = alias + "_"
    # 충돌 회피
    base = alias
    i = 2
    existing_aliases = set(used_aliases.values())
    while alias in existing_aliases:
        alias = f"{base}_{i}"
        i += 1
    used_aliases[fqcn_or_role] = alias
    return alias


def render_sequence_diagram(events: List[dict], endpoint: dict,
                             controller_fqcn: str) -> str:
    """Event 리스트 → Mermaid ``sequenceDiagram`` 텍스트.

    Empty event list 면 endpoint 호출만 표시한 minimal diagram 반환.
    """
    lines = ["sequenceDiagram"]
    used: Dict[str, str] = {}

    # 고정 participant 3개
    user_id = _participant_id("User", used)
    ctrl_id = _participant_id(controller_fqcn or "Controller", used)
    # Mapper / SAP / DB 는 이벤트에 실제 나올 때만 선언
    mapper_declared = False
    sap_declared = False
    db_declared = False

    lines.append(f"    actor {user_id}")
    lines.append(f"    participant {ctrl_id} as {_escape_label(_short_alias(controller_fqcn))}")

    # 선언을 먼저 모아서 미리 declare — Mermaid 는 사용 전 선언 안 해도 되지만
    # 미리 선언하면 렌더 시 순서가 일정해짐.
    to_declare_services: List[str] = []  # FQCN 순서대로
    for ev in events:
        if ev["kind"] == "call":
            to_cls = ev["to_class"]
            if to_cls and to_cls != controller_fqcn and to_cls not in used:
                # dedup 은 used dict 가 보장 (id 발급 시점에 등록)
                if to_cls not in to_declare_services:
                    to_declare_services.append(to_cls)
    for fqcn in to_declare_services:
        pid = _participant_id(fqcn, used)
        lines.append(f"    participant {pid} as {_escape_label(_short_alias(fqcn))}")

    # Root: User → Controller : HTTP method + URL
    http = endpoint.get("http_method") or "GET"
    url = endpoint.get("url") or "/"
    method_name = endpoint.get("method_name") or ""
    lines.append(f"    {user_id}->>{ctrl_id}: {_escape_label(http)} {_escape_label(url)}")
    if method_name:
        lines.append(f"    Note over {ctrl_id}: {_escape_label(method_name)}()")

    # Events 순회
    for ev in events:
        if ev["kind"] == "call":
            src = _participant_id(ev["from_class"], used)
            tgt = _participant_id(ev["to_class"], used)
            label = _escape_label(ev.get("method", "") + "()")
            if ev.get("self_call"):
                # self-call: 자기 자신에게 화살표
                lines.append(f"    {src}->>{src}: {label}")
            else:
                lines.append(f"    {src}->>{tgt}: {label}")
        elif ev["kind"] == "sql":
            src = _participant_id(ev["from_class"], used)
            if not mapper_declared:
                mapper_alias = _participant_id("Mapper", used)
                lines.insert(2, f"    participant {mapper_alias} as Mapper")
                mapper_declared = True
            else:
                mapper_alias = used["Mapper"]
            ns = ev.get("namespace", "")
            sid = ev.get("sql_id", "")
            op = ev.get("op", "").upper()
            sql_label = f"{op} {ns}.{sid}" if op else f"{ns}.{sid}"
            lines.append(f"    {src}->>{mapper_alias}: {_escape_label(sql_label)}")
            # Mapper → DB : 테이블 표시
            tables = ev.get("tables") or []
            if tables:
                if not db_declared:
                    db_alias = _participant_id("DB", used)
                    lines.insert(2, f"    participant {db_alias} as DB")
                    db_declared = True
                else:
                    db_alias = used["DB"]
                tbl_label = ", ".join(tables[:4])
                if len(tables) > 4:
                    tbl_label += f" …(+{len(tables) - 4})"
                lines.append(f"    {mapper_alias}->>{db_alias}: {_escape_label(tbl_label)}")
        elif ev["kind"] == "rfc":
            src = _participant_id(ev["from_class"], used)
            if not sap_declared:
                sap_alias = _participant_id("SAP", used)
                lines.insert(2, f"    participant {sap_alias} as SAP")
                sap_declared = True
            else:
                sap_alias = used["SAP"]
            rfc_name = ev.get("rfc_name", "")
            lines.append(f"    {src}->>{sap_alias}: {_escape_label(rfc_name)}")

    return "\n".join(lines)
