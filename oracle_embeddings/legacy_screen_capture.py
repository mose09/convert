"""capture-screens — AS-IS 화면 → Figma 편집 가능 레이어용 DOM JSON 캡처.

Playwright headless Chromium 으로 AS-IS 프론트 (React 등) 화면을 실제
렌더링한 뒤, ``assets/dom_serializer.js`` 를 주입해 DOM 레이아웃을
``docs/FIGMA_JSON_SPEC.md`` 스키마의 JSON 으로 추출한다. 산출 JSON 은
사내 Figma 플러그인 (``figma_plugin/``) 이 읽어 편집 가능한 디자인
레이어로 재구성한다.

공개 API: :func:`capture_screens`

폐쇄망 전제:
  * Playwright wheel + Chromium 브라우저 번들 사전 반입
  * ``PLAYWRIGHT_BROWSERS_PATH`` 환경변수로 번들 경로 지정
  * 외부 SaaS (html.to.design 등) 전송 없음 — 모든 처리 로컬

진단 로그는 count 기반 요약 1줄 (CLAUDE.md 단방향 환경 컨벤션).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .legacy_util import normalize_url

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SERIALIZER_PATH = Path(__file__).parent / "assets" / "dom_serializer.js"

# slug 안전화 — normalize_url 결과에서 파일명 불가 문자 치환
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9_\-.]+")

# 동적 세그먼트 감지 (라우트 원본 기준 — :id / {id} / * )
_DYNAMIC_SEG_RE = re.compile(r"(:\w+|\{\w+\}|(?<=/)\*(?=/|$))")


@dataclass
class CaptureSummary:
    """capture_screens 실행 결과 요약."""
    total: int = 0
    captured: int = 0
    failed: int = 0
    skipped: int = 0
    out_dir: str = ""
    failed_routes: list = field(default_factory=list)  # [(route, reason)]
    captured_files: list = field(default_factory=list)  # [str paths]


def route_to_slug(route: str) -> str:
    """라우트 → 파일명 슬러그.

    ``legacy_util.normalize_url`` 로 정규화 후 ``/`` → ``_``,
    ``{p}`` → ``param``. 루트(``/``)는 ``root``.
    """
    norm = normalize_url(route)
    if norm in ("", "/"):
        return "root"
    slug = norm.lstrip("/").replace("{p}", "param").replace("/", "_")
    slug = _SLUG_UNSAFE_RE.sub("-", slug)
    return slug or "root"


def is_dynamic_route(route: str) -> bool:
    """``:id`` / ``{id}`` / ``*`` 세그먼트 포함 여부."""
    return bool(_DYNAMIC_SEG_RE.search(route or ""))


def fill_route_params(route: str, param_fill: dict[str, str] | None) -> str:
    """동적 세그먼트를 ``--param-fill key=value`` 값으로 치환.

    ``:id`` 와 ``{id}`` 모두 같은 key (``id``) 로 매칭. 치환 못 한
    세그먼트가 남으면 원본 그대로 반환 (호출측이 skip 결정).
    """
    if not param_fill:
        return route
    out = route
    for key, value in param_fill.items():
        out = out.replace(":" + key, value).replace("{" + key + "}", value)
    return out


def resolve_routes(
    routes_file: str | None = None,
    frontend_dir: str | None = None,
    single_url: str | None = None,
    patterns: dict | None = None,
) -> list[str]:
    """캡처 대상 라우트 목록 결정. 우선순위:

    ① ``routes_file`` — 한 줄 1라우트 텍스트 파일 (``#`` 주석/빈 줄 무시)
    ② ``frontend_dir`` — ``legacy_react_router.build_url_to_component_map``
       으로 React 라우트 자동 추출. patterns.yaml 의
       ``url.url_prefix_strip`` / ``url.react_route_prefix`` 적용
       (analyze-legacy 와 동일).
    ③ ``single_url`` — 단일 라우트

    반환 목록은 입력 순서 유지 + 중복 제거.
    """
    routes: list[str] = []
    seen: set[str] = set()

    def _add(r: str):
        r = (r or "").strip()
        if not r or r in seen:
            return
        seen.add(r)
        routes.append(r)

    if routes_file:
        try:
            text = Path(routes_file).read_text(encoding="utf-8")
        except OSError as e:
            raise SystemExit(f"routes 파일 읽기 실패: {routes_file}\n  {e}") from e
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            _add(line)
        return routes

    if frontend_dir:
        from .legacy_react_router import build_url_to_component_map
        url_cfg = (patterns or {}).get("url") or {}
        strip_patterns = url_cfg.get("url_prefix_strip") or None
        route_prefix = url_cfg.get("react_route_prefix") or None
        url_map = build_url_to_component_map(
            frontend_dir,
            strip_patterns=strip_patterns,
            route_prefix=route_prefix,
        )
        for url in sorted(url_map.keys()):
            _add(url)
        return routes

    if single_url:
        _add(single_url)
        return routes

    return routes


def _load_serializer() -> str:
    try:
        return _SERIALIZER_PATH.read_text(encoding="utf-8")
    except OSError as e:
        raise SystemExit(
            f"dom_serializer.js 읽기 실패: {_SERIALIZER_PATH}\n  {e}"
        ) from e


def capture_screens(
    base_url: str,
    routes: list[str],
    out_dir: str | os.PathLike,
    *,
    viewport: tuple[int, int] = (1920, 1080),
    storage_state: str | None = None,
    wait_selector: str | None = None,
    wait_ms: int = 0,
    max_image_kb: int = 500,
    param_fill: dict[str, str] | None = None,
) -> CaptureSummary:
    """라우트 목록을 순회 캡처해 화면별 JSON 저장.

    Parameters
    ----------
    base_url : AS-IS 프론트 베이스 URL (예: ``http://localhost:3000``)
    routes : 캡처할 라우트 목록 (예: ``["/order/list", "/material"]``)
    out_dir : JSON 출력 디렉토리
    viewport : 브라우저 viewport (w, h)
    storage_state : Playwright storage_state JSON 경로 (로그인 세션 주입)
    wait_selector : 렌더 완료 대기 CSS selector (미지정 시 networkidle)
    wait_ms : 추가 고정 대기 (밀리초)
    max_image_kb : 이미지 base64 최대 크기 — 초과 시 placeholder RECT
    param_fill : 동적 세그먼트 치환 값. 치환 안 된 동적 라우트는 skip

    Returns
    -------
    CaptureSummary — captured / failed / skipped count + 실패 사유
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise SystemExit(
            "playwright 미설치 — capture-screens 사용 불가.\n"
            "  폐쇄망 설치: requirements.txt 의 Playwright 주석 참고\n"
            "  (wheel + Chromium 번들 반입 + PLAYWRIGHT_BROWSERS_PATH)"
        ) from e

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    serializer_js = _load_serializer()
    base = (base_url or "").rstrip("/")

    summary = CaptureSummary(total=len(routes), out_dir=str(out_path))

    # 동적 라우트 사전 분류 — param_fill 적용 후에도 동적이면 skip
    runnable: list[tuple[str, str]] = []  # (원본 route, 실제 URL path)
    for route in routes:
        filled = fill_route_params(route, param_fill)
        if is_dynamic_route(filled):
            summary.skipped += 1
            summary.failed_routes.append(
                (route, "동적 세그먼트 미치환 — --param-fill 로 치환 가능")
            )
            continue
        runnable.append((route, filled))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context_kwargs: dict = {
            "viewport": {"width": viewport[0], "height": viewport[1]},
        }
        if storage_state:
            context_kwargs["storage_state"] = storage_state
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        for route, filled in runnable:
            url = base + (filled if filled.startswith("/") else "/" + filled)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if wait_selector:
                    page.wait_for_selector(wait_selector, timeout=15000)
                else:
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass  # SPA 가 폴링하면 networkidle 안 옴 — 진행
                if wait_ms > 0:
                    page.wait_for_timeout(wait_ms)

                page.evaluate(serializer_js)
                root = page.evaluate(
                    f"window.__serializeDom({{maxImageKb: {int(max_image_kb)}}})"
                )
                doc = {
                    "schemaVersion": SCHEMA_VERSION,
                    "meta": {
                        "url": filled,
                        "title": page.title() or "",
                        "viewport": {"w": viewport[0], "h": viewport[1]},
                        "capturedAt": datetime.now().astimezone().isoformat(),
                    },
                    "root": root,
                }
                slug = route_to_slug(route)
                fp = out_path / f"{slug}.json"
                fp.write_text(
                    json.dumps(doc, ensure_ascii=False),
                    encoding="utf-8",
                )
                summary.captured += 1
                summary.captured_files.append(str(fp))
            except Exception as e:  # noqa: BLE001 — 라우트 1건 실패가 전체 중단 금지
                summary.failed += 1
                reason = str(e).split("\n")[0][:200]
                summary.failed_routes.append((route, reason))
                logger.warning("capture 실패 %s: %s", route, reason)

        context.close()
        browser.close()

    # 실패 라우트 기록
    if summary.failed_routes:
        lines = ["# capture-screens 실패/스킵 라우트", ""]
        for route, reason in summary.failed_routes:
            lines.append(f"- `{route}` — {reason}")
        (out_path / "_failed.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    print(
        f"  Captured: {summary.captured}/{summary.total} screens "
        f"(failed: {summary.failed}, skipped: {summary.skipped})"
    )
    return summary
