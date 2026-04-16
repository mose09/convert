"""Nexcore 진단: endpoint 0인 Controller 의 extends + 메서드 시그니처 출력."""
import sys
import re
from oracle_embeddings.legacy_java_parser import parse_all_java, _is_nexcore_controller, _NEXCORE_METHOD_RE, _NEXCORE_PARAM_TYPES
from oracle_embeddings.mybatis_parser import _read_file_safe

if len(sys.argv) < 2:
    print("Usage: python diag_nexcore.py <backend_dir>")
    sys.exit(1)

classes = parse_all_java(sys.argv[1])
controllers = [c for c in classes if c.get("stereotype") == "Controller"]
no_ep = [c for c in controllers if not c.get("endpoints")]
has_ep = [c for c in controllers if c.get("endpoints")]

print(f"Controllers: {len(controllers)} total, {len(has_ep)} with endpoints, {len(no_ep)} without")
print(f"NEXCORE_PARAM_TYPES: {_NEXCORE_PARAM_TYPES}")
print()

# Show first 3 controllers without endpoints in detail
for c in no_ep[:3]:
    print(f"--- {c['class_name']} ---")
    print(f"  extends: {c.get('extends', '')}")
    print(f"  is_nexcore: {_is_nexcore_controller(c)}")
    # Read raw file and show public methods
    try:
        raw = _read_file_safe(c.get("filepath", ""), limit=10000)
        # Find public methods
        pub_methods = re.findall(
            r'public\s+\w[\w.<>,\[\]\s]*?\s+(\w+)\s*\(([^)]*)\)',
            raw
        )
        print(f"  public methods ({len(pub_methods)}):")
        for name, params in pub_methods[:8]:
            print(f"    {name}({params[:80]})")
    except Exception as e:
        print(f"  (cannot read file: {e})")
    print()
