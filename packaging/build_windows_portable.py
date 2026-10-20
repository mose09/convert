"""Assemble the existing python/app/run.bat layout on a non-Windows host.

Prepare site packages with uv's Windows target, never with Linux pip install:
  uv pip install --target build/windows-portable/site-packages \
    --python-platform x86_64-pc-windows-msvc --python-version 3.11 \
    --only-binary :all: -r requirements.txt pip setuptools wheel

Download the PSF python 3.11.9 NuGet archive from api.nuget.org, verify it
with `dotnet nuget verify --all`, then run this script. The official archive
hash is pinned below. This assembles Windows files; it does not run Windows.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SHA256 = '9283876d58c017e0e846f95b490da3bca0fc0a6ee1134b2870677cfb7eec3c67'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy_tree(source, destination):
    shutil.copytree(source, destination, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))


def assemble(runtime, site, destination, signature_log):
    if destination.exists():
        raise ValueError(f'Refusing to overwrite {destination}')
    if digest(runtime) != RUNTIME_SHA256:
        raise ValueError('Python NuGet archive hash does not match the official 3.11.9 artifact')
    signature = signature_log.read_text()
    if "Successfully verified package 'python.3.11.9'" not in signature:
        raise ValueError('Successful NuGet signature verification log is required')
    destination.mkdir(parents=True)
    python = destination / 'python'
    with zipfile.ZipFile(runtime) as archive:
        for member in archive.infolist():
            if not member.filename.startswith('tools/') or member.is_dir():
                continue
            relative = Path(member.filename).relative_to('tools')
            if relative.parts[0] in {'Tools', 'include', 'libs'}:
                continue  # Development headers/tools are not needed in a portable runtime.
            target = python / relative
            if target.resolve().is_relative_to(python.resolve()) is False:
                raise ValueError('Invalid archive path')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
    packages = python / 'Lib' / 'site-packages'
    # The official NuGet runtime includes bootstrap pip/setuptools. Replace
    # that directory as a whole so old .dist-info versions cannot survive.
    if packages.exists():
        shutil.rmtree(packages)
    copy_tree(site, packages)
    # uv runs on Linux and generates Unix convenience entrypoints. The app
    # uses python -m, not those scripts. Do not ship unusable Unix launchers.
    shutil.rmtree(packages / 'bin', ignore_errors=True)
    # msvc-runtime is a Windows-only dependency. DLLs at the executable root
    # are available before importing native Chroma / ONNX extensions.
    for parent in (packages, packages / 'Scripts'):
        for dll in parent.glob('*.dll'):
            shutil.copy2(dll, python / dll.name)
    (python / 'python311._pth').write_bytes(
        b'Lib\r\nDLLs\r\n.\r\nLib\\site-packages\r\n..\\app\r\nimport site\r\n')
    app = destination / 'app';app.mkdir()
    for name in ('webui', 'oracle_embeddings'):
        copy_tree(ROOT / name, app / name)
    for name in ('main.py', 'requirements.txt', 'config.yaml', '.env.example', 'README.md'):
        shutil.copy2(ROOT / name, app / name)
    (app / 'input').mkdir()
    for source in (ROOT / 'input').glob('*_template.*'):
        shutil.copy2(source, app / 'input' / source.name)
    (app / 'docs' / 'migration').mkdir(parents=True)
    for source in (ROOT / 'docs' / 'migration').glob('*.md'):
        shutil.copy2(source, app / 'docs' / 'migration' / source.name)
    for name in ('run.bat', 'check.bat', 'selfcheck.py', '사용법.txt'):
        shutil.copy2(ROOT / 'packaging' / name, destination / name)
        if name.endswith('.bat'):
            target = destination / name
            target.write_bytes(target.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    copy_tree(app / 'input', destination / 'input')
    copy_tree(app / 'docs', destination / 'docs')
    shutil.copy2(ROOT / 'webui' / 'assets' / 'guide.html', destination / 'guide.html')
    shutil.copy2(ROOT / 'user_manual.html', destination / 'user_manual.html')
    (destination / 'verification').mkdir()
    shutil.copy2(signature_log, destination / 'verification' / 'python-signature.txt')
    packages_info = {d.metadata['Name']: d.version for d in importlib.metadata.distributions(path=[str(packages)])}
    (destination / 'requirements-windows.lock.txt').write_text(
        '\n'.join(f'{name}=={version}' for name,version in sorted(packages_info.items(),key=lambda x:x[0].lower()))+'\n', encoding='utf-8')
    info = {'python_version': '3.11.9', 'platform': 'Windows x64',
            'runtime_source': 'https://api.nuget.org/v3-flatcontainer/python/3.11.9/python.3.11.9.nupkg',
            'runtime_sha256': RUNTIME_SHA256, 'runtime_variant': 'PSF NuGet runtime (same Python version, same portable layout)',
            'source_commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'source_has_uncommitted_changes': bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True)),
            'windows_execution_verified': False, 'packages': dict(sorted(packages_info.items())),
            'signature_revocation_lookup': 'NuGet default online verification passed with unreachable CRL server warnings',
            'excluded': ['real .env', 'credentials', 'user input/output', '.git', 'Linux entrypoints', 'development virtualenv'],
            'not_bundled': ['Oracle Instant Client', 'LLM/embedding model weights', 'Playwright browser binaries']}
    (destination / 'build-info.json').write_text(json.dumps(info,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    return info


def manifest_and_zip(destination):
    files=sorted(p for p in destination.rglob('*') if p.is_file() and p.name != 'SHA256SUMS.txt')
    manifest='\n'.join(f'{digest(p)}  {p.relative_to(destination).as_posix()}' for p in files)+'\n'
    (destination / 'SHA256SUMS.txt').write_text(manifest,encoding='utf-8')
    output=destination.with_suffix('.zip')
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                archive.write(path,Path('convert-tool')/path.relative_to(destination))
    output.with_suffix('.zip.sha256').write_text(f'{digest(output)}  {output.name}\n',encoding='ascii')
    return output


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=ROOT/'build/windows-portable/python.3.11.9.nupkg')
    parser.add_argument('--site-packages',type=Path,default=ROOT/'build/windows-portable/site-packages')
    parser.add_argument('--signature-log',type=Path,default=ROOT/'build/windows-portable/runtime-signature-verification-online.log')
    parser.add_argument('--output',type=Path,default=ROOT/'dist/convert-tool')
    parser.add_argument('--assemble-only',action='store_true',help='Allow review before creating the ZIP')
    args=parser.parse_args()
    info=assemble(args.runtime,args.site_packages,args.output,args.signature_log)
    print(f'Assembled {args.output}: {len(info["packages"])} Windows distributions')
    if not args.assemble_only:
        print(manifest_and_zip(args.output))
