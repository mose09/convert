"""Oracle exports reuse the XML mapping without rewriting PL/SQL structure."""
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from oracle_embeddings.migration.mapping_loader import load_mapping
from oracle_embeddings.migration.plsql_rewriter import read_sql_file, rewrite_plsql


@pytest.fixture
def mapping_path(tmp_path):
    data = {'version': '1.0', 'tables': [
        {'type': 'rename', 'as_is': 'ORDERS', 'to_be': 'TB_ORDER'},
        {'type': 'rename', 'as_is': 'CUSTOMERS', 'to_be': 'TB_CUSTOMER'},
    ], 'columns': [
        {'as_is': {'table': 'ORDERS', 'column': 'ID'}, 'to_be': {'table': 'TB_ORDER', 'column': 'ORDER_ID'}},
        {'as_is': {'table': 'ORDERS', 'column': 'CUST_ID'}, 'to_be': {'table': 'TB_ORDER', 'column': 'CUSTOMER_ID'}},
        {'as_is': {'table': 'CUSTOMERS', 'column': 'ID'}, 'to_be': {'table': 'TB_CUSTOMER', 'column': 'CUSTOMER_ID'}},
    ]}
    p = tmp_path / 'mapping.yaml'
    p.write_text(yaml.safe_dump(data))
    return p


@pytest.fixture
def mapping(mapping_path):
    return load_mapping(mapping_path)


def test_declarations_and_control_flow(mapping):
    source = '''CREATE OR REPLACE PROCEDURE P(P_ID ORDERS.ID%TYPE) AS
 V_VAL LEGACY.ORDERS.ID%TYPE;
 R ORDERS%ROWTYPE;
 CURSOR C IS SELECT ID FROM ORDERS;
BEGIN
 SELECT o.ID INTO V_VAL FROM ORDERS o WHERE o.ID=P_ID;
 R.ID := V_VAL;
 FOR X IN (SELECT ID FROM ORDERS) LOOP NULL; END LOOP;
 OPEN C FOR SELECT ID FROM ORDERS;
EXCEPTION WHEN OTHERS THEN RAISE;
END;
/'''
    r = rewrite_plsql(source, mapping)
    assert not r.warnings
    assert 'P_ID TB_ORDER.ORDER_ID%TYPE' in r.text
    assert 'V_VAL LEGACY.TB_ORDER.ORDER_ID%TYPE' in r.text
    assert 'R TB_ORDER%ROWTYPE' in r.text
    assert 'R.ORDER_ID := V_VAL' in r.text
    assert 'SELECT o.ORDER_ID INTO V_VAL FROM TB_ORDER o WHERE o.ORDER_ID=P_ID' in r.text
    assert 'FOR X IN (SELECT ORDER_ID AS ID FROM TB_ORDER) LOOP NULL; END LOOP;' in r.text
    assert r.text.endswith('EXCEPTION WHEN OTHERS THEN RAISE;\nEND;\n/')


@pytest.mark.parametrize('source, expected', [
    ('INSERT INTO ORDERS(ID) VALUES (:ID) RETURNING ID INTO ID;',
     'INSERT INTO TB_ORDER(ORDER_ID) VALUES (:ID) RETURNING ORDER_ID INTO ID;'),
    ('UPDATE ORDERS SET ID=V_ID WHERE ID=P_ID;', 'UPDATE TB_ORDER SET ORDER_ID=V_ID WHERE ORDER_ID=P_ID;'),
    ('DELETE FROM ORDERS WHERE ID=:ID;', 'DELETE FROM TB_ORDER WHERE ORDER_ID=:ID;'),
    ('SELECT ID BULK COLLECT INTO IDS FROM ORDERS;', 'SELECT ORDER_ID BULK COLLECT INTO IDS FROM TB_ORDER;'),
    ('SELECT ORDERS.ID INTO R.ID FROM ORDERS;', 'SELECT TB_ORDER.ORDER_ID INTO R.ID FROM TB_ORDER;'),
    ('SELECT o.ID AS ID FROM ORDERS o ORDER BY ID;', 'SELECT o.ORDER_ID AS ID FROM TB_ORDER o ORDER BY ID;'),
    ('SELECT ID FROM ORDERS ORDERS;', 'SELECT ORDER_ID FROM TB_ORDER ORDERS;'),
    ('SELECT ORDERS.ID FROM ORDERS ORDERS;', 'SELECT ORDERS.ORDER_ID FROM TB_ORDER ORDERS;'),
])
def test_dml_targets_and_aliases(mapping, source, expected):
    r = rewrite_plsql(source, mapping)
    assert not r.warnings
    assert r.text == expected


def test_merge_and_join(mapping):
    s = 'MERGE INTO ORDERS o USING CUSTOMERS c ON (o.CUST_ID=c.ID) WHEN MATCHED THEN UPDATE SET o.ID=c.ID;'
    r = rewrite_plsql(s, mapping)
    assert not r.warnings
    assert r.text == 'MERGE INTO TB_ORDER o USING TB_CUSTOMER c ON (o.CUSTOMER_ID=c.CUSTOMER_ID) WHEN MATCHED THEN UPDATE SET o.ORDER_ID=c.CUSTOMER_ID;'


def test_cte_subquery_and_correlated_scopes(mapping):
    s = '''WITH x AS (SELECT ID FROM ORDERS) SELECT x.ID FROM x;
SELECT x.ID FROM (SELECT ID FROM ORDERS) x;
SELECT x.KEEP FROM (SELECT ID AS KEEP FROM ORDERS) x;
SELECT o.ID FROM ORDERS o WHERE EXISTS(SELECT 1 FROM CUSTOMERS c WHERE c.ID=o.CUST_ID);'''
    r = rewrite_plsql(s, mapping)
    assert not r.warnings
    assert 'WITH x AS (SELECT ORDER_ID FROM TB_ORDER) SELECT x.ORDER_ID FROM x;' in r.text
    assert 'SELECT x.ORDER_ID FROM (SELECT ORDER_ID FROM TB_ORDER) x;' in r.text
    assert 'SELECT x.KEEP FROM (SELECT ORDER_ID AS KEEP FROM TB_ORDER) x;' in r.text
    assert 'c.CUSTOMER_ID=o.CUSTOMER_ID' in r.text


def test_ambiguous_column_is_reported(mapping):
    r = rewrite_plsql('SELECT ID FROM ORDERS o JOIN CUSTOMERS c ON o.CUST_ID=c.ID;', mapping)
    assert 'SELECT ID FROM' in r.text
    assert any('ambiguous' in w for w in r.warnings)


def test_literals_comments_dynamic_sql_and_call_names(mapping):
    s = """CREATE OR REPLACE PACKAGE BODY ORDERS AS
PROCEDURE ID AS BEGIN
 -- SELECT ID FROM ORDERS;
 /* UPDATE ORDERS SET ID=1; */
 V := 'SELECT ID FROM ORDERS; ''ID''';
 V := q'[SELECT ID FROM ORDERS; 'quoted']';
 EXECUTE IMMEDIATE 'SELECT ID FROM ORDERS';
 OPEN C FOR V_SQL;
 ORDERS.ID(V);
 SELECT ORDERS.ID(o.ID) INTO V FROM ORDERS o;
END; END;
/"""
    r = rewrite_plsql(s, mapping)
    assert s[:s.index(' SELECT ORDERS.ID(o.ID)')] == r.text[:r.text.index(' SELECT ORDERS.ID(o.ORDER_ID)')]
    assert 'SELECT ORDERS.ID(o.ORDER_ID) INTO V FROM TB_ORDER o' in r.text
    assert len(r.warnings) == 2


def test_multiple_package_members_and_quoted_identifiers(mapping):
    s = '''CREATE OR REPLACE PACKAGE P AS
 FUNCTION F(P_ID ORDERS.ID%TYPE) RETURN ORDERS.ID%TYPE;
END;
/
CREATE OR REPLACE PACKAGE BODY P AS
 FUNCTION F(P_ID ORDERS.ID%TYPE) RETURN ORDERS.ID%TYPE IS V ORDERS.ID%TYPE;
 BEGIN SELECT "ID" INTO V FROM "ORDERS"; RETURN V; END;
 PROCEDURE G IS BEGIN UPDATE ORDERS SET ID=1; END;
END;
/'''
    r = rewrite_plsql(s, mapping)
    assert not r.warnings
    assert 'SELECT "ORDER_ID" INTO V FROM "TB_ORDER"' in r.text
    assert 'RETURN TB_ORDER.ORDER_ID%TYPE' in r.text
    assert 'UPDATE TB_ORDER SET ORDER_ID=1' in r.text


def test_unsupported_sql_and_complex_mapping_are_reported(mapping):
    r = rewrite_plsql('SELECT ID FROM ORDERS MODEL bad syntax;\nSELECT ID FROM ORDERS;', mapping)
    assert r.warnings
    assert 'SELECT ID FROM ORDERS MODEL bad syntax;' in r.text
    assert r.text.endswith('SELECT ORDER_ID FROM TB_ORDER;')
    mapping.find_table('ORDERS').type = 'split'
    r = rewrite_plsql('V ORDERS.ID%TYPE; SELECT ID FROM ORDERS;', mapping)
    assert r.text == 'V ORDERS.ID%TYPE; SELECT ID FROM ORDERS;'
    assert r.warnings


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'cp949'])
def test_encoding_newlines_and_cli_output(mapping_path, tmp_path, encoding):
    source = 'CREATE OR REPLACE PROCEDURE P AS\r\n V ORDERS.ID%TYPE; -- 한글\r\nBEGIN SELECT ID INTO V FROM ORDERS; END;\r\n/\r\n'
    folder = tmp_path / 'input' / 'nested'
    folder.mkdir(parents=True)
    sql = folder / 'object.SQL'
    sql.write_bytes(source.encode(encoding))
    output = tmp_path / 'output'
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'storage': {'output_dir': str(output)}}))
    command = [sys.executable, 'main.py', '--config', str(config), 'migrate-sql', '--mybatis-dir', str(sql), '--mapping', str(mapping_path)]
    p = subprocess.run(command, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    dest = next(output.rglob('object.SQL'))
    assert dest.parent.name == 'converted'
    expected = source.replace('ORDERS.ID%TYPE', 'TB_ORDER.ORDER_ID%TYPE').replace('SELECT ID INTO V FROM ORDERS', 'SELECT ORDER_ID INTO V FROM TB_ORDER')
    assert dest.read_bytes() == expected.encode(encoding)
    assert sql.read_bytes() == source.encode(encoding)
    assert not list(output.rglob('*_new.sql'))
    assert list(output.rglob('*.xlsx'))
    text, detected = read_sql_file(dest)
    assert text == expected


def test_mixed_directory_dry_run_and_no_recursive_output(mapping_path, tmp_path):
    inputs = tmp_path / 'input'
    (inputs / 'nested').mkdir(parents=True)
    (inputs / 'nested' / 'p.sql').write_text('BEGIN SELECT ID INTO V FROM ORDERS; END; /')
    (inputs / 'mapper.xml').write_text('<mapper namespace="X"><select id="a">SELECT ID FROM ORDERS</select></mapper>')
    output = inputs / 'output'
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'storage': {'output_dir': str(output)}}))
    command = [sys.executable, 'main.py', '--config', str(config), 'migrate-sql', '--mybatis-dir', str(inputs), '--mapping', str(mapping_path)]
    p = subprocess.run(command + ['--dry-run'], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert not list(output.rglob('p.sql'))
    for _ in range(2):
        p = subprocess.run(command, capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr
        assert 'Scanning 1 PL/SQL file(s)' in p.stdout
    dest = next(output.rglob('p.sql'))
    assert dest.relative_to(next(output.glob('migration/*'))).as_posix() == 'converted/nested/p.sql'
    assert 'SELECT ORDER_ID INTO V FROM TB_ORDER' in dest.read_text()
    dest = next(output.rglob('mapper.xml'))
    assert 'SELECT ORDER_ID FROM TB_ORDER' in dest.read_text()


def test_cursor_record_contract_and_rowtype_into(mapping):
    r = rewrite_plsql("""DECLARE
 R ORDERS%ROWTYPE;
 CURSOR C IS SELECT ID FROM ORDERS;
 S C%ROWTYPE;
BEGIN
 FOR X IN (SELECT ID FROM ORDERS) LOOP PRINT(X.ID); END LOOP;
 SELECT ID INTO R.ID FROM ORDERS;
 PRINT(R.ID);
END;""", mapping)
    assert not r.warnings
    assert 'CURSOR C IS SELECT ORDER_ID AS ID FROM TB_ORDER' in r.text
    assert 'SELECT ORDER_ID AS ID FROM TB_ORDER) LOOP PRINT(X.ID)' in r.text
    assert 'SELECT ORDER_ID INTO R.ORDER_ID FROM TB_ORDER' in r.text
    assert 'PRINT(R.ORDER_ID)' in r.text


def test_schema_qualified_mapping_and_reference(mapping_path):
    data = yaml.safe_load(mapping_path.read_text())
    data['tables'][0].update(as_is='LEGACY.ORDERS', to_be='NEW.TB_ORDER')
    data['columns'][0]['as_is']['table']='LEGACY.ORDERS'
    data['columns'][0]['to_be']['table']='NEW.TB_ORDER'
    data['columns'][1]['as_is']['table']='LEGACY.ORDERS'
    data['columns'][1]['to_be']['table']='NEW.TB_ORDER'
    mapping_path.write_text(yaml.safe_dump(data))
    m = load_mapping(mapping_path)
    r = rewrite_plsql('V LEGACY.ORDERS.ID%TYPE; SELECT LEGACY.ORDERS.ID FROM LEGACY.ORDERS;',m)
    assert not r.warnings
    assert r.text == 'V NEW.TB_ORDER.ORDER_ID%TYPE; SELECT NEW.TB_ORDER.ORDER_ID FROM NEW.TB_ORDER;'


def test_merge_derived_source_and_dml_correlation(mapping):
    r = rewrite_plsql('MERGE INTO ORDERS o USING (SELECT ID FROM CUSTOMERS) c ON(o.CUST_ID=c.ID) WHEN MATCHED THEN UPDATE SET o.ID=c.ID;',mapping)
    assert not r.warnings
    assert 'USING (SELECT CUSTOMER_ID FROM TB_CUSTOMER) c ON(o.CUSTOMER_ID=c.CUSTOMER_ID)' in r.text
    assert 'SET o.ORDER_ID=c.CUSTOMER_ID' in r.text
    r = rewrite_plsql('UPDATE ORDERS o SET ID=(SELECT c.ID FROM CUSTOMERS c WHERE c.ID=o.CUST_ID);',mapping)
    assert not r.warnings
    assert 'WHERE c.CUSTOMER_ID=o.CUSTOMER_ID' in r.text


def test_union_projection_and_cte_column_contract(mapping):
    r = rewrite_plsql('SELECT x.ID FROM (SELECT ID FROM ORDERS UNION ALL SELECT ID FROM ORDERS) x;',mapping)
    assert not r.warnings
    assert r.text.startswith('SELECT x.ORDER_ID FROM')
    r = rewrite_plsql('WITH x(ID) AS (SELECT ID FROM ORDERS) SELECT x.ID FROM x;',mapping)
    assert not r.warnings
    assert r.text == 'WITH x(ID) AS (SELECT ORDER_ID FROM TB_ORDER) SELECT x.ID FROM x;'


def test_records_are_scoped_to_packaged_members(mapping):
    s = """CREATE OR REPLACE PACKAGE BODY P AS
 PROCEDURE A IS R ORDERS%ROWTYPE;
 BEGIN R.ID := 1; END;
 PROCEDURE B IS R CUSTOMERS%ROWTYPE;
 BEGIN R.ID := 2; END;
END; /"""
    r = rewrite_plsql(s,mapping)
    assert not r.warnings
    assert 'R.ORDER_ID := 1' in r.text
    assert 'R.CUSTOMER_ID := 2' in r.text


@pytest.mark.parametrize('source', ["BEGIN V := q'[bad; SELECT ID FROM ORDERS", '/* SELECT ID FROM ORDERS', "BEGIN V := 'bad; SELECT ID FROM ORDERS"])
def test_broken_quoting_does_not_modify_source(mapping, source):
    r = rewrite_plsql(source, mapping)
    assert r.text == source
    assert r.warnings
    assert not r.changes


def test_review_using_join_renames_identifier(mapping):
    r = rewrite_plsql('SELECT ID FROM ORDERS a JOIN ORDERS b USING (ID);',mapping)
    assert not r.warnings
    assert r.text == 'SELECT ORDER_ID FROM TB_ORDER a JOIN TB_ORDER b USING (ORDER_ID);'


@pytest.mark.parametrize('source', [
    'SELECT ID FROM ORDERS a JOIN CUSTOMERS b USING (ID);',
    'SELECT ID FROM ORDERS a JOIN (SELECT ID AS ID FROM ORDERS) b USING (ID);',
    'SELECT * FROM ORDERS NATURAL JOIN CUSTOMERS;',
    'SELECT ID FROM ORDERS@REMOTE;',
    'SELECT "id" FROM "orders";',
])
def test_review_unsupported_join_remote_and_case_are_not_silently_changed(mapping,source):
    r = rewrite_plsql(source,mapping)
    assert r.text == source
    assert r.warnings
    assert not r.changes


def test_review_unmapped_column_still_updates_bare_table_qualifier(mapping):
    r = rewrite_plsql('SELECT ORDERS.UNCHANGED FROM ORDERS;',mapping)
    assert not r.warnings
    assert r.text == 'SELECT TB_ORDER.UNCHANGED FROM TB_ORDER;'


def test_review_unused_cte_is_not_a_column_source(mapping):
    r = rewrite_plsql('WITH unused AS (SELECT ID FROM CUSTOMERS) SELECT ID FROM ORDERS;',mapping)
    assert not r.warnings
    assert r.text == 'WITH unused AS (SELECT CUSTOMER_ID FROM TB_CUSTOMER) SELECT ORDER_ID FROM TB_ORDER;'


def test_review_record_type_anchor_shadows_same_named_table(mapping):
    r = rewrite_plsql('DECLARE ORDERS CUSTOMERS%ROWTYPE; V ORDERS.ID%TYPE; BEGIN ORDERS.ID:=1; END;',mapping)
    assert not r.warnings
    assert r.text == 'DECLARE ORDERS TB_CUSTOMER%ROWTYPE; V ORDERS.CUSTOMER_ID%TYPE; BEGIN ORDERS.CUSTOMER_ID:=1; END;'


def test_review_dbms_sql_dynamic_call_is_reported(mapping):
    source="BEGIN DBMS_SQL.PARSE(c,'SELECT ID FROM ORDERS',DBMS_SQL.NATIVE); END;"
    r=rewrite_plsql(source,mapping)
    assert r.text==source
    assert any('DBMS_SQL.PARSE' in w for w in r.warnings)


def test_review_comments_inside_qualified_mapping_are_not_deleted(mapping_path):
    data = yaml.safe_load(mapping_path.read_text())
    data['tables'][0].update(as_is='LEGACY.ORDERS', to_be='NEW.TB_ORDER')
    for col in data['columns']:
        if col['as_is']['table']=='ORDERS':
            col['as_is']['table']='LEGACY.ORDERS'
            col['to_be']['table']='NEW.TB_ORDER'
    mapping_path.write_text(yaml.safe_dump(data))
    mapping=load_mapping(mapping_path)
    source='V LEGACY /* keep anchor */ .ORDERS.ID%TYPE; SELECT ID FROM LEGACY /* keep SQL */ .ORDERS;'
    r=rewrite_plsql(source,mapping)
    assert r.text==source
    assert len(r.warnings)==2
    assert not r.changes


def test_review_merge_unqualified_write_columns_belong_to_target(mapping):
    source='MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) WHEN MATCHED THEN UPDATE SET ID=c.ID WHEN NOT MATCHED THEN INSERT (ID,CUST_ID) VALUES(c.ID,c.ID);'
    r=rewrite_plsql(source,mapping)
    assert not r.warnings
    assert 'UPDATE SET ORDER_ID=c.CUSTOMER_ID' in r.text
    assert 'INSERT (ORDER_ID,CUSTOMER_ID) VALUES(c.CUSTOMER_ID,c.CUSTOMER_ID)' in r.text


def test_review_output_parent_does_not_hide_sql_inputs(mapping_path,tmp_path):
    inputs=tmp_path/'source';inputs.mkdir()
    source=inputs/'p.sql';source.write_text('BEGIN SELECT ID INTO V FROM ORDERS; END; /')
    config=tmp_path/'config.yaml';config.write_text(yaml.safe_dump({'storage':{'output_dir':str(tmp_path)}}))
    p=subprocess.run([sys.executable,'main.py','--config',str(config),'migrate-sql','--mybatis-dir',str(inputs),'--mapping',str(mapping_path)],capture_output=True,text=True)
    assert p.returncode==0,p.stdout+p.stderr
    assert 'Scanning 1 PL/SQL file(s)' in p.stdout
    result=next((tmp_path/'migration').rglob('p.sql'))
    assert 'SELECT ORDER_ID INTO V FROM TB_ORDER' in result.read_text()


def test_review_nested_declare_shadowing_preserves_file(mapping):
    source='''CREATE OR REPLACE PROCEDURE P AS R ORDERS%ROWTYPE;
BEGIN
 DECLARE R SOME_CUSTOM_RECORD;
 BEGIN R.ID:=1; END;
 R.ID:=2;
END;
/'''
    r=rewrite_plsql(source,mapping)
    assert r.text==source
    assert not r.changes
    assert any('nested DECLARE' in w and 'entire PL/SQL file unchanged' in w for w in r.warnings)


def test_review_custom_record_anchor_is_not_treated_as_same_named_table(mapping):
    source='DECLARE ORDERS CUSTOM_RECORD; V ORDERS.ID%TYPE; BEGIN NULL; END; /'
    r=rewrite_plsql(source,mapping)
    assert r.text==source
    assert any('local custom-record' in w for w in r.warnings)


def test_review_multiple_anonymous_units_are_not_mistaken_for_nested_declare(mapping):
    source='DECLARE V ORDERS.ID%TYPE; BEGIN NULL; END;\n/\nDECLARE X ORDERS.ID%TYPE; BEGIN NULL; END;\n/'
    r=rewrite_plsql(source,mapping)
    assert not r.warnings
    assert r.text.count('TB_ORDER.ORDER_ID%TYPE')==2


def test_review_nested_function_shadowing_preserves_file(mapping):
    source='''CREATE OR REPLACE PROCEDURE P AS
 R ORDERS%ROWTYPE;
 FUNCTION F RETURN NUMBER IS R CUSTOMERS%ROWTYPE;
 BEGIN R.ID:=1; RETURN 1; END;
BEGIN R.ID:=2; END;
/'''
    r=rewrite_plsql(source,mapping)
    assert r.text==source
    assert not r.changes
    assert any('nested subprogram' in w for w in r.warnings)


def test_parser_diagnostics_are_single_line_plain_text(mapping):
    r=rewrite_plsql('SELECT ID FROM ORDERS MODEL bad syntax;',mapping)
    assert r.warnings
    assert all('\n' not in w and '\x1b' not in w for w in r.warnings)
