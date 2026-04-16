"""Nexcore base class 진단: Controller 인데 endpoint 0 인 클래스의 extends 값 출력."""
import sys
from oracle_embeddings.legacy_java_parser import parse_all_java

if len(sys.argv) < 2:
    print("Usage: python diag_nexcore.py <backend_dir>")
    sys.exit(1)

classes = parse_all_java(sys.argv[1])
count = 0
for c in classes:
    if c.get("stereotype") == "Controller" and not c.get("endpoints"):
        print(f'{c["class_name"]:40s} extends={c.get("extends", "")}')
        count += 1
        if count >= 30:
            print(f"... ({sum(1 for x in classes if x.get('stereotype')=='Controller' and not x.get('endpoints'))} total)")
            break

if count == 0:
    print("No controllers with 0 endpoints found.")
    # Show all controllers for debugging
    for c in classes:
        if c.get("stereotype") == "Controller":
            eps = len(c.get("endpoints") or [])
            print(f'{c["class_name"]:40s} extends={c.get("extends", "")}  endpoints={eps}')
