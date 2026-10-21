"""Mapper blocks must resolve each SQL statement's identifiers independently."""
import pytest
import yaml
import subprocess
import sys

from lxml import etree

from oracle_embeddings.migration.mapping_loader import load_mapping
from oracle_embeddings.migration.xml_rewriter import rewrite_xml


@pytest.fixture
def mapping(tmp_path):
    path = tmp_path / 'mapping.yaml'
    data = {'version': '1.0', 'tables': [], 'columns': []}
    for table, target, columns in [
        ('ORDERS', 'TB_ORDER', [('ID', 'ORDER_ID'), ('CUST_ID', 'CUSTOMER_ID')]),
        ('CUSTOMERS', 'TB_CUSTOMER', [('ID', 'CUSTOMER_ID'), ('NAME', 'CUSTOMER_NAME')]),
    ]:
        data['tables'].append({'type': 'rename', 'as_is': table, 'to_be': target})
        for column, renamed in columns:
            data['columns'].append({'as_is': {'table': table, 'column': column},
                                    'to_be': {'table': target, 'column': renamed}})
    path.write_text(yaml.safe_dump(data))
    return load_mapping(path)


@pytest.mark.parametrize('wrapper', [('', ''), ('BEGIN\n', '\nEND;')], ids=['sql-list', 'anonymous-block'])
def test_xml_multiple_updates_keep_separate_column_scopes(mapping, tmp_path, wrapper):
    body = (wrapper[0] + "UPDATE ORDERS SET ID=#{id};\n"
            "UPDATE CUSTOMERS SET NAME=#{name} WHERE ID=#{id};" + wrapper[1])
    source = '<mapper namespace="X"><update id="updates"><![CDATA[' + body + ']]></update></mapper>'
    path = tmp_path / 'mapper.xml'
    path.write_text(source)
    result = rewrite_xml(path, mapping)
    assert result.parse_error is None
    assert len(result.results) == 1
    assert result.results[0].status in {'AUTO', 'AUTO_WARN'}
    actual = ''.join(result.tree.getroot().find('update').itertext())
    assert 'UPDATE TB_ORDER SET ORDER_ID=#{id};' in actual
    assert 'UPDATE TB_CUSTOMER SET CUSTOMER_NAME=#{name} WHERE CUSTOMER_ID=#{id};' in actual
    assert actual.startswith(wrapper[0]) and actual.endswith(wrapper[1])
    assert path.read_text() == source


@pytest.mark.parametrize('wrapped', [False, True], ids=['ordinary-merge', 'block-merge'])
def test_xml_merge_target_and_source_same_named_columns(mapping, tmp_path, wrapped):
    body = ('MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID) '
            'WHEN MATCHED THEN UPDATE SET o.ID=c.ID '
            'WHEN NOT MATCHED THEN INSERT(ID,CUST_ID) VALUES(c.ID,c.ID);')
    if wrapped:
        body = 'BEGIN\n' + body + '\nUPDATE ORDERS SET ID=#{id};\nEND;'
    path = tmp_path / 'mapper.xml'
    path.write_text('<mapper namespace="X"><update id="merge"><![CDATA[' + body + ']]></update></mapper>')
    result = rewrite_xml(path, mapping)
    assert result.results[0].status in {'AUTO', 'AUTO_WARN'}
    actual = ''.join(result.tree.getroot().find('update').itertext())
    assert 'UPDATE SET o.ORDER_ID=c.CUSTOMER_ID' in actual
    assert 'INSERT(ORDER_ID,CUSTOMER_ID) VALUES(c.CUSTOMER_ID,c.CUSTOMER_ID)' in actual
    assert 'ON(o.CUSTOMER_ID=c.CUSTOMER_ID)' in actual
    if wrapped:
        assert 'UPDATE TB_ORDER SET ORDER_ID=#{id};' in actual


def test_xml_block_preserves_bound_values_literals_and_sql_comments(mapping, tmp_path):
    body = ("BEGIN\nV := 'ORDERS.ID; CUSTOMERS.ID'; -- ORDERS.ID;\n"
            "UPDATE ORDERS SET ID=#{id}; /* CUSTOMERS.ID; */\n"
            "UPDATE CUSTOMERS SET NAME='ID; ORDERS', ID=#{other};\nEND;")
    path = tmp_path / 'mapper.xml'
    path.write_text('<mapper namespace="X"><update id="comments"><![CDATA[' + body + ']]></update></mapper>')
    result = rewrite_xml(path, mapping)
    assert result.results[0].status in {'AUTO', 'AUTO_WARN'}
    actual = ''.join(result.tree.getroot().find('update').itertext())
    assert "V := 'ORDERS.ID; CUSTOMERS.ID'; -- ORDERS.ID;" in actual
    assert 'UPDATE TB_ORDER SET ORDER_ID=#{id}; /* CUSTOMERS.ID; */' in actual
    assert "UPDATE TB_CUSTOMER SET CUSTOMER_NAME='ID; ORDERS', CUSTOMER_ID=#{other};" in actual


def test_mapper_wide_substitutions_do_not_touch_positioned_block(mapping, tmp_path):
    path = tmp_path / 'mapper.xml'
    path.write_text('''<mapper namespace="X">
<select id="customer">SELECT ID FROM CUSTOMERS</select>
<update id="block"><![CDATA[BEGIN
UPDATE ORDERS SET ID=#{ID,jdbcType=NUMERIC};
UPDATE CUSTOMERS SET NAME=${display_name} WHERE ID=#{customer.id};
END;]]></update>
</mapper>''')
    result = rewrite_xml(path, mapping)
    actual = ''.join(result.tree.getroot().find('update').itertext())
    assert 'UPDATE TB_ORDER SET ORDER_ID=#{ID,jdbcType=NUMERIC};' in actual
    assert 'UPDATE TB_CUSTOMER SET CUSTOMER_NAME=${display_name} WHERE CUSTOMER_ID=#{customer.id};' in actual
    assert 'SELECT CUSTOMER_ID FROM TB_CUSTOMER' in ''.join(result.tree.getroot().find('select').itertext())


def test_xml_unsupported_statement_isolated_from_later_updates(mapping, tmp_path):
    path = tmp_path / 'mapper.xml'
    unsupported = 'UPDATE ORDERS SET ID=(SELECT ID FROM CUSTOMERS MODEL bad syntax);'
    path.write_text('<mapper namespace="X"><update id="block"><![CDATA[BEGIN\n' + unsupported +
                    '\nUPDATE ORDERS SET ID=1; UPDATE CUSTOMERS SET ID=2;\nEND;]]></update>'
                    '<select id="customer">SELECT ID FROM CUSTOMERS</select></mapper>')
    result = rewrite_xml(path, mapping)
    actual = ''.join(result.tree.getroot().find('update').itertext())
    assert result.results[0].status == 'AUTO_WARN'
    assert len(result.results[0].warnings) == 1
    assert unsupported in actual
    assert 'UPDATE TB_ORDER SET ORDER_ID=1; UPDATE TB_CUSTOMER SET CUSTOMER_ID=2;' in actual


def test_xml_merge_delete_where_and_following_update(mapping, tmp_path):
    path = tmp_path / 'mapper.xml'
    path.write_text('''<mapper namespace="X"><update id="block"><![CDATA[BEGIN
MERGE INTO ORDERS o USING CUSTOMERS c ON(o.CUST_ID=c.ID)
WHEN MATCHED THEN UPDATE SET ID=c.ID DELETE WHERE ID<0;
UPDATE CUSTOMERS SET ID=#{id};
END;]]></update></mapper>''')
    result = rewrite_xml(path, mapping)
    actual = ''.join(result.tree.getroot().find('update').itertext())
    assert result.results[0].status == 'AUTO'
    assert 'UPDATE SET ORDER_ID=c.CUSTOMER_ID DELETE WHERE ORDER_ID<0;' in actual
    assert 'UPDATE TB_CUSTOMER SET CUSTOMER_ID=#{id};' in actual


def test_static_xml_comment_does_not_turn_block_into_dynamic_sql(mapping, tmp_path):
    path = tmp_path / 'mapper.xml'
    path.write_text('''<mapper namespace="X"><update id="block"><!-- existing mapper note -->
BEGIN
UPDATE ORDERS SET ID=1;
UPDATE CUSTOMERS SET ID=2;
END;</update></mapper>''')
    result = rewrite_xml(path, mapping)
    assert result.results[0].status in {'AUTO', 'AUTO_WARN'}
    stmt = result.tree.getroot().find('update')
    assert stmt[0].text == ' existing mapper note '
    actual = ''.join(stmt.itertext())
    assert 'UPDATE TB_ORDER SET ORDER_ID=1;' in actual
    assert 'UPDATE TB_CUSTOMER SET CUSTOMER_ID=2;' in actual


def test_cli_format_style_does_not_regenerate_procedural_xml(mapping, tmp_path):
    mapping_path = tmp_path / 'mapping.yaml'
    data = yaml.safe_load(mapping_path.read_text())
    data['options'] = {'output_format': {'style': 'korean_legacy'}}
    mapping_path.write_text(yaml.safe_dump(data))
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    path = inputs / 'mapper.xml'
    body = ('BEGIN\nUPDATE ORDERS SET ID=#{id};\n'
            'UPDATE CUSTOMERS SET ID=#{customerId};\nEND;')
    path.write_text('<mapper namespace="X"><update id="block"><![CDATA[' + body + ']]></update></mapper>')
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'storage': {'output_dir': str(tmp_path / 'output')}}))
    completed = subprocess.run([
        sys.executable, 'main.py', '--config', str(config), 'migrate-sql',
        '--mybatis-dir', str(inputs), '--mapping', str(mapping_path),
        '--to-be-schema-from-mapping',
    ], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    dest = next((tmp_path / 'output').rglob('mapper.xml'))
    tree = etree.parse(str(dest))
    actual = ''.join(tree.getroot().find('update').itertext())
    assert [line.strip() for line in actual.splitlines() if line.strip()] == [
        'BEGIN', 'UPDATE TB_ORDER SET ORDER_ID=#{id};',
        'UPDATE TB_CUSTOMER SET CUSTOMER_ID=#{customerId};', 'END;',
    ]
    assert path.read_text().endswith(body + ']]></update></mapper>')
