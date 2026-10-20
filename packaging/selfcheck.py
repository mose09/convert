"""Offline checks for an extracted portable distribution (no DB/LLM calls)."""
from __future__ import annotations

import importlib
import importlib.metadata
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
APP = ROOT / 'app'


def main():
    source_only = '--source-only' in sys.argv
    print('Convert portable distribution self-check')
    print('Platform:', platform.system(), platform.machine())
    print('Python:', sys.version.split()[0])
    if not source_only:
        if sys.platform != 'win32' or sys.version_info[:2] != (3, 11):
            raise RuntimeError('Run check.bat with the included Windows Python 3.11 runtime')
        print('Runtime:', sys.executable)
    else:
        print('SOURCE-ONLY: Windows runtime execution is NOT being verified')
    sys.path.insert(0, str(APP))
    os.environ['ANONYMIZED_TELEMETRY'] = 'False'
    modules = ['yaml', 'dotenv', 'openpyxl', 'sqlglot', 'lxml.etree', 'streamlit']
    if not source_only:
        modules += ['oracledb', 'openai', 'pptx', 'tree_sitter',
                    'tree_sitter_javascript', 'tree_sitter_typescript',
                    'chromadb', 'onnxruntime', 'playwright.sync_api']
    for name in modules:
        importlib.import_module(name)
        print('Import OK:', name)
    version = importlib.metadata.version('sqlglot')
    if tuple(int(v) for v in version.split('.')[:3]) < (30, 23, 0):
        raise RuntimeError('sqlglot>=30.23.0 required, found ' + version)
    import yaml
    from oracle_embeddings.migration.mapping_loader import load_mapping
    from oracle_embeddings.migration.plsql_rewriter import rewrite_plsql
    with tempfile.TemporaryDirectory(prefix='convert-check-') as temporary:
        work = Path(temporary)
        inputs = work / 'input'; (inputs / 'nested').mkdir(parents=True)
        mapping = work / 'mapping.yaml'
        mapping.write_text(yaml.safe_dump({'version': '1.0', 'tables': [
            {'type': 'rename', 'as_is': 'ORDERS', 'to_be': 'TB_ORDER'}], 'columns': [
            {'as_is': {'table': 'ORDERS', 'column': 'ID'},
             'to_be': {'table': 'TB_ORDER', 'column': 'ORDER_ID'}}]}), encoding='utf-8')
        source = ('CREATE OR REPLACE PROCEDURE P AS V ORDERS.ID%TYPE;\n'
                  'BEGIN SELECT ID INTO V FROM ORDERS; END;\n/\n')
        sql = inputs / 'nested' / 'object.sql'
        sql.write_text(source, encoding='utf-8')
        (inputs / 'mapper.xml').write_text('<mapper namespace="Check"><select id="find">SELECT ID FROM ORDERS</select></mapper>', encoding='utf-8')
        config = work / 'config.yaml'
        output = work / 'output'
        config.write_text(yaml.safe_dump({'storage': {'output_dir': str(output)}}), encoding='utf-8')
        proc = subprocess.run([sys.executable, '-X', 'utf8', str(APP / 'main.py'), '--config', str(config),
                               'migrate-sql', '--mybatis-dir', str(inputs), '--mapping', str(mapping)],
                              cwd=APP, capture_output=True, text=True, encoding='utf-8',
                              env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'))
        if proc.returncode:
            raise RuntimeError(proc.stdout + '\n' + proc.stderr)
        converted = next(output.glob('migration/*/converted'))
        result = (converted / 'nested' / 'object.sql').read_text(encoding='utf-8')
        assert 'V TB_ORDER.ORDER_ID%TYPE' in result
        assert 'SELECT ORDER_ID INTO V FROM TB_ORDER' in result
        assert 'SELECT ORDER_ID FROM TB_ORDER' in (converted / 'mapper.xml').read_text(encoding='utf-8')
        assert sql.read_text(encoding='utf-8') == source
        assert list(output.glob('migration/*/*.xlsx'))
        warning = rewrite_plsql("BEGIN EXECUTE IMMEDIATE 'SELECT ID FROM ORDERS'; END;", load_mapping(mapping))
        assert warning.warnings and "'SELECT ID FROM ORDERS'" in warning.text
    print('SQL / XML / %TYPE / Excel / original preservation / dynamic SQL warning: OK')
    print('PASS (source only)' if source_only else 'PASS (Windows runtime and offline migration)')
    print('Oracle compilation, DB/LLM access and browser capture were not tested.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('FAIL:', type(exc).__name__, str(exc))
        sys.exit(1)
