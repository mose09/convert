"""User-visible PL/SQL guidance, warning visibility and safe folder publishing."""
from pathlib import Path

import pytest
import yaml
from openpyxl import load_workbook
from streamlit.testing.v1 import AppTest


APP = str(Path(__file__).resolve().parents[1] / 'webui' / 'app.py')


def migration_app():
    app=AppTest.from_file(APP,default_timeout=30).run()
    next(r for r in app.radio if r.label=='화면').set_value('SQL 마이그레이션').run()
    assert not app.exception
    return app


def folder_app(tmp_path,source,output=None):
    inputs=tmp_path/'input';inputs.mkdir()
    (inputs/'object.sql').write_text(source)
    mapping=tmp_path/'mapping.yaml'
    mapping.write_text(yaml.safe_dump({'version':'1.0','tables':[{'type':'rename','as_is':'ORDERS','to_be':'TB_ORDER'}],
        'columns':[{'as_is':{'table':'ORDERS','column':'ID'},'to_be':{'table':'TB_ORDER','column':'ORDER_ID'}}]}))
    output=output or tmp_path/'result'
    app=migration_app()
    next(r for r in app.radio if r.label=='실행 방식').set_value('폴더 지정 (지정 폴더로 출력)').run()
    for widget in app.text_input:
        if widget.label.startswith('①'):widget.set_value(str(inputs))
        elif widget.label.startswith('②'):widget.set_value(str(mapping))
        elif widget.label.startswith('③'):widget.set_value(str(output))
    app.run()
    return app,inputs,output


def run_conversion(app):
    next(b for b in app.button if b.label=='▶ 변환 실행').click().run()
    assert not app.exception, [e.message for e in app.exception]


def test_help_explains_examples_and_partial_conversion():
    app=migration_app()
    assert any('AUTO_WARN' in msg.value and 'Conversions' in msg.value for msg in app.info)
    assert any('동적 SQL·모호한 구문이란?' in e.label for e in app.expander)
    code='\n'.join(c.value for c in app.code)
    assert 'EXECUTE IMMEDIATE' in code and 'V_TABLE_NAME' in code
    assert 'SELECT ID' in code and 'SELECT o.ID' in code
    prose='\n'.join(m.value for m in app.markdown)
    assert '고정 문자열이어도' in prose
    assert '컴파일 성공을 의미하지 않습니다' in '\n'.join(c.value for c in app.caption)


def test_upload_result_warnings_are_visible_without_truncated_log():
    app=migration_app()
    warning='warning: object.sql: line 4: dynamic SQL is preserved; inspect EXECUTE IMMEDIATE manually'
    app.session_state['mig_result']={'ok':True,'summary':'Converted 1 statement(s): AUTO=0 AUTO_WARN=1 NEEDS_LLM=0 UNRESOLVED=0 PARSE_FAIL=0',
        'zip':b'zip','log':warning+'\n'+'\n'.join(f'normal line {n}' for n in range(100))}
    app.run()
    assert not app.exception
    assert app.warning and not app.success
    assert any(warning in t.value for t in app.text)
    assert any(e.label=='수동 검토 항목' and e.proto.expanded for e in app.expander)
    assert app.get('download_button')


def test_folder_warning_and_excel_notes(tmp_path):
    source="BEGIN SELECT ID INTO V FROM ORDERS; EXECUTE IMMEDIATE 'SELECT ID FROM ORDERS'; END; /"
    app,inputs,output=folder_app(tmp_path,source)
    run_conversion(app)
    assert app.warning and not app.success and not app.error
    result=(output/'object.sql').read_text()
    assert 'SELECT ORDER_ID INTO V FROM TB_ORDER' in result
    assert "EXECUTE IMMEDIATE 'SELECT ID FROM ORDERS'" in result
    assert (inputs/'object.sql').read_text()==source
    assert any('object.sql: line 1' in t.value for t in app.text)
    workbook=load_workbook(next(output.glob('*.xlsx')),read_only=True)
    rows=list(workbook['Conversions'].values)
    status=rows[0].index('Status');notes=rows[0].index('Notes')
    assert rows[1][status]=='AUTO_WARN'
    assert 'dynamic SQL' in rows[1][notes]
    summary={row[0]:row[1] for row in workbook['Summary'].values if len(row)>1 and row[0]}
    assert summary['Warning-free entries rate']=='0.0% (AUTO only)'
    assert 'Automation rate' not in summary


@pytest.mark.parametrize('relationship',['same','inside','parent'])
def test_related_output_folders_cannot_overwrite_or_reingest_sources(tmp_path,relationship):
    output={'same':tmp_path/'input','inside':tmp_path/'input'/'result','parent':tmp_path}[relationship]
    source='BEGIN SELECT ID INTO V FROM ORDERS; END; /'
    app,inputs,_=folder_app(tmp_path,source,output)
    run_conversion(app)
    assert app.error and '별도 폴더' in app.error[0].value
    assert (inputs/'object.sql').read_text()==source
    assert not list(tmp_path.rglob('*.xlsx'))


def test_failed_batch_does_not_copy_partial_output(tmp_path):
    source='BEGIN SELECT ID INTO V FROM ORDERS; END; /'
    app,inputs,output=folder_app(tmp_path,source)
    (inputs/'z_invalid.sql').write_bytes(b'\xff\xff')
    run_conversion(app)
    assert app.error and not app.success
    assert not list(output.glob('*.sql'))
    assert not list(output.glob('*.xlsx'))
    assert (inputs/'object.sql').read_text()==source


def test_existing_output_symlink_cannot_overwrite_original(tmp_path):
    source='BEGIN SELECT ID INTO V FROM ORDERS; END; /'
    app,inputs,output=folder_app(tmp_path,source)
    output.mkdir()
    (output/'object.sql').symlink_to(inputs/'object.sql')
    run_conversion(app)
    assert app.error
    assert '출력 경로' in app.error[0].value
    assert (inputs/'object.sql').read_text()==source
    assert not list(output.glob('*.xlsx'))
