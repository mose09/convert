"""Nexcore method-scope 진단: endpoint 의 method body + field calls + sql calls 확인."""
import sys
from oracle_embeddings.legacy_java_parser import parse_all_java

if len(sys.argv) < 2:
    print("Usage: python diag_method.py <backend_dir>")
    sys.exit(1)

classes = parse_all_java(sys.argv[1])

# Show first 3 controllers with endpoints
count = 0
for c in classes:
    if c.get("stereotype") != "Controller":
        continue
    eps = c.get("endpoints") or []
    if not eps:
        continue
    methods = c.get("methods") or []
    print(f"=== {c['class_name']} ({len(eps)} endpoints, {len(methods)} methods) ===")
    print(f"  autowired: {[f['name'] + ':' + f['type_simple'] for f in c.get('autowired_fields', [])[:5]]}")
    for ep in eps[:3]:
        print(f"  EP: {ep['method_name']} [{ep['annotation']}]")
        # Find matching method
        matched = [m for m in methods if m.get("name") == ep["method_name"]]
        if matched:
            m = matched[0]
            print(f"    method body length: {len(m.get('body', ''))}")
            print(f"    body_field_calls: {m.get('body_field_calls', [])}")
            print(f"    body_sql_calls: {m.get('body_sql_calls', [])}")
            print(f"    body_rfc_calls: {m.get('body_rfc_calls', [])}")
        else:
            print(f"    >>> NO MATCHING METHOD FOUND in methods list")
            print(f"    available methods: {[m['name'] for m in methods[:10]]}")
    print()
    count += 1
    if count >= 3:
        break
