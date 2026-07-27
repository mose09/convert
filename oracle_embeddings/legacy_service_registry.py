"""Service XML 레지스트리 파서 — 서비스ID 기반 프레임워크 지원.

일부 사내 프레임워크(SK 계열 등)는 URL 대신 **서비스 ID** 로 백엔드를
호출한다:

  JSP:  ``httpSend("fabCBMDataList", paramJson, onSuccess, onFail, opt);``
  XML:  ``<service id="fabCBMDataList" appId="Application"
             serviceClass="com.skhy.fab.CBMData.controller.CBMDataController" >``

이 모듈은 backend/frontend 디렉토리에서 위 형태의 service 정의 XML 을
찾아 ``{service_id: {"class": FQCN, "method": str, "file": relpath}}`` 맵을
만든다. analyze-legacy 는 이 맵으로 serviceClass 클래스에 합성 엔드포인트
(``/<service_id>``)를 붙여, JSP 스캐너가 뽑은 ``httpSend`` 서비스 ID 와
정규화 키가 일치하게 만든다 (버튼 → 서비스ID → 컨트롤러 체인 연결).
"""
from __future__ import annotations

import logging
import os
import re

from .mybatis_parser import _read_file_safe

logger = logging.getLogger(__name__)

_SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "target", "build",
              "dist", "bin", "out"}

# <service ...> 태그 전체 (self-closing / 열림 모두). 속성 순서 무관하게
# 태그 안에서 개별 속성을 다시 뽑는다.
_SERVICE_TAG_RE = re.compile(r"<service\b([^>]*?)/?>", re.IGNORECASE | re.DOTALL)
_ATTR_RES = {
    "id": re.compile(r"\bid\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
    "class": re.compile(r"\bserviceClass\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
    # 프레임워크에 따라 method / serviceMethod / methodName 등 변형 존재
    "method": re.compile(r"\b(?:serviceMethod|methodName|method)\s*=\s*[\"']([^\"']+)[\"']",
                          re.IGNORECASE),
}


def scan_service_registry(dirs: list[str]) -> dict[str, dict]:
    """``dirs`` 하위 모든 .xml 에서 service 정의를 수집한다.

    반환: ``{service_id: {"class": FQCN, "method": str, "file": relpath}}``.
    같은 id 가 여러 번 정의되면 첫 정의 유지 (first-win). serviceClass 가
    없는 <service> 태그(다른 스키마의 우연한 동명 태그)는 무시하므로
    MyBatis mapper / Spring bean XML 과 섞여 있어도 안전하다.
    """
    out: dict[str, dict] = {}
    for base in dirs:
        if not base or not os.path.isdir(base):
            continue
        for root, dnames, fnames in os.walk(base):
            dnames[:] = [d for d in dnames if d not in _SKIP_DIRS
                         and not d.startswith(".")]
            for fn in fnames:
                if not fn.lower().endswith(".xml"):
                    continue
                path = os.path.join(root, fn)
                try:
                    text = _read_file_safe(path)
                except Exception:
                    continue
                # 빠른 사전 필터 — serviceClass 문자열 없는 파일 skip
                if "serviceClass" not in text:
                    continue
                rel = os.path.relpath(path, base).replace(os.sep, "/")
                for m in _SERVICE_TAG_RE.finditer(text):
                    attrs = m.group(1)
                    mid = _ATTR_RES["id"].search(attrs)
                    mcls = _ATTR_RES["class"].search(attrs)
                    if not mid or not mcls:
                        continue
                    sid = mid.group(1).strip()
                    if not sid or sid in out:
                        continue
                    mmeth = _ATTR_RES["method"].search(attrs)
                    out[sid] = {
                        "class": mcls.group(1).strip(),
                        "method": (mmeth.group(1).strip() if mmeth else ""),
                        "file": rel,
                    }
    if out:
        logger.info("Service registry: %d service definitions", len(out))
    return out
