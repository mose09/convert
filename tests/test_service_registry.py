"""서비스 ID 레지스트리 (httpSend 계열 프레임워크) 회귀 테스트.

JSP: ``httpSend("fabCBMDataList", ...)`` → service XML:
``<service id="fabCBMDataList" serviceClass="com...CBMDataController">``
→ 컨트롤러 합성 엔드포인트(/<id>) 매칭.
"""
from oracle_embeddings.legacy_service_registry import scan_service_registry
from oracle_embeddings.legacy_jsp_scanner import (
    build_api_url_index, extract_button_triggers,
)

_SERVICE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<services>
  <service id="fabCBMDataList" appId="Application"
           serviceClass="com.skhy.fab.CBMData.controller.CBMDataController" >
  </service>
  <service serviceClass="com.skhy.fab.X" id="attrOrderSwapped"/>
  <service id="noClassAttr" appId="App"></service>
</services>
"""

_JSP = """<%@ page contentType="text/html; charset=UTF-8" %>
<button type="button" onclick="onSearchList()">조회</button>
<script>
function onSearchList(){
    var paramJson = getParam();
    if(paramJson) httpSend("fabCBMDataList", paramJson, onOk, onFail, httpOption);
}
</script>
"""


def test_scan_service_registry(tmp_path):
    d = tmp_path / "backend" / "config"
    d.mkdir(parents=True)
    (d / "service-config.xml").write_text(_SERVICE_XML, encoding="utf-8")
    reg = scan_service_registry([str(tmp_path / "backend")])
    assert reg["fabCBMDataList"]["class"] == \
        "com.skhy.fab.CBMData.controller.CBMDataController"
    # 속성 순서가 바뀌어도 인식
    assert reg["attrOrderSwapped"]["class"] == "com.skhy.fab.X"
    # serviceClass 없는 <service> 태그는 무시
    assert "noClassAttr" not in reg


def test_httpsend_service_id_extracted(tmp_path):
    d = tmp_path / "front"
    d.mkdir()
    (d / "list.jsp").write_text(_JSP, encoding="utf-8")
    api = build_api_url_index(str(d))
    # httpSend 첫 인자(bare 서비스 ID)가 /<id> pseudo-URL 로
    assert "/fabcbmdatalist" in api
    trig = extract_button_triggers(str(d), api)
    assert trig.get("/fabcbmdatalist") == ["조회"]


def test_custom_service_call_method_via_patterns(tmp_path):
    d = tmp_path / "front"
    d.mkdir()
    (d / "x.jsp").write_text(
        '<button onclick="callSvc(\'mySvcId\')">실행</button>',
        encoding="utf-8")
    patterns = {"frontend": {"service_call_methods": ["callSvc"]}}
    api = build_api_url_index(str(d), patterns=patterns)
    assert "/mysvcid" in api
    trig = extract_button_triggers(str(d), api, patterns=patterns)
    assert trig.get("/mysvcid") == ["실행"]


def test_dot_service_extension_scanned(tmp_path):
    """정의 파일이 .xml 이 아니라 *.service 확장자여도 수집 (사용자 실환경)."""
    d = tmp_path / "backend" / "config"
    d.mkdir(parents=True)
    (d / "CBMData.service").write_text(_SERVICE_XML, encoding="utf-8")
    reg = scan_service_registry([str(tmp_path / "backend")])
    assert reg["fabCBMDataList"]["class"] == \
        "com.skhy.fab.CBMData.controller.CBMDataController"
