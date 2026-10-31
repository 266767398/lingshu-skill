#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布前版本绑定检查：改了代码就必须升 SKILL.md 的 version。

用法：
  python scripts/check_version.py            # 比较工作区（含暂存）与 HEAD
  python scripts/check_version.py --staged   # 只比较暂存区（pre-commit 钩子用）
  python scripts/check_version.py --base v2.21
  python scripts/check_version.py --install  # 把 pre-commit 钩子装进 .git/hooks

规则：代码文件（scripts/、assets/、cordis-plugin/、根目录启动脚本与 json）
有变更，但 SKILL.md 的 version 与基准版本相同 → 退出码 1，拒绝提交。
文档（docs/、README、CHANGELOG、SKILL-lite/tiny）不算代码，不触发。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^version:\s*([0-9][0-9.]*)\s*$", re.MULTILINE)
CODE_PREFIXES = ("scripts/", "assets/", "cordis-plugin/")
CODE_ROOT_FILES = {"start-panel.bat", "package.json"}
DOC_NAMES = {"README.md", "CHANGELOG.md", "SKILL.md", "SKILL-lite.md",
             "SKILL-tiny.md", "AGENTS.md"}


def _git(args: list[str]) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return out.stdout if out.returncode == 0 else None


def _read_version(text: str) -> str | None:
    m = VERSION_RE.search(text or "")
    return m.group(1).strip() if m else None


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _is_code(path: str) -> bool:
    p = _norm(path)
    if Path(p).name in DOC_NAMES or p.startswith("docs/"):
        return False
    if p in CODE_ROOT_FILES:
        return True
    return p.startswith(CODE_PREFIXES)


def _changed_files(staged: bool, base: str) -> list[str]:
    if staged:
        raw = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMRT"])
        return [x for x in (raw or "").splitlines() if x.strip()]
    # 工作区模式用 git status：git diff 看不到未跟踪的新文件（新增模块/脚本是最常见的
    # "改了代码"场景），漏掉它们就等于放行。状态码含 ??（未跟踪）、A/M/D/R 等。
    raw = _git(["status", "--porcelain"])
    files = []
    for line in (raw or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:               # 重命名：取新路径
            path = path.split(" -> ", 1)[1]
        files.append(path.strip().strip('"'))
    return files


def check(staged: bool = False, base: str = "HEAD") -> int:
    if _git(["rev-parse", "--is-inside-work-tree"]) is None:
        print("[version] 不是 git 仓库，跳过版本绑定检查")
        return 0

    # 基准（HEAD）与本次要提交的 SKILL.md
    head_text = _git(["show", "HEAD:SKILL.md"])
    if head_text is None:
        print("[version] 还没有 HEAD 版本（首次提交），跳过")
        return 0
    if staged:
        cur_text = _git(["show", ":SKILL.md"])
        if cur_text is None:
            print("[version] 暂存区里没有 SKILL.md，跳过")
            return 0
    else:
        path = ROOT / "SKILL.md"
        if not path.exists():
            print("[version] 工作区没有 SKILL.md，跳过")
            return 0
        cur_text = path.read_text(encoding="utf-8-sig")

    old_ver = _read_version(head_text)
    new_ver = _read_version(cur_text)
    if old_ver is None or new_ver is None:
        print("[version] 读不出 SKILL.md 的 version，跳过")
        return 0

    code = [p for p in _changed_files(staged, base) if _is_code(p)]
    if not code:
        return 0
    if old_ver == new_ver:
        print(f"[version] 拒绝：代码文件有 {len(code)} 处变更，但 SKILL.md 的 version "
              f"仍是 v{old_ver}。先升版本再提交。", file=sys.stderr)
        for p in code[:10]:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"[version] OK：代码变更 {len(code)} 处，版本 v{old_ver} → v{new_ver}")
    return 0


def install_hook() -> int:
    hook_src = ROOT / "scripts" / "hooks" / "pre-commit"
    if not hook_src.exists():
        print(f"[version] 找不到钩子模板：{hook_src}", file=sys.stderr)
        return 2
    git_dir = _git(["rev-parse", "--git-dir"])
    if git_dir is None:
        print("[version] 不是 git 仓库，无法安装钩子", file=sys.stderr)
        return 2
    target_dir = (ROOT / git_dir.strip()).resolve() / "hooks"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "pre-commit"
    if target.exists():
        backup = target.with_name(f"pre-commit.bak-{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(target, backup)
        print(f"[version] 已备份原钩子：{backup}")
    shutil.copy2(hook_src, target)
    try:
        target.chmod(target.stat().st_mode | 0o111)
    except OSError:
        pass
    print(f"[version] 已安装 pre-commit 钩子：{target}")
    print("  之后代码有变更而 SKILL.md 版本没升，提交会被拒绝。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="灵枢：改代码必须升版本")
    ap.add_argument("--staged", action="store_true", help="只检查暂存区（pre-commit 用）")
    ap.add_argument("--base", default="HEAD", help="基准版本，默认 HEAD")
    ap.add_argument("--install", action="store_true", help="安装 pre-commit 钩子")
    args = ap.parse_args(argv)
    if args.install:
        return install_hook()
    return check(args.staged, args.base)


if __name__ == "__main__":
    sys.exit(main())
