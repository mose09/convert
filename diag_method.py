"""심층 진단: _extract_method_bodies 가 왜 비어있는지 확인."""
import sys
import re
from oracle_embeddings.legacy_java_parser import (
    parse_all_java, _METHOD_SIG_RE, _strip_comments,
    _extract_class_info, _scan_balanced_braces,
)
from oracle_embeddings.mybatis_parser import _read_file_safe

if len(sys.argv) < 2:
    print("Usage: python diag_method.py <backend_dir>")
    sys.exit(1)

classes = parse_all_java(sys.argv[1])

# First controller with endpoints but empty methods
for c in classes:
    if c.get("stereotype") != "Controller":
        continue
    if not c.get("endpoints"):
        continue
    if c.get("methods"):
        continue  # skip controllers that DO have methods

    fp = c.get("filepath", "")
    print(f"=== {c['class_name']} ({fp}) ===")
    print(f"  endpoints: {len(c.get('endpoints', []))}")
    print(f"  methods: {len(c.get('methods', []))}")

    try:
        raw = _read_file_safe(fp)
        content_nc = _strip_comments(raw)
        class_info = _extract_class_info(content_nc)
        if not class_info:
            print("  >>> class_info is None!")
            break
        print(f"  class_info.start: {class_info.get('start')}")
        print(f"  class_info.header_end: {class_info.get('header_end')}")
        print(f"  content_nc length: {len(content_nc)}")

        header_end = class_info.get("header_end", 0)
        if header_end <= 0:
            print(f"  >>> header_end is {header_end} (invalid)")
            break

        # Check class body
        open_brace = header_end - 1
        if open_brace < len(content_nc) and content_nc[open_brace] == "{":
            print(f"  open brace found at {open_brace}")
        else:
            print(f"  >>> char at header_end-1: '{content_nc[open_brace] if open_brace < len(content_nc) else 'EOF'}'")
            # search forward
            ob2 = content_nc.find("{", header_end - 1)
            print(f"  >>> next '{{' at {ob2}")
            open_brace = ob2

        class_body_end = _scan_balanced_braces(content_nc, open_brace)
        body_text = content_nc[open_brace:class_body_end]
        print(f"  class body length: {len(body_text)}")

        # Try _METHOD_SIG_RE on the body
        sigs = list(_METHOD_SIG_RE.finditer(body_text))
        print(f"  _METHOD_SIG_RE matches in body: {len(sigs)}")
        for s in sigs[:5]:
            print(f"    -> {s.group('name')} at offset {s.start()}")

        if not sigs:
            # Show first 500 chars of body for debugging
            print(f"  body[0:500]:")
            print(f"  {body_text[:500]}")
    except Exception as e:
        print(f"  >>> ERROR: {e}")

    break  # only first problematic controller
