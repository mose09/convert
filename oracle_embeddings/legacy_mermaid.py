"""Mermaid sequence diagram 생성 — Phase A + Phase B.

``legacy_analyzer.trace_chain_events`` 가 반환한 event 리스트를 Mermaid
``sequenceDiagram`` 텍스트로 렌더. 각 event 의 ``context_stack`` (Phase B)
을 보고 ``alt / else / loop / opt / end`` 래핑을 자동으로 emit.

참가자 종류:
- ``User``          — 엔드포인트 트리거 (actor)
- Controller class  — endpoint 의 root_class
- 각 service impl   — cross-class call 대상
- ``Mapper``        — MyBatis XML 전체 (namespace 별 note 로 구분)
- ``SAP``           — RFC system
- ``DB``            — Oracle. SQL 호출 직후 `Mapper->>DB: INSERT INTO TB_X`

Phase B 매핑 규칙 (control block → Mermaid block):
- ``if``         → ``alt <cond>``
- ``else_if``    → ``else <cond>``  (sibling, 체인 유지)
- ``else``       → ``else``         (sibling, 체인 유지)
- ``for``        → ``loop <cond>``
- ``while``      → ``loop <cond>``
- ``do_while``   → ``loop do-while <cond>``
- ``switch``     → ``alt switch(<cond>)``
- ``try``        → ``opt try``
- ``catch``      → ``else catch <ex>`` (try 와 sibling)
- ``finally``    → ``else finally``

Phase C 는 LLM 로 조건 자연어화 (예: ``x > 0`` → ``고객 존재 시``) —
이 모듈은 그대로 두고 사전 변환해서 ``condition`` 필드를 덮어쓰면 됨.
"""
from __future__ import annotations

from typing import Dict, List


_MERMAID_RESERVED = {"Note", "Participant", "Actor", "End", "Loop", "Alt", "Else",
                     "Opt", "Rect"}


def _short_alias(fqcn: str) -> str:
    """``com.x.OrderServiceImpl`` → ``OrderServiceImpl``."""
    if not fqcn:
        return "Unknown"
    return fqcn.rsplit(".", 1)[-1]


def _escape_label(text: str) -> str:
    """Mermaid label 에 들어가도 안전하도록 기호 치환.

    Mermaid sequenceDiagram 에서 ``<`` / ``>`` 는 화살표 문법 (``->>`` /
    ``<<-``) 의 일부로 해석돼 파싱 오류 유발. Java for-loop 조건
    (``int i = 0; i < list.size(); i++``) 이나 제네릭 (``List<String>``)
    같은 게 블록 라벨에 들어가면 렌더가 깨짐. HTML 엔티티로 escape.

    ``:`` 는 Mermaid 의 메시지 구분자, ``"`` 는 label 경계라서 제거/치환.
    """
    if not text:
        return ""
    # 먼저 HTML 엔티티로 escape — order matters (< 먼저 치환되면 &lt; 속
    # ``<`` 도 잡혀서 이중 escape 되지 않도록 단일 pass).
    out = text.replace("<", "&lt;").replace(">", "&gt;")
    out = out.replace(":", " ").replace('"', "'")
    out = out.replace("\n", " ").replace("\r", " ")
    return out.strip()


def _participant_id(fqcn_or_role: str, used_aliases: Dict[str, str]) -> str:
    """Mermaid participant alias 를 반환 (필요 시 신규 등록)."""
    if fqcn_or_role in used_aliases:
        return used_aliases[fqcn_or_role]
    alias = _short_alias(fqcn_or_role) or "X"
    alias = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in alias)
    if alias in _MERMAID_RESERVED:
        alias = alias + "_"
    base = alias
    i = 2
    existing_aliases = set(used_aliases.values())
    while alias in existing_aliases:
        alias = f"{base}_{i}"
        i += 1
    used_aliases[fqcn_or_role] = alias
    return alias


# ── Phase B — control-block rendering helpers ────────────────────────


def _block_open_text(block: dict) -> str:
    """block dict → Mermaid 열기 라인 (없으면 빈 문자열).

    Mermaid 자체 키워드 (alt / loop / opt) 뒤에 ``IF`` / ``FOR`` / ``TRY``
    같은 익숙한 접두어를 붙여서 다이어그램 프레임에 ``alt IF cond`` 처럼
    표시되게 함. 순수 Mermaid 는 if/for 를 지원 안 해 alt/loop 으로
    통일돼 있지만, 사용자 가독성 위해 접두어로 Java 원래 의도를 명시.
    """
    kind = block.get("kind")
    cond = _escape_label(block.get("condition", ""))
    if kind == "if":
        return f"alt IF {cond}" if cond else "alt IF"
    if kind == "for":
        return f"loop FOR {cond}" if cond else "loop FOR"
    if kind == "while":
        return f"loop WHILE {cond}" if cond else "loop WHILE"
    if kind == "do_while":
        return f"loop DO-WHILE {cond}" if cond else "loop DO-WHILE"
    if kind == "switch":
        return f"alt SWITCH {cond}" if cond else "alt SWITCH"
    if kind == "try":
        return "opt TRY"
    # else_if / else / catch / finally 는 sibling — 여기선 opener 아님
    return ""


def _block_sibling_text(block: dict) -> str:
    """sibling 전환 시 ``else <label>`` 라인.

    Mermaid 의 sibling 전환 키워드는 ``else`` 고정 (alt/opt 블록 모두 공용).
    가독성 위해 뒤에 ``ELSE IF`` / ``ELSE`` / ``CATCH`` / ``FINALLY``
    레이블을 붙여서 원래 Java 구조를 명시.
    """
    kind = block.get("kind")
    cond = _escape_label(block.get("condition", ""))
    if kind == "else_if":
        return f"else ELSE IF {cond}" if cond else "else ELSE IF"
    if kind == "else":
        return "else ELSE"
    if kind == "catch":
        return f"else CATCH {cond}" if cond else "else CATCH"
    if kind == "finally":
        return "else FINALLY"
    return "else"


def _same_block(a: dict, b: dict) -> bool:
    return bool(a.get("block_id")) and a.get("block_id") == b.get("block_id")


def _is_sibling(prev_block: dict, curr_block: dict) -> bool:
    """같은 method 의 같은 chain 이지만 chain_index 가 다르면 sibling."""
    if not prev_block or not curr_block:
        return False
    if prev_block.get("method_key") != curr_block.get("method_key"):
        return False
    if prev_block.get("chain_id") != curr_block.get("chain_id"):
        return False
    return prev_block.get("chain_index") != curr_block.get("chain_index")


def _emit_transition(prev_ctx: List[dict], curr_ctx: List[dict],
                      lines: List[str]) -> None:
    """prev_ctx 에서 curr_ctx 로의 context 전환을 Mermaid wrapper 로 emit.

    단계:
      1. 공통 prefix 찾기 (같은 block_id 인 위치)
      2. divergence 지점이 sibling 전환이면 close + ``else <label>`` + open.
         그렇지 않으면 close + open.
      3. 공통 prefix 보다 깊은 prev 블록들은 역순으로 ``end`` 로 닫음.
      4. 새 curr 블록들은 순방향으로 opener emit.
    """
    i = 0
    while (i < len(prev_ctx) and i < len(curr_ctx)
           and _same_block(prev_ctx[i], curr_ctx[i])):
        i += 1
    # i 는 처음 다른 depth

    sibling_at = None
    if (i < len(prev_ctx) and i < len(curr_ctx)
            and _is_sibling(prev_ctx[i], curr_ctx[i])):
        sibling_at = i

    close_down_to = (sibling_at + 1) if sibling_at is not None else i
    # prev 의 깊은 블록부터 역순 close
    for j in range(len(prev_ctx) - 1, close_down_to - 1, -1):
        lines.append("    end")

    if sibling_at is not None:
        lines.append(f"    {_block_sibling_text(curr_ctx[sibling_at])}")
        open_from = sibling_at + 1
    else:
        open_from = i

    for j in range(open_from, len(curr_ctx)):
        opener = _block_open_text(curr_ctx[j])
        if opener:
            lines.append(f"    {opener}")
        else:
            # else_if / else / catch / finally 가 context 의 최하위가
            # 아닌 경우는 이론상 없지만 방어적으로 alt 로 처리.
            lines.append(f"    alt {_escape_label(curr_ctx[j].get('condition', ''))}")


def render_sequence_diagram(events: List[dict], endpoint: dict,
                             controller_fqcn: str) -> str:
    """Event 리스트 → Mermaid ``sequenceDiagram`` 텍스트.

    Phase B 지원: 각 event 의 ``context_stack`` 으로 alt/else/loop/end
    자동 래핑. ``context_stack`` 이 없으면 Phase A 식 linear emit.
    """
    lines = ["sequenceDiagram"]
    used: Dict[str, str] = {}

    user_id = _participant_id("User", used)
    ctrl_id = _participant_id(controller_fqcn or "Controller", used)

    lines.append(f"    actor {user_id}")
    lines.append(f"    participant {ctrl_id} as {_escape_label(_short_alias(controller_fqcn))}")

    # service participants 미리 선언
    to_declare_services: List[str] = []
    for ev in events:
        if ev["kind"] == "call":
            to_cls = ev["to_class"]
            if to_cls and to_cls != controller_fqcn and to_cls not in used:
                if to_cls not in to_declare_services:
                    to_declare_services.append(to_cls)
    for fqcn in to_declare_services:
        pid = _participant_id(fqcn, used)
        lines.append(f"    participant {pid} as {_escape_label(_short_alias(fqcn))}")

    # Root 화살표
    http = endpoint.get("http_method") or "GET"
    url = endpoint.get("url") or "/"
    method_name = endpoint.get("method_name") or ""
    lines.append(f"    {user_id}->>{ctrl_id}: {_escape_label(http)} {_escape_label(url)}")
    if method_name:
        lines.append(f"    Note over {ctrl_id}: {_escape_label(method_name)}()")

    mapper_declared = False
    sap_declared = False
    db_declared = False

    prev_ctx: List[dict] = []
    # Events 순회 — context 전환 먼저, 그 다음 실제 화살표
    for ev in events:
        curr_ctx = ev.get("context_stack") or []
        if curr_ctx != prev_ctx:
            _emit_transition(prev_ctx, curr_ctx, lines)
            prev_ctx = curr_ctx

        if ev["kind"] == "call":
            src = _participant_id(ev["from_class"], used)
            tgt = _participant_id(ev["to_class"], used)
            label = _escape_label(ev.get("method", "") + "()")
            if ev.get("self_call"):
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

    # 마지막 events 이후 열려있는 블록들 close
    if prev_ctx:
        for _ in range(len(prev_ctx)):
            lines.append("    end")

    return "\n".join(lines)
