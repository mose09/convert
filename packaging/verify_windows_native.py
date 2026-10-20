"""Audit bundled C++ imports/exports after verify_windows_portable.py. Requires pefile."""
import json
import sys
from pathlib import Path
import pefile
root=Path(sys.argv[1]);python=root/'python'
static=json.loads((root/'verification/dependency-audit.json').read_text())
skip=set(static['inactive_pip_launcher_templates'])
crt={p.name.lower():p for p in python.glob('*.dll') if p.name.lower().startswith(('msvcp','vcruntime','concrt','vcomp','vccorlib'))}
exports={}
for name,path in crt.items():
 pe=pefile.PE(str(path),fast_load=True);pe.parse_data_directories(directories=[0])
 symbols=getattr(pe,'DIRECTORY_ENTRY_EXPORT',None)
 exports[name]={s.name for s in symbols.symbols if s.name} if symbols else set()
issues=[];checked=0;unbundled={};provided={p.name.lower() for p in python.rglob('*') if p.is_file() and p.suffix.lower() in {'.pyd','.dll','.exe'}}
for path in python.rglob('*'):
 if not path.is_file() or path.suffix.lower() not in {'.pyd','.dll','.exe'}:continue
 if path.relative_to(root).as_posix() in skip:continue
 pe=pefile.PE(str(path),fast_load=True);pe.parse_data_directories(directories=[1,13])
 for entry in list(getattr(pe,'DIRECTORY_ENTRY_IMPORT',[]))+list(getattr(pe,'DIRECTORY_ENTRY_DELAY_IMPORT',[])):
  name=entry.dll.decode().lower()
  if name not in provided:unbundled.setdefault(name,[]).append(path.relative_to(root).as_posix())
  if name not in exports:continue
  for symbol in entry.imports:
   if symbol.name:
    checked+=1
    if symbol.name not in exports[name]:issues.append(f'{path.name} -> {name}: {symbol.name.decode()} not exported')
report={'msvc_import_symbols_checked':checked,'missing_msvc_symbols':issues,'unbundled_os_dll_imports':sorted(unbundled),'windows_execution_verified':False,'excluded_inactive_installer_templates':sorted(skip)}
(root/'verification/native-import-audit.json').write_text(json.dumps(report,indent=2)+'\n')
print('C++ runtime import symbols checked:',checked,'missing:',len(issues))
assert not issues,issues
