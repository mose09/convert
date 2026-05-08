"""
verify.py — Screen Closure 검증 스크립트 (standalone).

사용법:
    python verify.py <repo_root> <entry_file>
    python verify.py <repo_root> <entry_file> --patterns patterns.yaml
    python verify.py <repo_root> <entry_file> --output ./closures

옵션:
    --max-depth     BFS 깊이 (기본 3)
    --token-budget  토큰 상한 (기본 12000)
    --output        화면/팝업 closure 를 .md 파일로 저장할 디렉토리

동작:
    1. <entry_file> 을 진입점으로 closure 빌드
    2. closure 안의 popup_refs 를 순회하며 같은 함수로 팝업 closure 도 빌드
    3. 결과 요약 + (--output 시) Markdown 파일 저장
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from legacy_react_closure import build_closure, serialize_for_llm


def _load_patterns(path):
    if not path or not path.exists():
        return None
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding='utf-8'))
    except ImportError:
        print("[warn] pyyaml not installed, ignoring --patterns", file=sys.stderr)
        return None


def _safe_name(s):
    return (s or 'noname').replace('/', '_').replace(':', '').replace('*', 'x') or 'root'


def _print_closure(label, closure):
    print(f"\n══ {label}: {closure.entry_name} ══")
    by_mode = {}
    for f in closure.files:
        by_mode[f.mode] = by_mode.get(f.mode, 0) + 1
    print(f"  files={len(closure.files)} (by_mode={by_mode}), "
          f"api_calls={len(closure.api_calls)}, "
          f"popup_refs={len(closure.popup_refs)}, "
          f"tokens={closure.total_tokens}, "
          f"truncated={closure.truncated}")
    for f in closure.files:
        print(f"    [d{f.depth} {f.mode:9}] {f.rel_path:55} ~{f.estimated_tokens:5} tok")
    if closure.api_calls:
        print("  API calls:")
        for a in closure.api_calls:
            h = f"  handler={a.handler}" if a.handler else ""
            print(f"    {a.method:6} {a.url or '(dyn)':40} @ {a.file}:{a.line}{h}")
    if closure.popup_refs:
        print("  Popup refs:")
        for p in closure.popup_refs:
            file_hint = p.component_file.name if p.component_file else "<no-file>"
            print(f"    - {p.component_name:24} ({p.trigger:11}) → {file_hint}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('repo', type=Path)
    ap.add_argument('entry', type=Path)
    ap.add_argument('--patterns', type=Path, default=None)
    ap.add_argument('--output', type=Path, default=None)
    ap.add_argument('--max-depth', type=int, default=3)
    ap.add_argument('--token-budget', type=int, default=12000)
    args = ap.parse_args()

    patterns = _load_patterns(args.patterns)

    # 메인 화면 closure
    screen = build_closure(
        entry_file=args.entry,
        repo_root=args.repo,
        patterns=patterns,
        max_depth=args.max_depth,
        token_budget=args.token_budget,
        verbose=True,
    )
    _print_closure("Screen", screen)

    # 팝업 closure (있으면)
    popup_closures = []
    for popup in screen.popup_refs:
        if popup.component_file is None:
            continue
        pc = build_closure(
            entry_file=popup.component_file,
            repo_root=args.repo,
            patterns=patterns,
            max_depth=args.max_depth,
            token_budget=args.token_budget,
            verbose=True,
        )
        popup_closures.append((popup, pc))
        _print_closure(f"Popup ({popup.component_name})", pc)

    # Markdown 저장
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        screen_md = args.output / f"screen__{_safe_name(screen.entry_name)}.md"
        screen_md.write_text(serialize_for_llm(screen), encoding='utf-8')
        for popup, pc in popup_closures:
            popup_md = args.output / f"popup__{_safe_name(pc.entry_name)}.md"
            popup_md.write_text(serialize_for_llm(pc), encoding='utf-8')
        print(f"\n[saved] {1 + len(popup_closures)} files → {args.output}")


if __name__ == '__main__':
    main()
