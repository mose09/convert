"""Independent Oracle MERGE coverage: source contracts and PL/SQL stay intact."""
import pytest
import yaml

from oracle_embeddings.migration.mapping_loader import load_mapping
from oracle_embeddings.migration.plsql_rewriter import rewrite_plsql


@pytest.fixture
def merge_mapping(tmp_path):
    path = tmp_path / 'mapping.yaml'
    path.write_text(yaml.safe_dump({
        'version': '1.0',
        'tables': [
            {'type': 'rename', 'as_is': 'ORDERS', 'to_be': 'TB_ORDER'},
            {'type': 'rename', 'as_is': 'CUSTOMERS', 'to_be': 'TB_CUSTOMER'},
        ],
        'columns': [
            {'as_is': {'table': table, 'column': column},
             'to_be': {'table': target_table, 'column': target_column}}
            for table, target_table, columns in [
                ('ORDERS', 'TB_ORDER', [('ID', 'ORDER_ID'), ('CUST_ID', 'CUSTOMER_ID')]),
                ('CUSTOMERS', 'TB_CUSTOMER', [('ID', 'CUSTOMER_ID'), ('NAME', 'CUSTOMER_NAME')]),
            ]
            for column, target_column in columns
        ],
    }))
    return load_mapping(path)


# Expectations are identifier-only edits. No formatter or SQL regeneration may
# change Oracle hints, whitespace, literals, bind names, or procedural variables.
MERGES = [
    (
        'MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
        'WHEN MATCHED THEN UPDATE SET o.ID=c.ID '
        'WHEN NOT MATCHED THEN INSERT(ID,CUST_ID) VALUES(c.ID,c.ID);',
        'MERGE INTO TB_ORDER o USING TB_CUSTOMER c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET o.ORDER_ID=c.CUSTOMER_ID '
        'WHEN NOT MATCHED THEN INSERT(ORDER_ID,CUSTOMER_ID) VALUES(c.CUSTOMER_ID,c.CUSTOMER_ID);',
    ),
    (
        'MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
        'WHEN MATCHED THEN UPDATE SET ID=c.ID WHERE c.ID>:ID;',
        'MERGE INTO TB_ORDER o USING TB_CUSTOMER c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET ORDER_ID=c.CUSTOMER_ID WHERE c.CUSTOMER_ID>:ID;',
    ),
    (
        'MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
        'WHEN NOT MATCHED THEN INSERT(ID,CUST_ID) VALUES(c.ID,:CUST_ID) WHERE c.ID>0;',
        'MERGE INTO TB_ORDER o USING TB_CUSTOMER c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN NOT MATCHED THEN INSERT(ORDER_ID,CUSTOMER_ID) VALUES(c.CUSTOMER_ID,:CUST_ID) WHERE c.CUSTOMER_ID>0;',
    ),
    (
        'MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
        'WHEN MATCHED THEN UPDATE SET ID=c.ID DELETE WHERE o.ID<0;',
        'MERGE INTO TB_ORDER o USING TB_CUSTOMER c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET ORDER_ID=c.CUSTOMER_ID DELETE WHERE o.ORDER_ID<0;',
    ),
    (
        'MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
        'WHEN MATCHED THEN UPDATE SET o.ID=c.ID WHERE c.ID>0 DELETE WHERE o.ID<0 '
        'WHEN NOT MATCHED THEN INSERT(ID,CUST_ID) VALUES(c.ID,c.ID) WHERE c.ID>0;',
        'MERGE INTO TB_ORDER o USING TB_CUSTOMER c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET o.ORDER_ID=c.CUSTOMER_ID WHERE c.CUSTOMER_ID>0 DELETE WHERE o.ORDER_ID<0 '
        'WHEN NOT MATCHED THEN INSERT(ORDER_ID,CUSTOMER_ID) VALUES(c.CUSTOMER_ID,c.CUSTOMER_ID) WHERE c.CUSTOMER_ID>0;',
    ),
    (
        'MERGE /*+ USE_HASH(o c) */ INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
        'WHEN MATCHED THEN UPDATE SET o.ID=NVL(c.ID,:ID);',
        'MERGE /*+ USE_HASH(o c) */ INTO TB_ORDER o USING TB_CUSTOMER c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET o.ORDER_ID=NVL(c.CUSTOMER_ID,:ID);',
    ),
    (
        'MERGE INTO ORDERS o USING (SELECT ID FROM CUSTOMERS) c ON(o.CUST_ID=c.ID) '
        'WHEN NOT MATCHED THEN INSERT(ID) VALUES(c.ID);',
        'MERGE INTO TB_ORDER o USING (SELECT CUSTOMER_ID FROM TB_CUSTOMER) c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN NOT MATCHED THEN INSERT(ORDER_ID) VALUES(c.CUSTOMER_ID);',
    ),
    (
        'MERGE INTO ORDERS o USING (SELECT ID AS ID FROM CUSTOMERS) c ON(o.CUST_ID=c.ID) '
        'WHEN NOT MATCHED THEN INSERT(ID) VALUES(c.ID);',
        'MERGE INTO TB_ORDER o USING (SELECT CUSTOMER_ID AS ID FROM TB_CUSTOMER) c ON(o.CUSTOMER_ID=c.ID) '
        'WHEN NOT MATCHED THEN INSERT(ORDER_ID) VALUES(c.ID);',
    ),
    (
        'MERGE INTO ORDERS o USING (SELECT :ID ID, V_NAME NAME FROM DUAL) c ON(o.ID=c.ID) '
        'WHEN NOT MATCHED THEN INSERT(ID) VALUES(c.ID);',
        'MERGE INTO TB_ORDER o USING (SELECT :ID ID, V_NAME NAME FROM DUAL) c ON(o.ORDER_ID=c.ID) '
        'WHEN NOT MATCHED THEN INSERT(ORDER_ID) VALUES(c.ID);',
    ),
    (
        'MERGE INTO LEGACY.ORDERS o USING LEGACY.CUSTOMERS c ON(o.CUST_ID=c.ID) '
        'WHEN MATCHED THEN UPDATE SET o.ID=c.ID;',
        'MERGE INTO LEGACY.TB_ORDER o USING LEGACY.TB_CUSTOMER c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET o.ORDER_ID=c.CUSTOMER_ID;',
    ),
    (
        'MERGE INTO ORDERS o USING (WITH x AS (SELECT ID FROM CUSTOMERS) SELECT ID FROM x) c '
        'ON(o.CUST_ID=c.ID) WHEN MATCHED THEN UPDATE SET o.ID=c.ID;',
        'MERGE INTO TB_ORDER o USING (WITH x AS (SELECT CUSTOMER_ID FROM TB_CUSTOMER) SELECT CUSTOMER_ID FROM x) c '
        'ON(o.CUSTOMER_ID=c.CUSTOMER_ID) WHEN MATCHED THEN UPDATE SET o.ORDER_ID=c.CUSTOMER_ID;',
    ),
    (
        'MERGE INTO ORDERS o USING (SELECT ID FROM CUSTOMERS UNION ALL SELECT ID FROM CUSTOMERS) c '
        'ON(o.CUST_ID=c.ID) WHEN MATCHED THEN UPDATE SET o.ID=c.ID;',
        'MERGE INTO TB_ORDER o USING (SELECT CUSTOMER_ID FROM TB_CUSTOMER UNION ALL SELECT CUSTOMER_ID FROM TB_CUSTOMER) c '
        'ON(o.CUSTOMER_ID=c.CUSTOMER_ID) WHEN MATCHED THEN UPDATE SET o.ORDER_ID=c.CUSTOMER_ID;',
    ),
    (
        'MERGE INTO ORDERS USING CUSTOMERS ON(ORDERS.CUST_ID=CUSTOMERS.ID) '
        'WHEN MATCHED THEN UPDATE SET ORDERS.ID=CUSTOMERS.ID;',
        'MERGE INTO TB_ORDER USING TB_CUSTOMER ON(TB_ORDER.CUSTOMER_ID=TB_CUSTOMER.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET TB_ORDER.ORDER_ID=TB_CUSTOMER.CUSTOMER_ID;',
    ),
    (
        'MERGE INTO ORDERS o USING (SELECT * FROM CUSTOMERS) c ON(o.CUST_ID=c.ID) '
        'WHEN MATCHED THEN UPDATE SET o.ID=c.ID;',
        'MERGE INTO TB_ORDER o USING (SELECT * FROM TB_CUSTOMER) c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET o.ORDER_ID=c.CUSTOMER_ID;',
    ),
    (
        'MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
        'WHEN NOT MATCHED THEN INSERT (ID,CUST_ID) VALUES(c.ID,c.ID) '
        'WHEN MATCHED THEN UPDATE SET ID=c.ID;',
        'MERGE INTO TB_ORDER o USING TB_CUSTOMER c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN NOT MATCHED THEN INSERT (ORDER_ID,CUSTOMER_ID) VALUES(c.CUSTOMER_ID,c.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET ORDER_ID=c.CUSTOMER_ID;',
    ),
    (
        'MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
        'WHEN MATCHED THEN UPDATE SET o.ID=c.ID WHERE EXISTS(SELECT 1 FROM CUSTOMERS x WHERE x.ID=c.ID);',
        'MERGE INTO TB_ORDER o USING TB_CUSTOMER c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET o.ORDER_ID=c.CUSTOMER_ID WHERE EXISTS(SELECT 1 FROM TB_CUSTOMER x WHERE x.CUSTOMER_ID=c.CUSTOMER_ID);',
    ),
    (
        'MERGE INTO "ORDERS" "o" USING "CUSTOMERS" "c" ON("o"."CUST_ID"="c"."ID") '
        'WHEN NOT MATCHED THEN INSERT("ID") VALUES("c"."ID");',
        None,  # Lowercase quoted aliases require explicit review under the existing policy.
    ),
    (
        'MERGE INTO "ORDERS" "O" USING "CUSTOMERS" "C" ON("O"."CUST_ID"="C"."ID") '
        'WHEN NOT MATCHED THEN INSERT("ID") VALUES("C"."ID");',
        'MERGE INTO "TB_ORDER" "O" USING "TB_CUSTOMER" "C" ON("O"."CUSTOMER_ID"="C"."CUSTOMER_ID") '
        'WHEN NOT MATCHED THEN INSERT("ORDER_ID") VALUES("C"."CUSTOMER_ID");',
    ),
]


@pytest.mark.parametrize('wrapped', [False, True], ids=['standalone', 'procedure'])
@pytest.mark.parametrize('source, expected', MERGES)
def test_oracle_merge_identifier_only_rewrite(merge_mapping, source, expected, wrapped):
    prefix = ('CREATE OR REPLACE PROCEDURE P(ID NUMBER) AS\r\n'
              " V_NAME VARCHAR2(100) := 'ORDERS.ID'; -- ORDERS.ID\r\nBEGIN\r\n ") if wrapped else ''
    suffix = '\r\n COMMIT;\r\nEXCEPTION WHEN OTHERS THEN RAISE;\r\nEND;\r\n/\r\n' if wrapped else ''
    result = rewrite_plsql(prefix + source + suffix, merge_mapping)
    if expected is None:
        assert result.text == prefix + source + suffix
        assert result.warnings
        assert not result.changes
    else:
        assert not result.warnings
        assert result.text == prefix + expected + suffix
        assert result.statements == 1
        assert result.changes


def test_multiple_merges_and_nonmerge_oracle_statements(merge_mapping):
    source = '''CREATE OR REPLACE PROCEDURE P AS
 V ORDERS.ID%TYPE;
BEGIN
 SELECT o.ID INTO V FROM ORDERS o WHERE o.ID=:ID;
 MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID)
 WHEN MATCHED THEN UPDATE SET o.ID=c.ID;
 UPDATE ORDERS SET ID=V WHERE CUST_ID=:CUST_ID;
 MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID)
 WHEN NOT MATCHED THEN INSERT(ID,CUST_ID) VALUES(c.ID,c.ID);
 DELETE FROM ORDERS WHERE ID=V;
END;
/'''
    result = rewrite_plsql(source, merge_mapping)
    assert not result.warnings
    assert result.statements == 5
    assert 'V TB_ORDER.ORDER_ID%TYPE;' in result.text
    assert 'SELECT o.ORDER_ID INTO V FROM TB_ORDER o WHERE o.ORDER_ID=:ID;' in result.text
    assert result.text.count('MERGE INTO TB_ORDER o USING TB_CUSTOMER c') == 2
    assert 'UPDATE TB_ORDER SET ORDER_ID=V WHERE CUSTOMER_ID=:CUST_ID;' in result.text
    assert 'DELETE FROM TB_ORDER WHERE ORDER_ID=V;' in result.text


@pytest.mark.parametrize('body, expected, count', [
    (
        'UPDATE ORDERS SET ID=1; UPDATE ORDERS SET CUST_ID=2; UPDATE CUSTOMERS SET NAME=V_NAME WHERE ID=3;',
        'UPDATE TB_ORDER SET ORDER_ID=1; UPDATE TB_ORDER SET CUSTOMER_ID=2; UPDATE TB_CUSTOMER SET CUSTOMER_NAME=V_NAME WHERE CUSTOMER_ID=3;',
        3,
    ),
    (
        "V_NAME := 'UPDATE ORDERS SET ID=9;'; -- UPDATE ORDERS SET ID=9;\n"
        "UPDATE ORDERS SET ID=1; /* ; UPDATE ORDERS SET ID=9; */\n"
        "V_NAME := q'[UPDATE ORDERS SET ID=9; 'quoted']'; UPDATE ORDERS SET CUST_ID=2;",
        "V_NAME := 'UPDATE ORDERS SET ID=9;'; -- UPDATE ORDERS SET ID=9;\n"
        "UPDATE TB_ORDER SET ORDER_ID=1; /* ; UPDATE ORDERS SET ID=9; */\n"
        "V_NAME := q'[UPDATE ORDERS SET ID=9; 'quoted']'; UPDATE TB_ORDER SET CUSTOMER_ID=2;",
        2,
    ),
    (
        'IF P_ID>0 THEN UPDATE ORDERS SET ID=P_ID; ELSE UPDATE CUSTOMERS SET ID=P_ID; END IF; '
        'FOR I IN 1..3 LOOP UPDATE ORDERS SET CUST_ID=I; END LOOP;',
        'IF P_ID>0 THEN UPDATE TB_ORDER SET ORDER_ID=P_ID; ELSE UPDATE TB_CUSTOMER SET CUSTOMER_ID=P_ID; END IF; '
        'FOR I IN 1..3 LOOP UPDATE TB_ORDER SET CUSTOMER_ID=I; END LOOP;',
        3,
    ),
    (
        'FORALL I IN 1..IDS.COUNT UPDATE ORDERS SET ID=IDS(I) WHERE CUST_ID=:CUST_ID; '
        'UPDATE CUSTOMERS SET ID=P_ID;',
        'FORALL I IN 1..IDS.COUNT UPDATE TB_ORDER SET ORDER_ID=IDS(I) WHERE CUSTOMER_ID=:CUST_ID; '
        'UPDATE TB_CUSTOMER SET CUSTOMER_ID=P_ID;',
        2,
    ),
    (
        'MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
        'WHEN MATCHED THEN UPDATE SET o.ID=c.ID DELETE WHERE o.ID<0; '
        'UPDATE ORDERS SET ID=1; SELECT ID INTO P_ID FROM ORDERS; UPDATE CUSTOMERS SET ID=2;',
        'MERGE INTO TB_ORDER o USING TB_CUSTOMER c ON(o.CUSTOMER_ID=c.CUSTOMER_ID) '
        'WHEN MATCHED THEN UPDATE SET o.ORDER_ID=c.CUSTOMER_ID DELETE WHERE o.ORDER_ID<0; '
        'UPDATE TB_ORDER SET ORDER_ID=1; SELECT ORDER_ID INTO P_ID FROM TB_ORDER; UPDATE TB_CUSTOMER SET CUSTOMER_ID=2;',
        4,
    ),
    (
        'UPDATE ORDERS SET ID=1; EXCEPTION WHEN NO_DATA_FOUND THEN UPDATE ORDERS SET ID=2; '
        'WHEN OTHERS THEN UPDATE CUSTOMERS SET ID=3; RAISE;',
        'UPDATE TB_ORDER SET ORDER_ID=1; EXCEPTION WHEN NO_DATA_FOUND THEN UPDATE TB_ORDER SET ORDER_ID=2; '
        'WHEN OTHERS THEN UPDATE TB_CUSTOMER SET CUSTOMER_ID=3; RAISE;',
        3,
    ),
])
def test_semicolon_separated_procedure_updates(merge_mapping, body, expected, count):
    prefix = 'CREATE OR REPLACE PROCEDURE P(P_ID IN OUT NUMBER) AS V_NAME VARCHAR2(100); BEGIN\n'
    suffix = '\nEND;\n/\n'
    result = rewrite_plsql(prefix + body + suffix, merge_mapping)
    assert not result.warnings
    assert result.statements == count
    assert result.text == prefix + expected + suffix


def test_updates_in_multiple_package_members(merge_mapping):
    source = '''CREATE OR REPLACE PACKAGE BODY PKG AS
 PROCEDURE P IS BEGIN
  UPDATE ORDERS SET ID=1; UPDATE CUSTOMERS SET ID=2;
 END;
 FUNCTION F RETURN NUMBER IS BEGIN
  UPDATE ORDERS SET CUST_ID=3; UPDATE ORDERS SET ID=4;
  RETURN 1;
 END;
END;
/'''
    result = rewrite_plsql(source, merge_mapping)
    assert not result.warnings
    assert result.statements == 4
    assert result.text == source.replace('UPDATE ORDERS SET ID=', 'UPDATE TB_ORDER SET ORDER_ID=').replace(
        'UPDATE ORDERS SET CUST_ID=', 'UPDATE TB_ORDER SET CUSTOMER_ID=').replace(
        'UPDATE CUSTOMERS SET ID=', 'UPDATE TB_CUSTOMER SET CUSTOMER_ID=')


def test_merge_delete_unqualified_column_uses_updated_target(merge_mapping):
    source = ('MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
              'WHEN MATCHED THEN UPDATE SET ID=c.ID DELETE WHERE ID<0;')
    result = rewrite_plsql(source, merge_mapping)
    assert not result.warnings
    assert 'UPDATE SET ORDER_ID=c.CUSTOMER_ID DELETE WHERE ORDER_ID<0;' in result.text


@pytest.mark.parametrize('fragment', [
    'WHEN NOT MATCHED THEN INSERT(ID) VALUES(c.ID) DELETE WHERE o.ID<0',
    'WHEN MATCHED THEN UPDATE SET ID=c.ID DELETE WHERE',
    'WHEN MATCHED THEN UPDATE SET ID=c.ID DELETE WHERE o.ID<0 DELETE WHERE o.ID=0',
])
def test_invalid_merge_preserves_statement_and_continues_updates(merge_mapping, fragment):
    merge = 'MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) ' + fragment + ';'
    source = 'BEGIN UPDATE ORDERS SET ID=1; ' + merge + ' UPDATE CUSTOMERS SET ID=2; END;'
    result = rewrite_plsql(source, merge_mapping)
    assert result.warnings
    assert merge in result.text
    assert 'UPDATE TB_ORDER SET ORDER_ID=1;' in result.text
    assert 'UPDATE TB_CUSTOMER SET CUSTOMER_ID=2;' in result.text


def test_unsupported_update_does_not_hide_subsequent_updates(merge_mapping):
    unsupported = 'UPDATE ORDERS SET ID=(SELECT ID FROM CUSTOMERS MODEL bad syntax);'
    source = ('CREATE OR REPLACE PROCEDURE P AS BEGIN\n'
              'UPDATE ORDERS SET ID=1;\n' + unsupported + '\n'
              'UPDATE ORDERS SET CUST_ID=2; UPDATE CUSTOMERS SET ID=3;\nEND;\n/')
    result = rewrite_plsql(source, merge_mapping)
    assert result.statements == 4
    assert len(result.warnings) == 1
    assert unsupported in result.text
    assert 'UPDATE TB_ORDER SET ORDER_ID=1;' in result.text
    assert 'UPDATE TB_ORDER SET CUSTOMER_ID=2; UPDATE TB_CUSTOMER SET CUSTOMER_ID=3;' in result.text


def test_merge_delete_nested_query_and_case_conditions(merge_mapping):
    source = ("MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) "
              "WHEN MATCHED THEN UPDATE SET ID=c.ID "
              "DELETE WHERE CASE WHEN o.ID<0 THEN 1 ELSE 0 END=1 "
              "AND EXISTS(SELECT 1 FROM CUSTOMERS x WHERE x.ID=o.CUST_ID AND x.NAME='WHEN; DELETE WHERE') "
              "WHEN NOT MATCHED THEN INSERT(ID) VALUES(c.ID);")
    result = rewrite_plsql(source, merge_mapping)
    assert not result.warnings
    assert 'CASE WHEN o.ORDER_ID<0 THEN 1 ELSE 0 END=1' in result.text
    assert "EXISTS(SELECT 1 FROM TB_CUSTOMER x WHERE x.CUSTOMER_ID=o.CUSTOMER_ID AND x.CUSTOMER_NAME='WHEN; DELETE WHERE')" in result.text
    assert 'WHEN NOT MATCHED THEN INSERT(ORDER_ID) VALUES(c.CUSTOMER_ID);' in result.text
