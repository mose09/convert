"""Parser tolerance must preserve timestamp spelling and warn about Oracle validity."""
from pathlib import Path
import re

import pytest
import yaml

from oracle_embeddings.migration.mapping_loader import load_mapping
from oracle_embeddings.migration.plsql_rewriter import rewrite_plsql


@pytest.fixture
def mapping(tmp_path):
    data = {'version': '1.0', 'tables': [], 'columns': []}
    for table, prefix in [('TABLE1', 'T1'), ('TABLE2', 'T2'), ('TABL2', 'M2'),
                          ('TALB2', 'S2'), ('TALB3', 'S3')]:
        target = 'NEW_' + table
        data['tables'].append({'type': 'rename', 'as_is': table, 'to_be': target})
        for column in ['COL1', 'COL2', 'COL3', 'COL4', 'COLUMN1', 'BBB', 'CCC', 'USR_ID']:
            data['columns'].append({
                'as_is': {'table': table, 'column': column},
                'to_be': {'table': target, 'column': prefix + '_' + column},
            })
    path = tmp_path / 'mapping.yaml'
    path.write_text(yaml.safe_dump(data))
    return load_mapping(path)


@pytest.mark.parametrize('expression', [
    'SYSTIMESTAMP(0)', 'SYSTIMESTAMP(3)', 'SYSTIMESTAMP(6)', 'SYSTIMESTAMP(9)',
    'systimestamp ( 0 )', 'SyStImEsTaMp\n(6)', 'SYSTIMESTAMP /* keep */ (0)',
    'SYSTIMESTAMP -- keep\n (0)',
])
def test_timestamp_precision_does_not_block_identifier_conversion(mapping, expression):
    source = 'UPDATE TABLE2 SET COL1=' + expression + ' WHERE COL2=:COL2;'
    result = rewrite_plsql(source, mapping)
    assert result.text == 'UPDATE NEW_TABLE2 SET T2_COL1=' + expression + ' WHERE T2_COL2=:COL2;'
    assert result.statements == 1
    assert result.changes
    assert any('SYSTIMESTAMP' in warning.upper() for warning in result.warnings)


@pytest.mark.parametrize('expression', [
    'PKG.SYSTIMESTAMP(0)', '"SYSTIMESTAMP"(0)', '"PKG"."SYSTIMESTAMP"(0)',
])
def test_qualified_and_quoted_functions_are_not_timestamp_tolerance_targets(mapping, expression):
    source = 'UPDATE TABLE2 SET COL1=' + expression + ' WHERE COL2=:COL2;'
    result = rewrite_plsql(source, mapping)
    assert result.text == 'UPDATE NEW_TABLE2 SET T2_COL1=' + expression + ' WHERE T2_COL2=:COL2;'
    assert not result.warnings


@pytest.mark.parametrize('expression', ['SYSTIMESTAMP(10)', 'SYSTIMESTAMP(V_PRECISION)',
                                         'SYSTIMESTAMP(-1)', 'SYSTIMESTAMP(0,1)'])
def test_other_timestamp_arguments_remain_conservative(mapping, expression):
    source = 'UPDATE TABLE2 SET COL1=' + expression + ';'
    result = rewrite_plsql(source, mapping)
    assert result.text == source
    assert result.warnings
    assert not result.changes


def test_real_parse_error_not_hidden_by_timestamp_tolerance(mapping):
    invalid = 'UPDATE TABLE2 SET COL1=SYSTIMESTAMP(0) WHERE COL2=;'
    result = rewrite_plsql('BEGIN ' + invalid + ' UPDATE TABLE1 SET COL1=1; END;', mapping)
    assert invalid in result.text
    assert 'UPDATE NEW_TABLE1 SET T1_COL1=1;' in result.text
    assert result.warnings
    assert any('preserved' in warning.lower() for warning in result.warnings)


def test_timestamp_names_in_literals_and_comments_do_not_trigger_tolerance(mapping):
    source = ("BEGIN V := q'[SYSTIMESTAMP(0); UPDATE TABLE2 SET COL1=1;]';\n"
              "-- SYSTIMESTAMP(0)\nUPDATE TABLE2 SET COL1='SYSTIMESTAMP(0)'; END;")
    result = rewrite_plsql(source, mapping)
    assert result.text == source.replace("UPDATE TABLE2 SET COL1='", "UPDATE NEW_TABLE2 SET T2_COL1='")
    assert not result.warnings


def test_multiple_timestamp_calls_keep_offsets_in_select(mapping):
    source = ('SELECT SYSTIMESTAMP(0), COL1, SYSTIMESTAMP(6) INTO V1,V2,V3 '
              'FROM TABLE2 WHERE COL2=:COL2;')
    result = rewrite_plsql(source, mapping)
    assert result.text == ('SELECT SYSTIMESTAMP(0), T2_COL1, SYSTIMESTAMP(6) INTO V1,V2,V3 '
                           'FROM NEW_TABLE2 WHERE T2_COL2=:COL2;')
    assert len(result.warnings) == 2


def test_timestamp_call_in_merge_preserves_target_source_scopes(mapping):
    source = ('MERGE INTO TABLE1 A USING TABLE2 B ON(A.COL1=B.COL1) '
              'WHEN MATCHED THEN UPDATE SET A.COL2=SYSTIMESTAMP(0) '
              'WHEN NOT MATCHED THEN INSERT(COL1,COL2) VALUES(B.COL1,SYSTIMESTAMP(6));')
    result = rewrite_plsql(source, mapping)
    assert result.text == ('MERGE INTO NEW_TABLE1 A USING NEW_TABLE2 B ON(A.T1_COL1=B.T2_COL1) '
                           'WHEN MATCHED THEN UPDATE SET A.T1_COL2=SYSTIMESTAMP(0) '
                           'WHEN NOT MATCHED THEN INSERT(T1_COL1,T1_COL2) VALUES(B.T2_COL1,SYSTIMESTAMP(6));')
    assert len(result.warnings) == 2


def test_screenshot_procedure_all_eleven_regions_convert(mapping):
    source = (Path(__file__).parent / 'fixtures' / 'screenshot_procedure.sql').read_text()
    result = rewrite_plsql(source, mapping)
    assert result.statements == 11
    assert len(re.findall(r'MERGE INTO NEW_', result.text)) == 3
    assert len(re.findall(r'UPDATE NEW_', result.text)) == 6
    assert not re.search(r'\b(?:FROM|UPDATE|INTO) (?:TABLE1|TABLE2|TABL2|TALB2|TALB3)\b', result.text)
    assert re.findall(r'C_1\.\w+', result.text, re.I) == re.findall(r'C_1\.\w+', source, re.I)
    assert re.findall(r"'(?:''|[^'])*'", result.text) == re.findall(r"'(?:''|[^'])*'", source)
    assert 'SELECT T2_COL1 AS COL1, T2_COL2 AS COL2, T2_COL3 AS COL3, T2_COL4 AS COL4' in result.text
    assert 'V_BBB NEW_TABLE1.T1_COLUMN1%TYPE;' in result.text
    assert "SET T2_COL1 = 'BBB', T2_COL2 = SYSTIMESTAMP(0), T2_COL3 = 'PGR IS NULL'" in result.text
    assert result.text.count('SELECT A.S2_COL1, C_1.COL1, C_1.COL2, A.S2_COL3,') == 2
    assert result.text.count('ON (A.M2_COL1 = C_1.COL1 AND A.M2_COL2 = C_1.COL2)') == 2
    assert result.text.count('VALUES (,C_1.COL1, C_1.COL2);') == 3
    assert result.text.count('COMMIT;') == source.count('COMMIT;')
    assert result.text.endswith('EXCEPTION\n WHEN OTHERS THEN RAISE;\nEND;\n/\n')
    assert result.warnings
    assert any('SYSTIMESTAMP' in warning.upper() for warning in result.warnings)
