"""Static review of Windows portable files; does not execute Windows binaries."""
from __future__ import annotations

import importlib.metadata
from collections import Counter
import json
from pathlib import Path
import struct
import sys

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import compatible_tags, cpython_tags, parse_tag


def audit(root):
    packages = root / 'python' / 'Lib' / 'site-packages'
    normalize=lambda s:s.lower().replace('_','-')
    found=list(importlib.metadata.distributions(path=[str(packages)]))
    duplicates=[name for name,count in Counter(normalize(d.metadata['Name']) for d in found).items() if count>1]
    distributions={normalize(d.metadata['Name']):d for d in found}
    tags=set(cpython_tags((3,11),['cp311'],['win_amd64'])) | set(compatible_tags((3,11),'cp311',['win_amd64']))
    issues=['Duplicate distribution: '+name for name in duplicates]
    for name,distribution in distributions.items():
        spec=distribution.metadata.get('Requires-Python')
        if spec and not SpecifierSet(spec).contains('3.11.9'):
            issues.append(f'{name}: incompatible Python {spec}')
        wheel_tags=set()
        for line in (distribution.read_text('WHEEL') or '').splitlines():
            if line.startswith('Tag: '):wheel_tags |= parse_tag(line[5:])
        if wheel_tags and not wheel_tags.intersection(tags):issues.append(f'{name}: incompatible wheel tags')
    environment=default_environment()
    environment.update(os_name='nt',sys_platform='win32',platform_system='Windows',platform_machine='AMD64',python_version='3.11',python_full_version='3.11.9',implementation_name='cpython',platform_python_implementation='CPython')
    extras={name:{''} for name in distributions}
    changed=True
    while changed:
        changed=False
        for name,distribution in distributions.items():
            for raw in distribution.requires or []:
                requirement=Requirement(raw)
                if requirement.marker and not any(requirement.marker.evaluate(dict(environment,extra=extra)) for extra in list(extras[name])):
                    continue
                key=normalize(requirement.name)
                if key not in distributions:
                    issues.append(f'{name}: missing {requirement}')
                    continue
                if requirement.specifier and not requirement.specifier.contains(distributions[key].version):
                    issues.append(f'{name}: incompatible {requirement}')
                for extra in requirement.extras:
                    if extra not in extras[key]:extras[key].add(extra);changed=True
    templates=[]
    binaries=[]
    allowed_templates={'t32.exe','w32.exe','t64-arm.exe','w64-arm.exe'}
    for path in root.rglob('*'):
        if not path.is_file():continue
        if path.suffix.lower()=='.so':issues.append(f'Linux library: {path.relative_to(root)}')
        if path.suffix.lower() not in {'.dll','.pyd','.exe'}:continue
        relative=path.relative_to(root).as_posix()
        with path.open('rb') as stream:
            header=stream.read(64)
            if header[:2]!=b'MZ':issues.append(f'Not PE: {relative}');continue
            offset=struct.unpack_from('<I',header,60)[0]
            stream.seek(offset);pe=stream.read(6)
        machine=struct.unpack_from('<H',pe,4)[0]
        if (('/pip/_vendor/distlib/' in relative and path.name in allowed_templates)
            or '/site-packages/setuptools/' in relative and path.name in {'cli-32.exe','cli-arm64.exe','cli.exe','gui-32.exe','gui-arm64.exe','gui.exe'}):
            templates.append(relative);continue
        if pe[:4]!=b'PE\0\0' or machine!=0x8664:issues.append(f'Not x64: {relative}')
        binaries.append(relative)
    for expected in ('python.exe','python311.dll','msvcp140.dll','msvcp140_1.dll','vcruntime140.dll','vcruntime140_1.dll'):
        if not (root/'python'/expected).is_file():issues.append('Missing runtime '+expected)
    pth=(root/'python'/'python311._pth').read_text().splitlines()
    if not {'Lib','DLLs','Lib\\site-packages','..\\app','import site'}.issubset(set(pth)):issues.append('Invalid isolated Python search paths')
    if (packages/'bin').exists():issues.append('Unix entrypoints must not be shipped')
    forbidden=[p.relative_to(root).as_posix() for p in root.rglob('*') if p.name=='.env' or p.name=='.git' or p.name=='.venv']
    issues += ['Unexpected private/local file: '+p for p in forbidden]
    report={'target':'Windows x64 CPython 3.11.9','distribution_count':len(distributions),
            'native_binary_count':len(binaries),'inactive_pip_launcher_templates':templates,
            'issues':sorted(set(issues)), 'windows_execution_verified':False,
            'package_versions':{name:d.version for name,d in sorted(distributions.items())}}
    return report


if __name__=='__main__':
    root=Path(sys.argv[1])
    report=audit(root)
    target=root/'verification'/'dependency-audit.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print({key:value for key,value in report.items() if key not in {'package_versions','inactive_pip_launcher_templates'}})
    if report['issues']:sys.exit(1)
