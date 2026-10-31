#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 类脑记忆引擎入口。

v2.22 起实现按领域拆到同目录的 core/memory/emotion/persona/dream/cli：
本文件只做命名空间合并与 re-export，保证 `python scripts/brain.py <子命令>`
以及外部 `import brain` 的全部旧名字照旧可用。
"""

from __future__ import annotations

import sys

import core
import memory
import emotion
import persona
import dream
import cli

_MODULES = (core, memory, emotion, persona, dream, cli)

# 拆分前所有函数共用同一个模块命名空间，靠名字互相调用。拆分后先把各模块命名空间
# 合并进本模块，再原样回灌给每个子模块——跨模块调用仍按名字解析（过渡桥，
# 以后可以逐步改成显式 import，但不影响命令行兼容）。
for _m in _MODULES:
    globals().update({k: v for k, v in _m.__dict__.items() if not k.startswith("__")})

for _m in _MODULES:
    _m.__dict__.update({k: v for k, v in globals().items() if not k.startswith("__")})

# --home / --ro 会改 core 里的 HOME/READONLY；登记同步目标后，派生路径与只读状态
# 都会在切换时同步到所有子模块（原先单文件时天然共享）。
core._SYNC_TARGETS = list(_MODULES)
core._sync_state()
core._recompute_paths()


def main(argv=None) -> int:
    return cli.main(argv)


if __name__ == "__main__":
    sys.exit(main())
