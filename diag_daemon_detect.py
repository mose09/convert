#!/usr/bin/env python3
"""사용자 PC 에서 한 번 실행 — daemon entry 인식 진단.

implements Job / Tasklet 같은 패턴이 parse_java_file 에서 잡히는지
확인. 사용자 환경에서 ``--analyze-daemons`` 돌렸는데 일부 종류가
누락된 경우 정확한 원인 좁히기 용도.

usage:
  # 단일 파일 진단 — daemon_entries 채워졌는지
  python diag_daemon_detect.py <java_file>

  # 디렉토리 스캔 — daemon_entries 가진 모든 클래스 1줄씩 emit
  python diag_daemon_detect.py --scan <backend_dir>

CLAUDE.md ⚠ 단방향 환경 — 짧은 결론 1~2줄 emit, 사용자가 수기 타이핑
가능하게 self-classify. 결과 한 줄 알려주시면 어디서 막힌지 확정.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from oracle_embeddings.legacy_java_parser import (
        parse_java_file, parse_all_java, _extract_class_info,
        _strip_comments, _read_file_safe,
    )
except ImportError as e:
    print(f"✗ import 실패 — repo 루트에서 실행하세요: {e}")
    sys.exit(1)


def _classify_single(path: str) -> None:
    try:
        content = _read_file_safe(path)
    except Exception as e:
        print(f"✗ 파일 못 읽음: {e}")
        return

    info = parse_java_file(path)
    if not info:
        print(f"A) ✗ Java class 자체 미인식 — class 선언 패턴 확인 필요")
        return

    fqcn = info.get("fqcn", "")
    implements = info.get("implements") or []
    extends = info.get("extends", "")
    stereotype = info.get("stereotype", "")
    daemons = info.get("daemon_entries") or []

    if daemons:
        kinds = sorted({d.get("daemon_kind", "?") for d in daemons})
        print(f"C) ✓ '{fqcn}' daemon_entries={len(daemons)} kinds={kinds} "
              f"implements={implements}")
        return

    # daemon_entries 0 — 왜 미인식인지 분류
    quartz_hints = []
    src_nc = _strip_comments(content)
    if "implements Job" in src_nc or "implements\tJob" in src_nc:
        quartz_hints.append("text 'implements Job' 존재")
    if "@DisallowConcurrentExecution" in src_nc:
        quartz_hints.append("@DisallowConcurrentExecution 존재")
    if "org.quartz" in src_nc:
        quartz_hints.append("org.quartz import 존재")
    if any(t in src_nc for t in
           ("implements Tasklet", "implements ItemReader",
            "implements ItemProcessor", "implements ItemWriter")):
        quartz_hints.append("Spring Batch interface text 존재")

    if quartz_hints:
        print(f"B) ⚠ '{fqcn}' daemon_entries=0 but daemon 단서 있음 — "
              f"_extract_class_info 의 implements 파싱 누락. "
              f"단서: {quartz_hints[:3]}")
        print(f"   class_info.implements={implements}, extends={extends!r}")
    else:
        print(f"D) — '{fqcn}' daemon 단서 없음 (정상 — 데몬 아님). "
              f"stereotype={stereotype!r}, implements={implements}")


def _scan_dir(backend_dir: str) -> None:
    if not os.path.isdir(backend_dir):
        print(f"✗ 디렉토리 없음: {backend_dir}")
        return
    classes = parse_all_java(backend_dir)
    daemon_classes = [c for c in classes if c.get("daemon_entries")]
    if not daemon_classes:
        # 단서 있는 클래스도 함께 emit
        suspects = []
        for c in classes:
            impls = c.get("implements") or []
            ext = c.get("extends") or ""
            if any(t in ("Job", "Tasklet", "ItemReader", "ItemProcessor",
                         "ItemWriter", "ItemStreamReader", "ItemStreamWriter")
                   for t in impls):
                suspects.append((c.get("fqcn", ""), impls))
            elif ext in ("QuartzJobBean", "QuartzJob"):
                suspects.append((c.get("fqcn", ""), [ext]))
        if suspects:
            print(f"⚠ scan {len(classes)} classes — daemon_entries=0 but "
                  f"단서 있는 클래스 {len(suspects)}건 (첫 3개):")
            for fqcn, hint in suspects[:3]:
                print(f"   {fqcn}: {hint}")
        else:
            print(f"— scan {len(classes)} classes — daemon entry / "
                  f"단서 0건 (정상 — 데몬 없음)")
        return
    by_kind: dict[str, int] = {}
    for c in daemon_classes:
        for d in c.get("daemon_entries") or []:
            k = d.get("daemon_kind", "?")
            by_kind[k] = by_kind.get(k, 0) + 1
    print(f"✓ scan {len(classes)} classes — daemon classes={len(daemon_classes)} "
          f"by kind={dict(sorted(by_kind.items()))}")
    for c in daemon_classes[:10]:
        kinds = [d.get("daemon_kind", "?") for d in c.get("daemon_entries") or []]
        print(f"   {c.get('fqcn', '')}: {kinds}")
    if len(daemon_classes) > 10:
        print(f"   ... (+{len(daemon_classes) - 10})")


def main():
    args = sys.argv[1:]
    if not args:
        print("usage:")
        print("  python diag_daemon_detect.py <java_file>")
        print("  python diag_daemon_detect.py --scan <backend_dir>")
        sys.exit(2)
    if args[0] == "--scan":
        if len(args) < 2:
            print("usage: python diag_daemon_detect.py --scan <backend_dir>")
            sys.exit(2)
        _scan_dir(args[1])
    else:
        _classify_single(args[0])


if __name__ == "__main__":
    main()
