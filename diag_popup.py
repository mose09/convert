"""diag_popup.py — popup 식별 진단.

사용법::

    python diag_popup.py <frontend_dir>

main 으로 분류된 React 파일 중 popup 의심 (return 안 ``<Modal>`` 또는
``<Modal visible={props.X}>`` 또는 컴포넌트 이름이 popup 키워드 포함)
을 자동 분류해서 1-2줄 결론 emit.

자동 판정 결과:
  ✓ N개 popup 정확 식별
  ⚠ M개 main 으로 잡혔는데 popup 의심 (파일명 + 매칭된 신호)

자세한 dump 는 옵트인 ``--verbose``.
"""

import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from oracle_embeddings.legacy_react_api_scanner import (
    _collect_main_entries, _collect_self_modal_popup_files,
    _collect_popup_imports_per_main, _is_apps_react_file,
    _SELF_POPUP_RENDER_RE, _SELF_POPUP_FC_ARROW_RE,
    _SELF_POPUP_VISIBLE_FROM_PROPS_RE,
    _POPUP_COMPONENT_NAME_RE, _is_self_popup_file,
    _strip_comments,
)


def _read(fp):
    try:
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def diag(frontend_dir: str, verbose: bool = False) -> int:
    if not os.path.isdir(frontend_dir):
        print(f"✗ {frontend_dir} not a directory")
        return 1

    all_files = []
    for r, _, fs in os.walk(frontend_dir):
        for f in fs:
            if f.endswith((".js", ".jsx", ".ts", ".tsx")):
                all_files.append(os.path.join(r, f))

    rels = [os.path.relpath(f, frontend_dir).replace("\\", "/") for f in all_files]
    apps_react = [r for r in rels if _is_apps_react_file(r)]
    # apps/ 안 .js 파일이지만 _is_apps_react_file 인식 X — 단순 .jsx
    # 또는 components 폴더 안. popup 누락 가능성 큰 후보.
    apps_unrecognized = [
        r for r in rels
        if ("/apps/" in "/" + r or r.startswith("apps/"))
        and r not in apps_react
    ]
    main_set = _collect_main_entries(all_files, frontend_dir)
    self_popups = _collect_self_modal_popup_files(all_files, frontend_dir)
    import_popups = _collect_popup_imports_per_main(all_files, frontend_dir, main_set)
    total_popups = self_popups | import_popups

    # main 으로 분류된 파일 중 popup 신호 있는 것
    suspects = []
    for rel in sorted(main_set - total_popups):
        fp = os.path.join(frontend_dir, rel)
        content = _strip_comments(_read(fp))
        if not content:
            continue
        signals = []
        if _SELF_POPUP_RENDER_RE.search(content):
            signals.append("return-Modal")
        if _SELF_POPUP_FC_ARROW_RE.search(content):
            signals.append("FC-arrow-Modal")
        if _SELF_POPUP_VISIBLE_FROM_PROPS_RE.search(content):
            signals.append("visible={props.X}")
        if _POPUP_COMPONENT_NAME_RE.search(content):
            signals.append("popup-name")
        # 파일 내 어디든 <Modal title=> 만 존재 (top-level 아님) — weak signal
        if not signals:
            import re
            if re.search(r"<(?:Modal|Dialog|Drawer|Popup|Sheet|Layer)\w*\b", content):
                signals.append("has-modal-tag (weak)")
        if signals:
            suspects.append((rel, signals))

    # 결론
    print(f"apps React 파일 (인식): {len(apps_react)}")
    print(f"  main:  {len(main_set)}")
    print(f"  popup: {len(total_popups)} (self-modal={len(self_popups)}, "
          f"main-import={len(import_popups)})")
    if apps_unrecognized:
        # apps/ 안 인식 안 된 파일 — popup 누락 가능성 큰 후보
        print(f"apps/ 안 인식 안 됨: {len(apps_unrecognized)}개 "
              f"(_is_apps_react_file 가 index.* / popup-folder 만 인정)")
        if verbose or len(apps_unrecognized) <= 10:
            for r in apps_unrecognized[:10]:
                print(f"  {r}")
            if len(apps_unrecognized) > 10:
                print(f"  ... +{len(apps_unrecognized) - 10}개 더")
    print()
    if not suspects:
        if apps_unrecognized:
            print(f"⚠ 위 {len(apps_unrecognized)}개 파일이 popup 후보일 수 있음 "
                  f"— --verbose 로 위치 확인")
        else:
            print(f"✓ popup 누락 없음 ({len(total_popups)}개 정확 식별)")
        return 0
    print(f"⚠ {len(suspects)}개 main 으로 잡혔는데 popup 의심:")
    for rel, sigs in suspects[:20]:
        print(f"  {rel}  ← 신호: {', '.join(sigs)}")
    if len(suspects) > 20:
        print(f"  ... +{len(suspects) - 20}개 더")
    print()
    print("→ 'return-Modal' / 'visible={props.X}' / 'popup-name' 신호가 있는데도")
    print("  popup 으로 분류 안 됐다면 _is_self_popup_file 보강 필요.")
    print("→ 'has-modal-tag (weak)' 만 있으면 main 의 nested popup container")
    print("  (정상 — main 이 popup 자식 가짐).")

    if verbose:
        print()
        print("=== verbose: 각 의심 파일의 render top ===")
        import re
        for rel, _ in suspects:
            fp = os.path.join(frontend_dir, rel)
            content = _read(fp)
            m = re.search(r"\brender\s*\(\)\s*\{[^}]*?return\s+([^;]{0,200})",
                          content, re.DOTALL)
            snip = m.group(1)[:120].replace("\n", " ") if m else "<no render>"
            print(f"  {rel}")
            print(f"    render: {snip}")

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("frontend_dir")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    sys.exit(diag(args.frontend_dir, args.verbose))
