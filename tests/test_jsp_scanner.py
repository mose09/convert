"""JSP 프론트 스캐너 — 버튼→백엔드 URL 추출 회귀 테스트."""
import os

from oracle_embeddings.legacy_jsp_scanner import (
    build_api_url_index, extract_button_triggers, count_jsp_files,
)
from oracle_embeddings.legacy_frontend import (
    detect_frontend_framework, build_frontend_api_index,
)

_LIST_JSP = """<%@ page contentType="text/html; charset=UTF-8" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<html><body>
<%-- 주석 안 URL 은 무시: /ignore/inComment.do --%>
<form id="searchForm" name="searchForm" action="/user/userList.do" method="post">
  <input type="text" name="userName"/>
  <button type="submit">조회</button>
  <button type="button" onclick="fnSave()">저장</button>
  <input type="button" value="삭제" onclick="location.href='/user/userDelete.do'"/>
  <a href="/user/userExcel.do">엑셀다운</a>
  <a href="#" onclick="fnDetail()">상세</a>
  <a href="/css/style.css">스타일(정적, 무시)</a>
</form>
<script>
function fnSave(){ $.ajax({ url:'/user/userSave.do', type:'post' }); }
function fnDetail(){ goPage('${ctx}/user/userDetail.do'); }
</script>
</body></html>
"""

_COMMON_JS = "function goPage(url){ location.href = url; }\n"

_PATTERNS = {"frontend": {"api_call_methods": ["goPage", "fnSubmit"]}}


def _make_app(tmp_path):
    d = tmp_path / "jspapp"
    (d / "user").mkdir(parents=True)
    (d / "user" / "userList.jsp").write_text(_LIST_JSP, encoding="utf-8")
    (d / "user" / "common.js").write_text(_COMMON_JS, encoding="utf-8")
    return str(d)


def test_detect_jsp(tmp_path):
    app = _make_app(tmp_path)
    assert count_jsp_files(app) == 1
    assert detect_frontend_framework(app) == "jsp"


def test_api_index_covers_all_call_sites(tmp_path):
    app = _make_app(tmp_path)
    api = build_api_url_index(app, patterns=_PATTERNS)
    keys = set(api.keys())
    assert "/user/userlist.do" in keys      # form action
    assert "/user/userdelete.do" in keys    # location.href
    assert "/user/userexcel.do" in keys     # <a href>
    assert "/user/usersave.do" in keys      # $.ajax url
    assert "/user/userdetail.do" in keys    # goPage(${ctx}/...) EL 정리
    # 정적 리소스 / 주석 안 URL 은 제외
    assert not any("style.css" in k for k in keys)
    assert not any("incomment" in k for k in keys)


def test_button_triggers_map_labels(tmp_path):
    app = _make_app(tmp_path)
    api = build_api_url_index(app, patterns=_PATTERNS)
    trig = extract_button_triggers(app, api, patterns=_PATTERNS)
    assert trig.get("/user/userlist.do") == ["조회"]     # form submit → action
    assert trig.get("/user/usersave.do") == ["저장"]     # onclick fnSave → ajax
    assert trig.get("/user/userdelete.do") == ["삭제"]   # inline location.href
    assert trig.get("/user/userdetail.do") == ["상세"]   # cross-file goPage


def test_dispatch_via_build_frontend_api_index(tmp_path):
    """legacy_frontend 진입점이 JSP 로 자동 분기하는지."""
    app = _make_app(tmp_path)
    api, trig = build_frontend_api_index(app, patterns=_PATTERNS)
    assert api and trig
    assert trig.get("/user/userdelete.do") == ["삭제"]


def test_nested_quote_onclick(tmp_path):
    """onclick=\"location.href='...'\" 중첩 따옴표도 파싱."""
    d = tmp_path / "app"
    d.mkdir()
    (d / "x.jsp").write_text(
        "<button onclick=\"location.href='/a/b.do'\">이동</button>",
        encoding="utf-8")
    api = build_api_url_index(str(d))
    trig = extract_button_triggers(str(d), api)
    assert "/a/b.do" in api
    assert trig.get("/a/b.do") == ["이동"]
