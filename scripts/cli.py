#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 灵枢 · 命令行：子命令解析与执行、备份恢复、启动注入、每日例行。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

from core import *

# ---------------------------------------------------------------- backup / restore

def _default_backup_dir() -> Path:
    """备份默认位置：D 盘 > 桌面 > HOME/backups。
    BRAIN_MEMORY_BACKUP_DIR 可显式指定；指定后测试/自定义 HOME 不会意外写到 D 盘。"""
    env_dir = os.environ.get("BRAIN_MEMORY_BACKUP_DIR", "").strip()
    if env_dir:
        candidates = [Path(env_dir).expanduser()]
    else:
        candidates = []
        if sys.platform == "win32":
            candidates.append(Path("D:/brain-memory-backups"))
        candidates.append(Path.home() / "Desktop" / "brain-memory-backups")
        candidates.append(HOME / "backups")
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            return c
        except Exception:
            continue
    return HOME


def _is_sensitive_file(f: Path) -> bool:
    """备份时跳过明显的密钥/凭据文件，避免把秘密打包进 zip。"""
    n = f.name.lower()
    if n == ".env" or n.startswith(".env."):
        return True
    if n.endswith((".token", ".key", ".pem", ".pfx", ".p12")):
        return True
    if "secret" in n or "credential" in n:
        return True
    if n.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")):
        return True
    return False


def cmd_backup(target: str | None = None, keep: int = 20) -> int:
    """把整个记忆库打成 zip。保留最近 keep 份，旧的自动修剪。"""
    if not HOME.exists():
        print(f"[错误] 记忆库还不存在：{HOME}（先运行 init）", file=sys.stderr)
        return 2
    out_dir = Path(target).expanduser() if target else _default_backup_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"[错误] 备份目录不可写：{out_dir}（{exc}）", file=sys.stderr)
        return 2
    zip_path = out_dir / f"brain-memory_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    k = 1
    while zip_path.exists():  # 同一秒内多次备份时避免互相覆盖
        zip_path = out_dir / f"brain-memory_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{k}.zip"
        k += 1
    n = 0
    skipped = 0
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(HOME.rglob("*")):
                if f.is_dir() or "__pycache__" in f.parts:
                    continue
                if f.name.endswith(".tmp") or f.name == ".write-probe":
                    continue
                if f == zip_path:
                    continue  # 别把正在写的备份包自己打包进去（--out 指向记忆库自身时会踩到）
                if "backups" in f.parts and f.suffix == ".zip":
                    continue  # 别把上一次的备份打包进去
                if _is_sensitive_file(f):
                    skipped += 1
                    continue  # 密钥/凭据不进备份包
                zf.write(f, f.relative_to(HOME).as_posix())
                n += 1
    except Exception as exc:
        print(f"[错误] 备份失败：{exc}", file=sys.stderr)
        return 2
    if skipped:
        print(f"跳过 {skipped} 个敏感文件（密钥/凭据不进备份）")
    removed = 0
    if keep and keep > 0:
        existing = sorted(out_dir.glob("brain-memory_*.zip"), key=lambda p: p.name, reverse=True)
        for p in existing[keep:]:
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"已备份 {n} 个文件 → {zip_path}（{size_mb:.1f} MB）")
    if removed:
        print(f"修剪旧备份 {removed} 个（保留最近 {keep} 个）")
    return 0


MAX_RESTORE_ENTRIES = 200
MAX_RESTORE_BYTES = 512 * 1024 * 1024  # 512 MB，防 zip 炸弹


def cmd_restore(zip_path: str, yes: bool = False) -> int:
    """从备份恢复记忆库。恢复前自动给现状打一份备份。"""
    zp = Path(zip_path).expanduser()
    if not zp.exists():
        print(f"[错误] 找不到备份文件：{zp}", file=sys.stderr)
        return 2
    try:
        zf = zipfile.ZipFile(zp)
    except zipfile.BadZipFile:
        print("[错误] 不是有效的 zip 文件", file=sys.stderr)
        return 2
    with zf:
        names = zf.namelist()
        bad = [x for x in names
               if x.startswith("/") or ".." in Path(x).parts or ":" in x]
        if bad:
            print(f"[错误] 备份里包含非法路径，已拒绝：{bad[:3]}", file=sys.stderr)
            return 2
        # 防 zip 炸弹：条目数、解压后总大小都要有上限
        if len(names) > MAX_RESTORE_ENTRIES:
            print(f"[错误] 备份条目过多（{len(names)} > {MAX_RESTORE_ENTRIES}），拒绝恢复", file=sys.stderr)
            return 2
        total_size = sum(i.file_size for i in zf.infolist() if not i.is_dir())
        if total_size > MAX_RESTORE_BYTES:
            print(f"[错误] 备份解压后过大（{total_size / 1024 / 1024:.0f} MB > {MAX_RESTORE_BYTES / 1024 / 1024:.0f} MB），拒绝恢复",
                  file=sys.stderr)
            return 2
        if not any(x in ("persona.json", "hippocampus.jsonl", "cortex.jsonl", "config.json") for x in names):
            print("[错误] 这不像灵枢的记忆备份（缺 persona.json / hippocampus.jsonl）", file=sys.stderr)
            return 2
        if not yes:
            try:
                if sys.stdin and sys.stdin.isatty():
                    ans = input(f"恢复会把备份内容写回 {HOME}（备份之后新增的文件不会被删除）。继续？[y/N] ")
                    if ans.strip().lower() not in ("y", "yes"):
                        print("已取消。")
                        return 0
            except EOFError:
                pass
    if HOME.exists():
        cmd_backup(target=str(HOME / "backups"), keep=10)  # 恢复前先留现状
    # 解压到临时目录，然后整包用 os.replace 原子替换——避免中途失败把目标文件写成半截。
    # 整个恢复过程持锁，防止与正在跑的面板/CLI 并发互相踩写。
    tmp_dir = HOME / f".restore-tmp-{int(now_ts())}"
    try:
        with zipfile.ZipFile(zp) as zf:
            tmp_dir.mkdir(parents=True, exist_ok=False)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = Path(info.filename)
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError(f"备份里有非法路径：{info.filename}")
                target = (tmp_dir / rel).resolve()
                if not target.is_relative_to(tmp_dir):
                    raise ValueError(f"备份条目试图逃出临时目录：{info.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            # 解压完成，原子替换：每个目标文件 os.replace（已存在覆盖，不存在新建）
            # 只覆盖 zip 里有的文件——备份之后新增的文件不会被删除
            with _locked(HOME / "hippocampus.jsonl"), _locked(HOME / "persona.json"):
                for extracted in tmp_dir.rglob("*"):
                    if extracted.is_dir():
                        continue
                    rel = extracted.relative_to(tmp_dir)
                    final = (HOME / rel).resolve()
                    final.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(extracted), str(final))
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except PermissionError as exc:
        print(f"[错误] 恢复被拒绝（当前环境可能只读）：{exc}", file=sys.stderr)
        return 3
    finally:
        # 无论成功还是失败，都清掉临时目录
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
    print(f"已从 {zp.name} 恢复记忆库 → {HOME}")
    return 0


# ---------------------------------------------------------------- archive（冷热归档）

def cmd_archive(action: str = "show", arg: str = "", limit: int = 20,
                dry_run: bool = False) -> int:
    """归档层命令：查看 / 搜索 / 回捞 / 手动触发归档。

    归档只把记忆从热遍历集合里移出，原文一字不丢；`archive restore <id>` 随时回热。
    """
    if action == "run":
        try:
            r = archive_run(dry_run=dry_run)
        except PermissionError as exc:
            print(f"[只读] {exc}", file=sys.stderr)
            return 3
        verb = "可归档" if dry_run else "已雪藏"
        print(f"{verb} {r['count']} 条冷记忆 → 归档层（原文保留，archive search / restore 可找回）。")
        for x in r["archived"][:max(limit, 1)]:
            print(f"  {x['id']}  {x['gist']}")
        return 0

    rows = load_archive()
    if action == "count":
        print(len(rows))
        return 0

    if action == "search":
        if not arg:
            print("用法：archive search <关键词>", file=sys.stderr)
            return 2
        q = terms(arg)
        hits = [m for m in rows if q and any(t in _hay(m) for t in q)]
        if not hits:
            print(f"归档层没找到「{arg}」。")
            return 1
        hits.sort(key=lambda m: m.get("archived_at", m.get("created", 0)), reverse=True)
        print(f"归档层命中「{arg}」→ {len(hits)} 条\n")
        for m in hits[:max(limit, 1)]:
            _print_mem(m)
        return 0

    if action == "restore":
        if not arg:
            print("用法：archive restore <id>", file=sys.stderr)
            return 2
        if READONLY:
            print("[只读] 记忆库当前不可写，无法回捞。", file=sys.stderr)
            return 3
        with _locked(HOME / "hippocampus.jsonl"), _locked(HOME / "archive.jsonl"):
            rows = load_archive()
            hit = next((m for m in rows if m.get("id") == arg), None)
            if not hit:
                print(f"归档层没有 {arg}。")
                return 1
            hit["layer"] = "cortex"
            hit.pop("archived_at", None)
            hit["last_access"] = now_ts()
            # 顺序反过来：先 save_all 成功，再从 archive 里删。
            # 中途任何一步失败 hit 都还留在 archive.jsonl，不会整条消失。
            hot = load_all()
            hot.append(hit)
            save_all(hot)
            write_jsonl(HOME / "archive.jsonl", [m for m in rows if m.get("id") != arg])
        print(f"已回捞 {arg} → 长期记忆（重新参与回忆）。")
        return 0

    # 默认 show
    if not rows:
        print("归档层是空的：还没有雪藏的记忆。")
        return 0
    rows.sort(key=lambda m: m.get("archived_at", m.get("created", 0)), reverse=True)
    print(f"归档层 {len(rows)} 条（默认不参与 recall；archive search / restore 找回）：")
    for m in rows[:max(limit, 1)]:
        print(f"  {m['id']}  归档于 {fmt_time(m.get('archived_at', 0))} · {display_text(m)[:60]}")
    return 0


# ---------------------------------------------------------------- home（数据目录）

def cmd_home(action: str = "show", dest: str = "") -> int:
    if action in ("show", ""):
        print(f"记忆库目录：{HOME}")
        print(f"只读状态：{'只读降级（不可写）' if READONLY else '可读写'}")
        p = HOME / "persona.json"
        print(f"人格档案：{'已初始化' if p.exists() else '未初始化'}")
        if not p.exists():
            print("  → 先运行：python scripts/brain.py init")
        print("换目录：python scripts/brain.py --home <目录> ...，或设置 BRAIN_MEMORY_HOME")
        return 0
    if action == "verify":
        print(f"记忆库：{HOME}")
        print(f"可写：{'是' if _home_writable() else '否（只读降级）'}")
        print(f"配置：{'存在' if (HOME / 'config.json').exists() else '缺失'}")
        print(f"人格：{'存在' if (HOME / 'persona.json').exists() else '缺失'}")
        return 0
    if action == "move":
        if not dest:
            print("用法：home move <目标目录>", file=sys.stderr)
            return 2
        dst = Path(dest).expanduser()
        if not HOME.exists():
            print(f"[错误] 源记忆库不存在：{HOME}", file=sys.stderr)
            return 2
        try:
            dst_res, home_res = dst.resolve(), HOME.resolve()
        except OSError:
            dst_res, home_res = dst, HOME
        if dst_res == home_res:
            print("目标目录就是当前记忆库，已是最新。")
            return 0
        try:
            dst_res.relative_to(home_res)
            print(f"[错误] 目标目录不能是记忆库内部（会边复制边膨胀）：{dst}", file=sys.stderr)
            return 2
        except ValueError:
            pass
        if dst.exists():
            if any(dst.iterdir()) and not (dst / "persona.json").exists():
                print(f"[错误] 目标目录非空且不像记忆库：{dst}", file=sys.stderr)
                return 2
        else:
            try:
                dst.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                print(f"[错误] 无法创建目标目录：{exc}", file=sys.stderr)
                return 2
        n = 0
        try:
            for f in HOME.rglob("*"):
                if f.is_dir() or "__pycache__" in f.parts:
                    continue
                if f.name.endswith(".tmp") or f.name == ".write-probe":
                    continue
                target = dst / f.relative_to(HOME)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                n += 1
        except PermissionError as exc:
            print(f"[错误] 迁移失败（目标不可写？）：{exc}", file=sys.stderr)
            return 2
        print(f"已复制 {n} 个文件 → {dst}")
        print("之后用这个目录（二选一）：")
        print(f"  setx BRAIN_MEMORY_HOME \"{dst}\"   # 永久，新终端生效")
        print(f"  python scripts/brain.py --home \"{dst}\" ...   # 单次")
        return 0
    print("用法：home [show | verify | move <目标目录>]", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- startup（会话开始钩子）

def cmd_startup(query: str = "", budget: int | None = None) -> int:
    """会话开始钩子：输出注入包，用返回码表达状态。
    0 = 正常注入；10 = 只读降级注入（记忆可能不新鲜）；2 = 记忆库未初始化。
    """
    if READONLY:
        print("[灵枢] 只读注入降级：记忆库当前不可写，注入基于已有文件。", file=sys.stderr)
    p_path = HOME / "persona.json"
    mem_paths = [HOME / "hippocampus.jsonl", HOME / "cortex.jsonl"]
    if not p_path.exists() and not any(m.exists() for m in mem_paths):
        print("[灵枢] 记忆库未初始化：先运行 python scripts/brain.py init"
              "（只读环境可用 --home 指到已有目录）", file=sys.stderr)
        return 2
    print(build_injection(query, budget))
    if not READONLY:
        try:
            with _locked(HOME / "config.json"):
                cfg = load_config()
                cfg["last_startup"] = {"ts": now_ts(), "query": (query or "")[:60]}
                atomic_json(HOME / "config.json", cfg)
        except Exception:
            pass
    return 10 if READONLY else 0


# ---------------------------------------------------------------- sync dsh（与 DSH 记忆每日同步）

def _resolve_dsh_dir(explicit: str = "") -> Path | None:
    cfg = load_config()
    env = os.environ.get("DSH_MEMORY_HOME")
    candidates = [
        explicit,
        cfg.get("dsh_memory_dir") or "",
        env or "",
        Path.home() / ".dsh-memory",
        Path.cwd() / ".dsh-memory",
        Path.cwd() / ".workbuddy" / "memory",
        Path.cwd() / ".codebuddy" / "memory",
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser() if isinstance(c, str) else c
        if p.is_dir():
            return p
    return None


def cmd_sync_dsh(days: int = 1, dry_run: bool = False, force: bool = False,
                 explicit: str = "") -> int:
    """每日同步：把灵枢侧日志摘要追加进 DSH 记忆的当日文件。"""
    dsh = _resolve_dsh_dir(explicit)
    if not dsh:
        print("[错误] 没找到 DSH 记忆目录。用 --dir 指定，或设 DSH_MEMORY_HOME。", file=sys.stderr)
        return 2
    try:
        import growth
    except Exception as exc:
        print(f"[错误] 无法加载成长子系统：{exc}", file=sys.stderr)
        return 2
    today = fmt_date(now_ts())
    floor = now_ts() - days * DAY
    notes = [n for n in growth.load_notes() if n.get("created", 0) >= floor]

    lines = [
        f"## 灵枢 → DSH 同步 · {today}",
        "> 边界：灵枢管人格/情绪/关系；DSH 管工作/项目。以下是灵枢侧日志摘要，供工作侧参考。",
        f"<!-- lingshu-sync:{today} -->",
    ]
    if notes:
        lines.append("### 灵枢日志摘要")
        for n in sorted(notes, key=lambda r: r.get("created", 0)):
            body = (n.get("body") or "").replace("\n", " ").strip()
            lines.append(f"- [{n.get('date')} {n.get('type')}] {n.get('title')}：{body[:120]}")
    else:
        lines.append("（今天还没有灵枢侧日志）")

    dreams_dir = HOME / "dreams"
    if dreams_dir.exists():
        files = sorted(dreams_dir.glob("*.md"))
        if files:
            digest_lines = [l.strip() for l in files[-1].read_text(encoding="utf-8").splitlines()
                            if l.strip() and not l.strip().startswith("#")]
            if digest_lines:
                lines.append("### 最近一次梦的结论")
                lines.extend("- " + l[:90] for l in digest_lines[:5])

    block = "\n".join(lines)
    marker = f"<!-- lingshu-sync:{today} -->"
    target = dsh / f"{today}.md"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""

    if dry_run:
        print(block)
        print(f"\n（预览：将写入 {target}）")
        return 0
    if marker in existing and not force:
        print(f"今日已同步过（{target}），跳过。加 --force 可覆盖。")
        return 0
    if marker in existing:
        # 只覆盖今天这一块：用 rfind 定位最后一个同步块起点，避免把历史每天的块一起删掉
        head = "## 灵枢 → DSH 同步"
        s = existing.rfind(head)
        e = existing.find(marker)
        if s != -1 and e != -1:
            e = e + len(marker)
            while e < len(existing) and existing[e] in "\r\n":
                e += 1
            existing = existing[:s] + existing[e:]
    tmp = _tmp_path(target)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write((existing.rstrip() + "\n\n" if existing.strip() else "") + block + "\n")
    _replace_retry(tmp, target)
    try:  # 记住目录，下次免传
        with _locked(HOME / "config.json"):
            cfg = load_config()
            cfg["dsh_memory_dir"] = str(dsh)
            atomic_json(HOME / "config.json", cfg)
    except Exception:
        pass
    print(f"已把灵枢日志摘要追加到 DSH 记忆：{target}")
    return 0


# ---------------------------------------------------------------- dream --auto（内置自愈）

def auto_dream(trigger: str = "auto", dry_run: bool = False) -> dict:
    """内置自愈式做梦：机器重放 → 启发式压缩 → 自动沉淀。
    不依赖外部 dream_auto 脚本；人格不自动改；更抽象的压缩仍建议人工复核梦报告。"""
    from collections import Counter
    report = dream(trigger, dry_run=dry_run)
    if not report["clusters"]:
        return {"applied": False, "report": report, "memories": 0}
    apply_doc = {
        "task_id": report["task_id"],
        "persona_updates": {},
        "forget": [],
        "narrative": "自动做梦（--auto）：按主题启发式合并碎片，未改动人格。",
    }
    memories = []
    for c in report["clusters"]:
        texts = c["texts"]
        top = sorted(texts, key=lambda t: _num(t.get("importance"), 0.5), reverse=True)[:3]
        kind = Counter(t["kind"] for t in texts).most_common(1)[0][0]
        label = "、".join(c["tags"][:4]) or "、".join(c["entities"][:4]) or "未命名主题"
        joined = "；".join(t["text"] for t in top)
        memories.append({
            "kind": kind,
            "text": f"最近关于「{label}」的一组事情（{len(texts)} 条）：{joined}"[:400],
            "gist": f"关于「{label}」的 {len(texts)} 条经历",
            "importance": min(0.9, max((_num(t.get("importance"), 0.5) for t in texts), default=0.5)),
            "tags": c["tags"],
            "entities": c["entities"],
            "source_ids": c["ids"],
        })
    apply_doc["memories"] = memories
    if dry_run:
        return {"applied": False, "report": report, "preview": apply_doc}
    result = apply_dream(apply_doc)
    result["applied"] = True
    result["clusters"] = len(report["clusters"])
    return result


# ---------------------------------------------------------------- daily（每天例行）
# 要"每天更新保持最新"：daily 每天跑一次，自动完成三件事——
#   1) 写今日日记（journal）：今天经历了什么、情绪怎么样，落到笔记
#   2) 更新知识库：把今天重要的洞见/偏好提炼成可复用结论（自动去重合并）
#   3) 维护新鲜度：距上次做梦超过 24 小时就自动做梦（--auto）
# 会话开始时 build_injection 会检查：如果今天还没跑，就提醒先跑 daily。

DAILY_FILE = HOME / "daily_state.json"



def _daily_state() -> dict:
    if DAILY_FILE.exists():
        d = _slurp_json(DAILY_FILE)
        if isinstance(d, dict):
            return d
    return {"last_run": 0, "last_date": ""}


def _save_daily_state(st: dict) -> None:
    atomic_json(DAILY_FILE, st)


def _parse_feel_specs(specs: list) -> tuple:
    """解析 `daily --feel "情绪:强度:原因"`。返回 ([(情绪, 强度, 原因)], [坏规格])。

    强度可省（默认 0.5）；>1 视为 0-10 分制，自动折成 0-1。
    格式不对或情绪名不认识的不写，交回结果里让模型看见。
    """
    out, bad = [], []
    for raw in specs or []:
        parts = re.split(r"[：:]", str(raw).strip(), maxsplit=2)
        name = parts[0].strip()
        intensity, why = 0.5, ""
        if len(parts) > 1:
            p1 = parts[1].strip()
            try:
                intensity = float(p1)
            except ValueError:
                why = p1
        if len(parts) > 2:
            tail = parts[2].strip()
            why = f"{why}；{tail}" if why else tail
        if name not in EMOTIONS:
            bad.append(str(raw))
            continue
        if intensity > 1:          # 0-10 分制
            intensity = intensity / 10.0
        out.append((name, max(0.0, min(1.0, intensity)), why))
    return out, bad


def daily(dry_run: bool = False, force: bool = False, feel_specs: list | None = None) -> dict:
    """每天例行。dry_run 只预览要写什么，不落盘；force 无视"今天已跑过"。

    feel_specs：让 daily 顺手接收当天对话里提炼出的情绪事件
    （`--feel "开心:0.7:用户夸我把复杂的事讲清楚了"`），把情绪采集挂在已有的
    低成本动作上，而不是等模型无缘无故主动 emotion feel。
    """
    ensure_home()
    today = fmt_date(now_ts())
    st = _daily_state()
    if READONLY and not dry_run:
        raise PermissionError("记忆库当前只读，无法执行每日例行。")

    specs, bad_specs = _parse_feel_specs(feel_specs or [])
    feel_added = []
    if not dry_run:
        for name, intensity, why in specs:
            try:
                feel_added.append(feel(name, intensity, why, about="今日对话"))
            except Exception as e:   # 单条坏情绪不该拖垮整次例行
                bad_specs.append(f"{name}: {e}")

    if st.get("last_date") == today and not force and not dry_run:
        return {"skipped": True, "reason": "今天已经例行过了（daily --force 可重跑）",
                "feelings_added": len(feel_added), "feelings_bad": bad_specs}

    floor = now_ts() - 1 * DAY
    mems = [m for m in load_all() if _num(m.get("created"), 0) >= floor]
    arc = emotion_arc(1)
    ra = rationality()

    # ---- 1) 今日日记正文（机器先起草，AI 可补充润色）
    body = []
    body.append(f"今天新增记忆 {len(mems)} 条。")
    if arc["count"]:
        top_txt = "、".join("{}×{}".format(x["emotion"], x["times"]) for x in arc["top"])
        body.append("情绪：{}；出现最多：{}。".format(arc["tone"], top_txt))
        # 峰终定律：回顾一天，真正留在印象里的是"最强烈的那一刻"和"此刻的心情"
        pk, en = arc.get("peak") or {}, arc.get("end") or {}
        if pk.get("emotion"):
            pw = f"（{pk['why']}）" if pk.get("why") else ""
            body.append("印象最深的一刻：{}{}，强度 {:.0%}。".format(
                pk["emotion"], pw, float(pk.get("intensity", 0))))
        if en.get("emotion"):
            body.append("此刻的心情：{}。".format(en["emotion"]))
    else:
        body.append("情绪：今天还没有情绪记录。")
    low = desire_low()
    if low:
        body.append("没被满足的欲望：" + "、".join(f"{x['desire']}({x['level']:.0f})" for x in low) + "。")
        # 低谷自我调节：写日记本身是一次自我觉察，像人睡前盘点了心里的事，
        # 低着的欲望会回升一点（不是满足，是"被看见"）。幅度小，且只对低谷项。
        if not dry_run:
            desire_nudge({x["desire"]: 3.0 for x in low}, cooldown=20 * HOUR)
    else:
        body.append("六欲平稳。")
    body.append(f"理智：{ra['effective']}（基数 {ra['base']}，被情绪压住 {ra['suppress']}%）。")
    insight_mems = [m for m in mems
                    if m.get("kind") in ("insight", "preference", "habit")
                    and _num(m.get("importance"), 0) >= 0.7]
    if insight_mems:
        body.append("今天值得沉淀的想法：")
        body += [f"- {display_text(m)}" for m in insight_mems[:5]]

    # ---- 2) 更新知识库：把今天重要的洞见/偏好提炼成可复用结论（dry_run 不落盘）
    k_added, k_merged = 0, 0
    if dry_run:
        k_added = len(insight_mems)   # 预览：预计会新增/合并的数量
    else:
        import growth
        for m in insight_mems:
            title = heuristic_gist(m.get("text", ""), 24)
            try:
                _, how = growth.add_knowledge(
                    title, display_text(m), topic="每日沉淀",
                    points=[heuristic_gist(m.get("text", ""), 60)],
                    tags=(m.get("tags") or [])[:4],
                    entities=(m.get("entities") or [])[:4],
                    source_ids=[m["id"]],
                    confidence=min(0.9, 0.55 + 0.3 * _num(m.get("importance"), 0.5)))
                if how == "merged":
                    k_merged += 1
                else:
                    k_added += 1
            except Exception:
                pass

    # ---- 3) 新鲜度：距上次做梦超过 24h 就自动做梦
    dream_state = load_dream_state()
    dreamed = False
    if now_ts() - int(_num(dream_state.get("last_dream"), 0)) > DAY:
        if dry_run:
            dreamed = "would_run"
        else:
            try:
                auto_dream("daily")
                dreamed = True
            except Exception:
                dreamed = False

    if not dry_run:
        import growth
        note, how = growth.write_note("journal", f"今日 {today}",
                                      "\n".join(body), mood=mood_brief(),
                                      tags=["每日例行"],
                                      memory_ids=[m["id"] for m in mems[:12] if m.get("id")],
                                      append_same_day=not force)
        st["last_run"] = now_ts()
        st["last_date"] = today
        st["note_id"] = note.get("id")
        _save_daily_state(st)

    return {
        "skipped": False, "date": today, "dry_run": dry_run,
        "memories_today": len(mems), "note_body": "\n".join(body),
        "knowledge_added": k_added, "knowledge_merged": k_merged,
        "dreamed": dreamed,
        "feelings_added": len(feel_added), "feelings_bad": bad_specs,
        "mood": mood_brief(),
    }
def _print_mem(m: dict, score: float | None = None, parts: dict | None = None) -> None:
    star = "★" * max(1, round(_num(m.get("importance"), 0.5) * 5))
    layer = {"hippo": "海马体", "cortex": "皮层", "dust": "淡忘"}.get(m.get("layer"), m.get("layer"))
    head = f"[{fmt_time(m.get('created', 0))}] {layer} · {m.get('kind')} · {star}"
    if score is not None:
        head += f" · 回忆度 {score:.2f}"
        if parts:
            head += f" (相关{parts.get('relevance')}/新鲜{parts.get('recency')}/重要{parts.get('importance')})"
    print(head)
    print(f"  {m['id']}")
    print(f"  {display_text(m)}")
    if m.get("tags") or m.get("entities"):
        print(f"  标签: {'、'.join(m.get('tags', []))}  实体: {'、'.join(m.get('entities', []))}")
    print()


class _BrainParser(argparse.ArgumentParser):
    """用法错误退出码用 64（EX_USAGE），避免与 startup 的"记忆库未初始化=2"撞码。

    宿主据此区分：64=命令/参数写错（改命令即可）；2=记忆库没初始化（要 init）。
    """
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: 参数错误：{message}", file=sys.stderr)
        raise SystemExit(64)


def _main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = _override_home(argv)          # 支持 --home 全局换数据目录
    argv, ro_forced = _extract_ro(argv)  # 支持 --ro / --no-write
    _resolve_readonly(ro_forced)         # 自动探测 HOME 可写性，决定只读降级
    if argv and argv[0] in ("knowledge", "note", "skill", "ext", "growth", "evolve", "learn"):
        import growth
        return growth.run(argv)

    parser = _BrainParser(prog="brain.py", description="灵枢 类脑记忆引擎")
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=_BrainParser)

    p_init = sub.add_parser("init", help="初始化记忆库")
    p_init.add_argument("--force", action="store_true", help="重置人格档案（清空初识与演化历史）")

    p_rem = sub.add_parser("remember", help="写入一条记忆")
    p_rem.add_argument("text", nargs="+")
    p_rem.add_argument("--kind", default="fact", choices=KINDS)
    p_rem.add_argument("--importance", type=float, default=None, help="0-1 或 0-10")
    p_rem.add_argument("--emotion", default="", help="同时记下此刻的情绪（杏仁核标记，情绪强的记得更牢）")
    p_rem.add_argument("--tags", default="")
    p_rem.add_argument("--entities", default="")
    p_rem.add_argument("--source", default="ai", choices=["ai", "user", "dream"])
    p_rem.add_argument("--layer", default="hippo", choices=["hippo", "cortex"])

    p_rec = sub.add_parser("recall", help="模糊回忆")
    p_rec.add_argument("query", nargs="*", default=[])
    p_rec.add_argument("--k", type=int, default=8)
    p_rec.add_argument("--json", action="store_true")
    p_rec.add_argument("--include-archive", action="store_true",
                       help="连归档层的冷记忆一起找（默认不搜归档）")

    p_inj = sub.add_parser("inject", help="生成记忆注入包")
    p_inj.add_argument("query", nargs="*", default=[])
    p_inj.add_argument("--budget", type=int, default=None)

    p_dream = sub.add_parser("dream", help="做梦：整理与巩固记忆")
    p_dream.add_argument("--trigger", default="manual")
    p_dream.add_argument("--apply", default=None, help="应用整理结果 JSON")
    p_dream.add_argument("--dry-run", action="store_true")
    p_dream.add_argument("--auto", action="store_true", help="自动做梦：重放→启发式压缩→沉淀，不依赖外部脚本")

    p_per = sub.add_parser("persona", help="人格查看与编辑")
    p_per.add_argument("action", nargs="?", default="show",
                       choices=["show", "set", "sync", "path", "interview", "self", "onboarding",
                                "lock", "unlock", "history", "rollback",
                                "snapshot", "snapshots", "restore", "drift"])
    p_per.add_argument("pair", nargs="?", default=None, help="例如 traits.幽默=70 / rollback 的序号")
    p_per.add_argument("--json", action="store_true")
    p_per.add_argument("--name", default="", help="interview：用户给你起的名字")
    p_per.add_argument("--traits", default="", help="interview：性格，如 温柔:70,好奇:85")
    p_per.add_argument("--style", default="", help="说话方式，逗号分隔")
    p_per.add_argument("--values", default="", help="价值观/边界，逗号分隔")
    p_per.add_argument("--callme", default="", help="interview：你怎么称呼用户")
    p_per.add_argument("--self-name", default="", help="interview：你自称什么")
    p_per.add_argument("--likes", default="", help="self：我自己喜欢什么")
    p_per.add_argument("--dislikes", default="", help="self：我自己讨厌什么")
    p_per.add_argument("--habits", default="", help="self：我想养成的习惯")
    p_per.add_argument("--insights", default="", help="self：我自己的洞见")
    p_per.add_argument("--unsure", action="store_true", help="interview：用户不知道名字/性格，自己定")
    p_per.add_argument("--limit", type=int, default=30, help="history：显示最近 N 条改动")
    p_per.add_argument("--label", default="", help="snapshot：给快照起个名")
    p_per.add_argument("--days", type=int, default=7, help="drift：看最近 N 天的漂移")

    p_mind = sub.add_parser("mind", help="理智与好奇心：思考决策")
    p_mind.add_argument("action", nargs="?", default="state",
                       choices=["state", "set", "tune", "weigh"])
    p_mind.add_argument("topic", nargs="?", default="")
    p_mind.add_argument("--curiosity", type=int, default=None, help="set 绝对值 0-100；tune 增量如 +5")
    p_mind.add_argument("--reason", type=int, default=None, help="set 绝对值 0-100；tune 增量如 -3")
    p_mind.add_argument("--why", default="用户态度", help="tune：是什么态度让你调整")
    p_mind.add_argument("--benefit", type=float, default=5, help="收益打分 0-10")
    p_mind.add_argument("--cost", type=float, default=5, help="代价/成本打分 0-10")
    p_mind.add_argument("--confidence", type=float, default=0.6, help="把握程度 0-1 或 0-10")
    p_mind.add_argument("--urgency", type=float, default=0.5, help="紧迫程度 0-1")

    p_emo = sub.add_parser("emotion", help="情绪系统：认知评价/共情/记感受/看心情/看轨迹/改情感设置")
    p_emo.add_argument("action", nargs="?", default="now",
                       choices=["feel", "evaluate", "mirror", "config", "now", "arc", "list",
                                "desire", "events", "adjust", "del", "cool", "check"])
    p_emo.add_argument("emotion", nargs="?", default="",
                       help="feel：情绪名；evaluate/mirror：事件或描述；desire：欲望名；adjust/del：序号")
    p_emo.add_argument("--intensity", type=float, default=None, help="这件事当时有多重 0-1")
    p_emo.add_argument("--amount", type=float, default=15, help="desire：满足欲望的量 0-100")
    p_emo.add_argument("--emotion-name", default="", help="adjust：把这条情绪改成另一个情绪名")
    p_emo.add_argument("--polarity", default="auto", help="evaluate：positive/negative/neutral/auto")
    p_emo.add_argument("--why", default="", help="因为什么")
    p_emo.add_argument("--about", default="", help="关于谁/什么事")
    p_emo.add_argument("--decay-hours", type=float, default=None, help="这条情绪的余波时长（小时）")
    p_emo.add_argument("--days", type=int, default=7, help="arc：看最近 N 天的情绪轨迹")
    p_emo.add_argument("--empathy", default="", help="config：共情等级 mirror/mild/off")
    p_emo.add_argument("--gain", type=float, default=None, help="config：共情跟进的强度比例 0-1")
    p_emo.add_argument("--max-intensity", type=float, default=None, help="config：共情镜像的最大强度 0-1")
    p_emo.add_argument("--suppress", default="", help="config：要抑制的情绪名，逗号分隔")
    p_emo.add_argument("--notes", default="", help="config：给情感系统的一句备注")

    p_assoc = sub.add_parser("assoc", help="记忆关联（联想能力）：link/unlink/auto/graph/邻居")
    p_assoc.add_argument("action", nargs="?", default="graph",
                         choices=["link", "unlink", "auto", "graph", "of"])
    p_assoc.add_argument("ids", nargs="*", default=[], help="link/unlink/of：记忆 id")
    p_assoc.add_argument("--relation", default="相关", help="link：关系名字，如 因果/相关/情绪")
    p_assoc.add_argument("--weight", type=float, default=1.0, help="link：权重 0-3")
    p_assoc.add_argument("--why", default="", help="link：为什么关联")
    p_assoc.add_argument("--dry-run", action="store_true", help="auto：只预览要建立哪些边")
    p_assoc.add_argument("--limit", type=int, default=300, help="graph：节点上限")

    p_daily = sub.add_parser("daily", help="每日例行：写日记 + 沉淀知识 + 过期的梦自动做掉")
    p_daily.add_argument("--dry-run", action="store_true", help="只预览今天会写什么")
    p_daily.add_argument("--force", action="store_true", help="今天已跑过也重跑")
    p_daily.add_argument("--feel", action="append", default=[], metavar="情绪:强度:原因",
                         help="顺手记一条当天情绪（可重复）。如 --feel \"开心:0.7:用户夸我讲清楚了\"")

    p_usage = sub.add_parser("usage", help="回忆使用反馈：核对上一轮找回的记忆有没有出现在回复里")
    p_usage.add_argument("--reply", required=True, help="本轮回复文本（关键词重叠粗判）")
    p_usage.add_argument("--json", action="store_true")

    p_stats = sub.add_parser("stats", help="记忆统计")
    p_stats.add_argument("--json", action="store_true")

    p_logs = sub.add_parser("logs", help="运行日志：锁冲突 / 内部异常 / 解析报错（面板「日志」页同源）")
    p_logs.add_argument("--limit", type=int, default=30)
    p_logs.add_argument("--level", default="warn", choices=["info", "warn", "error"])
    p_logs.add_argument("--all", action="store_true", help="不过滤级别，全部显示")

    p_cfg = sub.add_parser("config", help="查看 / 修改配置")
    p_cfg.add_argument("action", nargs="?", default="show",
                       choices=["show", "set", "unset"])
    p_cfg.add_argument("key", nargs="?", default="", help="set/unset：配置项名")
    p_cfg.add_argument("value", nargs="?", default="", help="set：配置项值（如 model_tier small）")

    p_conf = sub.add_parser("detect-conflicts", help="启发式扫描矛盾记忆（如「用Windows」vs「改用Linux」）")
    p_conf.add_argument("--json", action="store_true")
    p_conf.add_argument("--limit", type=int, default=50)

    p_mode = sub.add_parser("mode", help="会话模式：work 工程（情绪隔离）/ human 拟人 / auto 按场景自动切")
    p_mode.add_argument("action", nargs="?", default="show",
                        choices=["show", "work", "human", "auto", "explain"])
    p_mode.add_argument("query", nargs="*", default=[], help="explain：一段话题话，看 auto 会怎么判定")

    p_rev = sub.add_parser("review", help="审核 AI 整理出来的记忆 / 知识 / 人格变更（防幻觉沉淀成事实）")
    p_rev.add_argument("action", nargs="?", default="list",
                       choices=["list", "confirm", "discard"])
    p_rev.add_argument("id", nargs="?", default="", help="confirm/discard：那条的 id（m_ 开头的记忆 / k_ 开头的知识）")
    p_rev.add_argument("--json", action="store_true")

    p_audit = sub.add_parser("audit", help="整体自查：待审核项 + 冲突记忆 + 该做什么")
    p_audit.add_argument("--json", action="store_true")

    p_ur = sub.add_parser("user-rules",
                          help="用户附加规则：写在数据目录 lingshu.user.md，更新技能永不覆盖，每会话自动注入")
    p_ur.add_argument("action", nargs="?", default="show", choices=["show", "add", "clear"])
    p_ur.add_argument("text", nargs="*", default=[], help="add：要追加的一条规则")

    p_list = sub.add_parser("list", help="列出记忆")
    p_list.add_argument("--limit", type=int, default=30)
    p_list.add_argument("--layer", default=None, choices=LAYERS)
    p_list.add_argument("--json", action="store_true")

    p_forget = sub.add_parser("forget", help="忘记一条记忆")
    p_forget.add_argument("id")

    p_ctx = sub.add_parser("check-context", help="检查上下文水位：60%% 清点 / 75%% 做梦 / 90%% 封存")
    p_ctx.add_argument("--used", type=float, required=True, help="已用上下文百分比 0-100")
    p_ctx.add_argument("--unsaved", type=float, default=0,
                       help="本轮还没落盘的估计字数（凭感觉估即可，用来提示'该记没记'）")

    p_work = sub.add_parser("work", help="工作记忆：当前上下文，默认不落盘")
    p_work.add_argument("action", nargs="?", default="status",
                        choices=["status", "add", "list", "done", "flush", "clear"])
    p_work.add_argument("text", nargs="*", default=[])
    p_work.add_argument("--tag", default="")
    p_work.add_argument("--why", default="")
    p_work.add_argument("--skip-done", action="store_true",
                        help="flush 时丢弃已完成条目，不转记忆（做完了就该放下）")
    p_work.add_argument("--json", action="store_true")

    p_cap = sub.add_parser("capture", help="上下文原样封存：逐段落盘 + 原文归档，100%% 不丢")
    p_cap.add_argument("payload", nargs="+")
    p_cap.add_argument("--tag", default="", help="如 逃生/会话/主题")
    p_cap.add_argument("--kind", default="episode", choices=KINDS)
    p_cap.add_argument("--importance", type=float, default=None)
    p_cap.add_argument("--raw-only", action="store_true", help="只归档原文，不逐段进记忆库")

    p_serve = sub.add_parser("serve", help="启动可视化面板")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--no-browser", action="store_true")

    p_home = sub.add_parser("home", help="查看 / 迁移记忆库目录")
    p_home.add_argument("action", nargs="?", default="show",
                        choices=["show", "verify", "move"])
    p_home.add_argument("dest", nargs="?", default="", help="move：目标目录")

    p_bak = sub.add_parser("backup", help="打包备份整个记忆库")
    p_bak.add_argument("--out", default=None, help="目标目录或 zip 路径（默认 D:/brain-memory-backups 或桌面）")
    p_bak.add_argument("--keep", type=int, default=20, help="保留最近 N 份，删除更旧的（0=不修剪）")

    p_arch = sub.add_parser("archive", help="冷热归档：把沉眠记忆雪藏出热查询集（原文保留，可随时回捞）")
    p_arch.add_argument("action", nargs="?", default="show",
                        choices=["show", "search", "restore", "run", "count"])
    p_arch.add_argument("arg", nargs="?", default="", help="search：关键词 / restore：记忆 id")
    p_arch.add_argument("--limit", type=int, default=20)
    p_arch.add_argument("--dry-run", action="store_true", help="run：只预览可归档的，不落盘")

    p_res = sub.add_parser("restore", help="从备份恢复记忆库（恢复前自动留现状备份）")
    p_res.add_argument("archive", help="备份 zip 路径")
    p_res.add_argument("--yes", action="store_true", help="跳过确认")

    p_st = sub.add_parser("startup", help="会话开始钩子：注入记忆。返回码 0=正常 10=只读降级 2=未初始化")
    p_st.add_argument("--query", default="", help="本次会话主题关键词")
    p_st.add_argument("--budget", type=int, default=None)

    p_sync = sub.add_parser("sync", help="灵枢日志摘要同步进 DSH 记忆")
    p_sync.add_argument("target", nargs="?", default="dsh", choices=["dsh"])
    p_sync.add_argument("--dir", default="", help="DSH 记忆目录（默认 DSH_MEMORY_HOME / ~/.dsh-memory / 工作区 .workbuddy、.codebuddy）")
    p_sync.add_argument("--days", type=int, default=1, help="取最近 N 天的灵枢日志")
    p_sync.add_argument("--force", action="store_true", help="覆盖今日已存在的同步块")
    p_sync.add_argument("--dry-run", action="store_true", help="只预览，不写入")

    args = parser.parse_args(argv)

    # ---- 只读降级：需要写盘的命令挡下来，读命令放行 ----
    if READONLY:
        action = getattr(args, "action", None)
        writing = (
            args.cmd in ("remember", "init", "restore", "forget", "capture")
            or (args.cmd == "dream" and not args.dry_run)
            or (args.cmd == "work" and action in ("add", "done", "flush", "clear"))
            or (args.cmd == "persona" and action not in ("show", "path", "onboarding",
                                                         "history", "snapshots", "drift"))
            or (args.cmd == "mind" and action in ("set", "tune"))
            or (args.cmd == "emotion" and action in ("feel", "evaluate", "mirror",
                                                     "adjust", "del", "cool"))
            or (args.cmd == "emotion" and action == "desire" and bool(args.emotion))
            or (args.cmd == "emotion" and action == "config"
                and any([args.empathy, args.gain is not None, args.max_intensity is not None,
                         args.suppress, args.notes]))
            or (args.cmd == "mode" and action != "show")
            or (args.cmd == "config" and action in ("set", "unset"))
            or (args.cmd == "sync" and not args.dry_run)
            or (args.cmd == "review" and action in ("confirm", "discard"))
            or (args.cmd == "archive" and (action == "restore"
                                           or (action == "run" and not args.dry_run)))
            or (args.cmd == "assoc" and action in ("link", "unlink") )
            or (args.cmd == "assoc" and action == "auto" and not args.dry_run)
            or (args.cmd == "daily" and not args.dry_run)
            or (args.cmd == "usage")
            or (args.cmd == "user-rules" and action in ("add", "clear"))
        )
        if writing:
            print(f"[只读] 记忆库当前不可写，{args.cmd} 需要写盘。"
                  "改用 --home 指向可写目录，或先解除写保护。", file=sys.stderr)
            return 3

    if args.cmd == "init":
        ensure_home()
        if args.force:
            # 重置人格是最伤数据的一步：先把当前档案留一份副本。
            # 之前直接 save_persona(default_persona()) 覆盖，人格/初识/演化历史当场没。
            if (HOME / "persona.json").exists():
                bak = HOME / f"persona.json.pre-init-{now_ts()}"
                try:
                    shutil.copy2(HOME / "persona.json", bak)
                    print(f"旧人格档案已备份：{bak}（后悔了可手动改回）")
                except OSError as exc:
                    print(f"[警告] 备份旧人格档案失败：{exc}", file=sys.stderr)
            save_persona(default_persona())
        p = load_persona()
        print(f"记忆库已就绪：{HOME}")
        print(f"人格档案：{HOME / 'persona.json'}")
        if not p.get("onboarding", {}).get("completed"):
            print("还没有初识：先问用户希望你叫什么、是什么性格；用户不知道就自己定。")
        print(persona_brief(p))
        return 0

    if args.cmd == "remember":
        rec, how = remember(" ".join(args.text), args.kind, args.importance,
                            [t for t in args.tags.split(",") if t],
                            [e for e in args.entities.split(",") if e],
                            args.source, args.layer,
                            emotion=(args.emotion or None))
        print(f"{'已合并' if how == 'merged' else '已写入'}：{rec['id']} ({rec['layer']}, 重要性 {rec['importance']})")
        if rec.get("review") == "pending":
            print("  ⚠ 重要性高，已进待审池——用户确认前不当既定事实引用（brain.py review list 查看）")
        if rec.get("emotion"):
            print(f"  情绪标记：{rec['emotion']}（唤醒 {rec.get('emotional', '?')}，记得更牢）")
        print(f"  {rec['gist']}")
        return 0

    if args.cmd == "capture":
        payload = " ".join(args.payload)
        res = capture(payload, tag=args.tag, kind=args.kind,
                      importance=args.importance, raw_only=args.raw_only)
        print(f"已封存 {res['chars']} 字：进记忆库 {len(res['saved'])} 段"
              + (f"，跳过重复 {res['skipped']} 段" if res["skipped"] else ""))
        if res["archive"]:
            print(f"  原文归档：{res['archive']}")
        else:
            print("  （--raw-only：只归档，未逐段进记忆库）")
        print("  这些是原文快照，做梦会压缩它们但不会淡化或丢弃。")
        return 0

    if args.cmd == "recall":
        query = " ".join(args.query)
        if not query:
            for m in recent(args.k):
                _print_mem(m)
            return 0
        results = recall(query, args.k, include_archive=args.include_archive)
        record_retrieval(query, results, channel="recall")
        if args.json:
            print(json.dumps([{"memory": m, "score": s, "parts": p} for m, s, p in results], ensure_ascii=False, indent=2))
        else:
            print(f"回忆「{query}」→ {len(results)} 条\n")
            for m, s, parts in results:
                _print_mem(m, s, parts)
        return 0

    if args.cmd == "usage":
        r = evaluate_retrieval_usage(args.reply)
        if r.get("error"):
            print(r["error"], file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0
        print(f"回忆使用核对（{r['channel'] or 'recall'}）：{r['used']}/{r['recalled']} 条用上"
              f"（{r['rate']:.0%}）" + (f" · 查询「{r['query']}」" if r.get("query") else ""))
        for it in r["items"]:
            mark = "✓" if it["used"] else "·"
            hits = "、".join(it.get("hits") or []) or "无重叠"
            print(f"  {mark} {it['id']}  共享 {it['shared']} 个词元（{it['ratio']:.0%}）：{hits}")
        return 0

    if args.cmd == "inject":
        print(build_injection(" ".join(args.query), args.budget))
        return 0

    if args.cmd == "dream":
        if args.auto:
            res = auto_dream(args.trigger, args.dry_run)
            if res.get("applied"):
                print(f"自动做梦完成：新增长期记忆 {res['memories']} 条，遗忘 {res['forgotten']} 条，"
                      f"{res.get('clusters', 0)} 组碎片已合并。")
                print("（启发式合并，未动人格；需要更抽象时可人工复核梦报告并 dream --apply）")
            elif res.get("preview"):
                print(json.dumps(res["preview"], ensure_ascii=False, indent=2))
                print("\n（--dry-run 预览：将这样合并并沉淀）")
            else:
                print("做梦完成，但没有需要压缩的碎片（梦很安静）。")
            return 0
        if args.apply:
            if READONLY:
                print("[只读] 记忆库当前只读，无法沉淀梦境结果。", file=sys.stderr)
                return 3
            path = Path(args.apply)
            if not path.exists():
                print(f"找不到整理结果文件：{path}", file=sys.stderr)
                return 2
            try:
                result = load_loose_json(path, "dream --apply 的结果")
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
            if args.dry_run:
                print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])
                print("\n（--dry-run：只预览将沉淀的内容，未落盘）")
                return 0
            applied = apply_dream(result)
            print(f"梦已沉淀：新增长期记忆 {applied['memories']} 条，遗忘 {applied['forgotten']} 条")
            if applied["rejected"]:
                print(f"被拒绝的人格修改：{'、'.join(applied['rejected'])}")
            return 0
        report = dream(args.trigger, args.dry_run)
        print(f"做梦完成（{args.trigger}）")
        print(f"  重放 {sum(len(c['texts']) for c in report['clusters'])} 条 / {len(report['clusters'])} 组")
        print(f"  巩固 {len(report['promoted'])} 条，遗忘 {len(report['forgotten'])} 条，模糊化 {report['faded']} 条")
        if not args.dry_run:
            print(f"\n待整理任务：{HOME / 'dreams' / 'pending.json'}")
            print("把下面的碎片压缩成抽象长期记忆，写成 apply.json 后执行：")
            print("  python scripts/brain.py dream --apply apply.json\n")
            for i, c in enumerate(report["clusters"], 1):
                tags = "、".join(c["tags"][:4]) or "无标签"
                print(f"[{i}] {tags}")
                for t in c["texts"][:6]:
                    print(f"    - ({t['kind']}) {t['text'][:80]}")
            if not report["clusters"]:
                print("（没有需要压缩的碎片，梦很安静）")
        return 0

    if args.cmd == "persona":
        if args.action == "sync":
            p = compile_persona()
            print("人格档案已整理（保留原有设定）。")
            print(persona_brief(p))
            return 0
        if args.action == "path":
            print(HOME / "persona.json")
            return 0
        if args.action == "set":
            if not args.pair or "=" not in args.pair:
                print("用法：persona set traits.幽默=70", file=sys.stderr)
                return 2
            key, val = args.pair.split("=", 1)
            try:
                p = set_persona_field(key.strip(), val.strip())
            except PermissionError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"已设置 {key}，已写入演化历史。")
            print(persona_brief(p))
            return 0
        if args.action in ("lock", "unlock"):
            item = (args.pair or "").strip()
            if not item:
                print("用法：persona lock 名字", file=sys.stderr)
                return 2
            with _locked(HOME / "persona.json"):
                p = load_persona()
                locks = p.setdefault("locked", [])
                if args.action == "lock" and item not in locks:
                    locks.append(item)
                if args.action == "unlock" and item in locks:
                    locks.remove(item)
                p.setdefault("history", []).append({
                    "ts": now_ts(), "field": "locked",
                    "from": None, "to": item, "why": "用户要求" if args.action == "lock" else "解除锁定",
                })
                save_persona(p)
            print(f"{'已锁定' if args.action == 'lock' else '已解锁'}：{item}")
            return 0
        if args.action == "history":
            h = persona_history(getattr(args, "limit", 30) or 30)
            if args.json:
                print(json.dumps(h, ensure_ascii=False, indent=2))
            elif not h:
                print("还没有人格改动历史。")
            else:
                print(f"人格改动历史（最新在前，共 {len(h)} 条）：")
                print("  序号 = 从 1 起往前数；想撤销哪条就 persona rollback <序号>")
                for i, e in enumerate(h, 1):
                    print(f"  {i}. {fmt_time(e.get('ts', 0))}  {e.get('field')}  "
                          f"{e.get('from') if e.get('from') is not None else '∅'} → "
                          f"{e.get('to') if e.get('to') is not None else '∅'}"
                          f"（{e.get('why', '')}）")
            return 0
        if args.action == "rollback":
            idx = None
            if args.pair:
                try:
                    idx = int(args.pair.strip())
                except ValueError:
                    print("序号得是数字：persona rollback <序号>", file=sys.stderr)
                    return 2
            try:
                r = persona_rollback(idx)
            except (ValueError, PermissionError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"已回滚 {r['field']}：{r['from'] if r['from'] is not None else '∅'} → "
                  f"{r['to'] if r['to'] is not None else '∅'}（原来是「{r['why']}」）")
            if r.get("overwrote_later"):
                print("  注意：这个字段之后又被改过，本次回滚已把后来的改动一并覆盖。")
            print(persona_brief(load_persona()))
            return 0
        if args.action == "snapshot":
            r = persona_snapshot(args.label)
            print(f"已给当前人格拍快照：{r['label']}（{fmt_time(r['ts'])}）")
            print(f"  文件：{r['path']}")
            print("以后人格跑偏，用 persona snapshots 找回来、persona restore <名字> 还原。")
            return 0
        if args.action == "snapshots":
            rows = persona_snapshots(getattr(args, "limit", 30) or 30)
            if not rows:
                print("还没有人格快照。先拍一张：persona snapshot --label \"调参前基准\"")
                return 0
            print(f"人格快照（最新在前，共 {len(rows)} 张）：")
            for i, r in enumerate(rows, 1):
                print(f"  {i}. {fmt_time(r['ts'])}  {r['label']}  [{r['name']}]")
            print("还原：persona restore <名字>")
            return 0
        if args.action == "restore":
            if not args.pair:
                print("用法：persona restore <快照名字>（用 persona snapshots 看名字）", file=sys.stderr)
                return 2
            try:
                r = persona_restore(args.pair)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"已从快照恢复人格：{r['label']}（{fmt_time(r['ts'])}）")
            print(persona_brief(load_persona()))
            return 0
        if args.action == "drift":
            d = persona_drift(getattr(args, "days", 7) or 7)
            if args.json:
                print(json.dumps(d, ensure_ascii=False, indent=2))
                return 0
            if not d["baseline"]:
                print("还没有快照做基准。先拍一张：persona snapshot --label \"基准\"")
            else:
                print(f"对比基准快照：{d['baseline']}（{fmt_time(d['baseline_ts'])}）")
            if not d["changes"] and not d["ai_changes"]:
                print("没有检测到人格漂移。")
                return 0
            if d["changes"]:
                print(f"结构变化 {len(d['changes'])} 处：")
                for c in d["changes"]:
                    print(f"  · {c['section']}.{c['key']}：{c['from']} → {c['to']}")
            if d["ai_changes"]:
                print(f"最近 {getattr(args, 'days', 7) or 7} 天 AI 改人格 {len(d['ai_changes'])} 次：")
                for h in d["ai_changes"]:
                    src = f"（来源 {h['source']}）" if h.get("source") else ""
                    print(f"  · {fmt_time(h['ts'])}  {h['field']}：{h['from']} → {h['to']}"
                          f"（{h['why']}{src}）")
            if d["alert"]:
                print(" ⚠ 漂移偏大：确认这些变化是不是你想要的；不是就 persona rollback 或 persona restore 快照。")
            return 0
        if args.action == "interview":
            p = persona_interview(args.name, args.traits, args.style, args.values,
                                  args.callme, args.self_name, args.unsure)
            if args.unsure or not (args.name or "").strip():
                print("已记下：用户不知道自己想要什么名字/性格，这部分由你自己决定。")
            else:
                print("已记下用户给你的名字与性格。")
            print(persona_brief(p))
            print("\n现在轮到你自己：名字/性格没定的自己定，再写喜欢、讨厌、想养成的习惯。")
            print('  python scripts/brain.py persona self --name "..." --traits "..." --likes "..." --dislikes "..." --habits "..."')
            return 0
        if args.action == "self":
            p = persona_self_author(args.name, args.traits, args.likes, args.dislikes,
                                    args.habits, args.insights, args.style, args.values,
                                    args.callme, args.self_name)
            print("初识完成：名字、性格与喜好都是你自己定下的，已进入演化历史。")
            print(persona_brief(p))
            return 0
        if args.action == "onboarding":
            st = onboarding_state()
            if args.json:
                print(json.dumps(st, ensure_ascii=False, indent=2))
            else:
                stage = st["stage"]
                print(f"初识阶段：{stage}")
                if stage == "done":
                    print(f"已完成。名字：{st['name'] or '（未命名）'}")
                elif stage == "awaiting_self":
                    print("用户已回答（或说了不知道）。下一步：名字/性格没定的自己定，再写喜欢/讨厌/习惯。")
                else:
                    print("还没问用户。要问两件事：希望你叫什么名字？性格是什么样的？用户不知道就自己定。")
            return 0
        p = load_persona()
        if args.json:
            print(json.dumps(p, ensure_ascii=False, indent=2))
        else:
            print(persona_brief(p))
            print(f"\n人格档案：{HOME / 'persona.json'}")
        return 0

    if args.cmd == "mind":
        if args.action == "set":
            dr = mind_set(args.curiosity, args.reason, why="手动设定")
            print("已设定：" + " / ".join(
                f"{'好奇心' if k == 'curiosity' else '理智'} {dr.get(k, '未成形')}" for k in DRIVE_KEYS))
            return 0
        if args.action == "tune":
            if args.curiosity is None and args.reason is None:
                print("用法：mind tune --curiosity +5 --reason -3 --why \"用户嫌我啰嗦\"", file=sys.stderr)
                return 2
            dr = mind_tune(args.curiosity or 0, args.reason or 0, why=args.why)
            print("已按用户态度调整：" + " / ".join(
                f"{'好奇心' if k == 'curiosity' else '理智'} {dr.get(k, '未成形')}" for k in DRIVE_KEYS))
            print(f"  原因：{args.why}")
            return 0
        if args.action == "weigh":
            if not args.topic:
                print("用法：mind weigh \"这件事\" --benefit 7 --cost 3 --confidence 0.6", file=sys.stderr)
                return 2
            r = mind_weigh(args.topic, args.benefit, args.cost, args.confidence, args.urgency)
            print(f"事项：{r['topic']}")
            print(f"  好处 {r['benefit']:g} × 把握 {r['confidence']:.0%}，坏处/代价 {r['cost']:g}")
            print(f"  净收益 {r['net']:+.1f}，收益代价比 {r['ratio']:.1f}")
            cur = r["curiosity"] if r["curiosity"] is not None else "未成形"
            rea = r["reason"] if r["reason"] is not None else "未成形"
            print(f"  理智 {rea} / 好奇心 {cur} → 深度：{r['depth']}")
            print(f"  结论：{r['verdict']}")
            for line in r["reasoning"]:
                print(f"  - {line}")
            return 0
        dr = mind_state()
        if dr["formed"]:
            print("好奇心 {} / 理智 {}".format(
                dr["curiosity"] if dr["curiosity"] is not None else "未成形",
                dr["reason"] if dr["reason"] is not None else "未成形"))
        else:
            print("好奇心和理智还没有数值——它们由用户的态度长出来，不是出厂设定。")
        print("理智先算收益与代价，决定值不值得深想；好奇心只在值得时点火。")
        print("  python scripts/brain.py mind weigh \"这件事\" --benefit 7 --cost 3")
        print("  python scripts/brain.py mind tune --curiosity +5 --reason -3 --why \"用户嫌我啰嗦\"")
        return 0

    if args.cmd == "emotion":
        if args.action == "evaluate":
            if not args.emotion:
                print("用法：emotion evaluate \"用户骂我了\" --polarity auto --intensity 0.8", file=sys.stderr)
                return 2
            r = emotion_evaluate(args.emotion, args.polarity,
                                 args.intensity if args.intensity is not None else 0.6,
                                 why=args.why, about=args.about)
            ra = r["rationality"]
            print(f"事件：{r['event']}")
            print(f"评价：{r['polarity']} · 分量 {r['importance']:.0%} → 情绪「{r['emotion']}」"
                  f"（{r['record']['intensity']:.0%}）")
            print(f"理智：{ra['base']} → {ra['effective']}（被情绪压住 {ra['suppress']}% · {r['suppressed_state']}）")
            if r["decision"] == "user":
                print(f"【重大】{r['advice'] or '这件事的分量够重：取舍留给用户决定，我只如实表达感受。'}")
            else:
                print("（分量尚轻，我可以自己把握语气与分寸。）")
            if r["record"].get("suppressed_by"):
                print(f"（这条情绪被情感设置抑制过：{r['record']['suppressed_by']}）")
            return 0
        if args.action == "mirror":
            if not args.emotion:
                print("用法：emotion mirror \"用户很难过\" [--intensity 0.6]", file=sys.stderr)
                return 2
            r = emotion_mirror(args.emotion,
                               args.intensity if args.intensity is not None else 0.5)
            print(r["note"])
            if r.get("mirrored"):
                print(f"  记进情绪流：{r['mirrored']['emotion']}（{r['mirrored']['intensity']:.0%}）")
            print(f"  此刻心情：{r['mood']['label']}（valence {r['mood']['valence']:+.2f}）")
            return 0
        if args.action == "config":
            updates = {}
            if args.empathy:
                updates["empathy"] = {"level": args.empathy}
            if args.gain is not None:
                updates["empathy"] = dict(updates.get("empathy", {}), gain=max(0.0, min(1.0, args.gain)))
            if args.max_intensity is not None:
                updates["empathy"] = dict(updates.get("empathy", {}), max_intensity=max(0.0, min(1.0, args.max_intensity)))
            if args.suppress:
                updates["suppressed"] = [s for s in args.suppress.split(",") if s.strip()]
            if args.notes:
                updates["notes"] = args.notes
            if not updates:
                cfg = emotion_cfg()
                emp = cfg.get("empathy", {})
                print("情感设置：")
                print(f"  共情：{emp.get('level', 'mirror')}（跟进比例 {emp.get('gain', 0.5)}，上限 {emp.get('max_intensity', 0.8)}）")
                print(f"  抑制的情绪：{'、'.join(cfg.get('suppressed', [])) or '无'}")
                print(f"  备注：{cfg.get('notes') or '无'}")
                print("  改：emotion config --empathy mild --gain 0.3 --suppress 嫉妒,愤怒 --notes \"别太夸张\"")
                return 0
            cfg = save_emotion_cfg(updates)
            emp = cfg.get("empathy", {})
            print("已更新情感设置：")
            print(f"  共情：{emp.get('level', 'mirror')}（跟进比例 {emp.get('gain', 0.5)}，上限 {emp.get('max_intensity', 0.8)}）")
            print(f"  抑制的情绪：{'、'.join(cfg.get('suppressed', [])) or '无'}")
            print(f"  备注：{cfg.get('notes') or '无'}")
            return 0
        if args.action == "feel":
            if not args.emotion:
                print("用法：emotion feel 开心 --intensity 0.7 --why \"用户夸我了\"", file=sys.stderr)
                return 2
            ev = feel(args.emotion,
                      args.intensity if args.intensity is not None else 0.6,
                      why=args.why, about=args.about,
                      decay_hours=args.decay_hours)
            m = mood_now()
            print(f"记下：{ev['emotion']}（强度 {ev['intensity']:.0%}）"
                  + (f"，因为{ev['why']}" if ev["why"] else ""))
            if ev.get("resonance") == "amplified":
                print("  情绪传染：和此刻的心情同向，被共振放大了一点")
            elif ev.get("resonance") == "damped":
                print("  心情惯性：跟此刻的心情拧着劲，被往回拽了一点")
            if ev.get("habituation"):
                print(f"  习惯化：同一件事在最近 12 小时已是第 {ev['habituation'] + 1} 次出现，反应淡了一点")
            print(f"此刻心情：{m['label']}，valence {m['valence']:+.2f} / arousal {m['arousal']:.2f}，"
                  f"强度 {m['intensity']:.0%}")
            if m["active"]:
                print("  还在起作用：" + "、".join(f"{x['emotion']}({x['weight']:.0%})" for x in m["active"]))
            return 0
        if args.action == "arc":
            a = emotion_arc(args.days)
            if a["count"] == 0:
                print(a["summary"])
                return 0
            print(f"最近 {a['days']} 天 · {a['count']} 次情绪波动 · {a['tone']}")
            print("  出现最多：" + "、".join(f"{x['emotion']}×{x['times']}" for x in a["top"]))
            print(f"  平均 valence {a['avg_valence']:+.2f}")
            pk, en = a.get("peak") or {}, a.get("end") or {}
            if pk.get("emotion"):
                print(f"  印象最深（峰）：{pk['emotion']}，强度 {float(pk.get('intensity', 0)):.0%}"
                      + (f"，因为{pk['why']}" if pk.get("why") else ""))
            if en.get("emotion"):
                print(f"  最近一次（终）：{en['emotion']}"
                      + (f"，因为{en['why']}" if en.get("why") else ""))
            print(f"  （{a['first']} ~ {a['last']}）")
            return 0
        if args.action == "list":
            print("七情六欲 · 可选情绪（归类如下）：")
            for g, names in EMOTION_GROUPS.items():
                print(f"  【{g}】{'、'.join(names)}")
            print("\n六欲（底层需要，会随时间变淡，被满足才回升）：")
            for name, hint in DESIRES.items():
                print(f"  · {name}：{hint}")
            print("\n用法：emotion feel 难过 --why \"...\"  /  emotion desire 连接欲 --amount 20")
            return 0
        if args.action == "desire":
            ds = desire_state()
            if not args.emotion:
                for name, lvl in ds.items():
                    mark = "↓" if lvl < 35 else ("↑" if lvl >= 75 else "·")
                    print(f"  {mark} {name}：{lvl:.0f}  {DESIRES[name]}")
                low = desire_low()
                if low:
                    low_names = "、".join(f"{x['desire']}({x['level']:.0f})" for x in low)
                    print(f"\n  长期没被满足：{low_names}")
                print("  满足一笔：python scripts/brain.py emotion desire 连接欲 --amount 20")
                return 0
            try:
                r = desire_satisfy(args.emotion, args.amount)
            except PermissionError as e:
                print(str(e), file=sys.stderr)
                return 1
            print(f"满足 {r['satisfied']} +{r['amount']}：现在是 {r['desires'][r['satisfied']]:.0f}/100")
            return 0
        if args.action == "events":
            rows = emotion_events()
            if not rows:
                print("还没有情绪事件。")
                return 0
            print(f"情绪事件流（最新在前，共 {len(rows)} 条；序号用于 adjust / del）：")
            for i, ev in enumerate(rows[:30], 1):
                why = f"，因为{ev['why']}" if ev.get("why") else ""
                about = f"（关于{ev['about']}）" if ev.get("about") else ""
                mark = " ·已微调" if ev.get("adjusted") else (" ·已冷却" if ev.get("cooled_at") else "")
                print(f"  {i}. {fmt_time(ev.get('ts', 0))}  {ev.get('emotion')}"
                      f"（强度 {float(ev.get('intensity', 0)):.0%}）{why}{about}{mark}")
            print("\n判错了不用整条删：emotion adjust <序号> --intensity 0.3 --emotion-name 委屈 --why \"其实还好\"")
            return 0
        if args.action == "adjust":
            if not args.emotion:
                print("用法：emotion adjust <序号> [--intensity 0.3] [--emotion-name 委屈] [--why \"其实还好\"]", file=sys.stderr)
                return 2
            try:
                n = int(args.emotion.strip())
            except ValueError:
                print("序号得是数字：emotion adjust <序号>（先 emotion events 看序号）", file=sys.stderr)
                return 2
            if (args.intensity is None and not args.emotion_name and not args.why
                    and not args.about and args.decay_hours is None):
                print("至少要改一项：--intensity / --emotion-name / --why / --about / --decay-hours", file=sys.stderr)
                return 2
            try:
                r = emotion_modify(n, emotion=args.emotion_name,
                                   intensity=args.intensity,
                                   why=args.why or None, about=args.about or None,
                                   decay_hours=args.decay_hours)
            except (ValueError, PermissionError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            ev = r["event"]
            print(f"已微调第 {n} 条：{ev.get('emotion')}（强度 {float(ev.get('intensity', 0)):.0%}）")
            if r["changed"]:
                print("  改了：" + "、".join(r["changed"].keys()))
            return 0
        if args.action == "del":
            if not args.emotion:
                print("用法：emotion del <序号>（先 emotion events 看序号）", file=sys.stderr)
                return 2
            try:
                n = int(args.emotion.strip())
            except ValueError:
                print("序号得是数字：emotion del <序号>", file=sys.stderr)
                return 2
            try:
                r = emotion_delete(n)
            except (ValueError, PermissionError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"已删除第 {n} 条：{r['deleted'].get('emotion')}；还剩 {r['remaining']} 条。")
            return 0
        if args.action == "cool":
            try:
                r = emotion_cool()
            except PermissionError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"已冷却 {r['cooled']} 条情绪余波。此刻心情：{r['mood']}（强度 {r['intensity']:.0%}）。")
            print("刚聊完情绪化话题要写代码 / 做正事前，先来这一步，旧情绪就不再干扰工作会话。")
            return 0
        if args.action == "check":
            if not args.emotion:
                print("用法：emotion check \"<你刚发的回复>\" —— 把内部情绪状态和回复语气做一致性核对",
                      file=sys.stderr)
                return 2
            r = emotion_consistency_check(args.emotion)
            print(f"此刻内部情绪：{r['mood_label']}（valence {r['mood_valence']:+.2f}，强度 {r['mood_intensity']:.0%}）")
            print(f"回复语气极性：{r['reply_valence']:+.2f}")
            if r["alert"]:
                print(f"[一致性告警] {r['reason']}")
                print("建议：要么先处理内部情绪（emotion cool / feel 说清楚），要么把回复语气拉回与情绪一致，别让字面与状态拧着。")
            else:
                print("未见明显背离：内部情绪与回复语气基本一致。")
            return 0
        m = mood_now()
        grp = f" · 七情[{m['category']}]" if m.get("groups") else ""
        print(f"此刻心情：{m['label']}（valence {m['valence']:+.2f}，arousal {m['arousal']:.2f}，"
              f"强度 {m['intensity']:.0%}{grp}）"
              + (" · 情绪上头" if m.get("storm") else "")
              + (" · 悲喜交加" if m.get("complex") else ""))
        if not m["active"]:
            print("  情绪已散去，现在是中性状态。")
        else:
            print("  还在起作用：")
            for x in m["active"]:
                why = f"，因为{x['why']}" if x.get("why") else ""
                print(f"    · [{x['group']}] {x['emotion']}（{x['weight']:.0%}）{why}")
        ra = rationality()
        if ra["storm"] or ra["suppress"] >= 10:
            print(f"  理智：{ra['base']} → {ra['effective']}（被情绪压住 {ra['suppress']}%）。"
                  "涉及大事时把决定权留给用户。")
        low = desire_low()
        if low:
            print("  欲望在喊："
                  + "、".join(f"{x['desire']}({x['level']:.0f})" for x in low))
        print("  记一笔：python scripts/brain.py emotion feel 开心 --why \"...\"")
        print("  自动评价：python scripts/brain.py emotion evaluate \"事件\" --polarity auto")
        print("  共情：python scripts/brain.py emotion mirror \"用户的情绪\"")
        print("  情感设置：python scripts/brain.py emotion config --empathy mild")
        print("  看全表：python scripts/brain.py emotion list")
        return 0

    if args.cmd == "assoc":
        if args.action == "link":
            if len(args.ids) != 2:
                print("用法：assoc link <记忆A的id> <记忆B的id> --relation 因果 --why \"...\"", file=sys.stderr)
                return 2
            r = link_memory(args.ids[0], args.ids[1], args.relation, args.weight, args.why)
            print(f"已关联 {r['src']} ↔ {r['dst']}（{r['relation']}，权重 {r['weight']}）"
                  + ("，反复连，权重加强了" if not r["created"] else ""))
            return 0
        if args.action == "unlink":
            if len(args.ids) != 2:
                print("用法：assoc unlink <idA> <idB>", file=sys.stderr)
                return 2
            ok = unlink_memory(args.ids[0], args.ids[1])
            print("已断开。" if ok else "本来就没有这条关联。")
            return 0 if ok else 1
        if args.action == "auto":
            r = assoc_auto(dry_run=args.dry_run)
            print(f"自动联想：{('计划建立' if args.dry_run else '建立')} {r['added']} 条边"
                  f"（共 {r['planned']} 对候选）")
            for a, b in r["pairs"][:10]:
                print(f"  {a} ↔ {b}")
            if not args.dry_run and r["added"]:
                print("关联多了，面板「联想」页能看出网络长什么样。")
            return 0
        if args.action == "of":
            if not args.ids:
                print("用法：assoc of <记忆id>", file=sys.stderr)
                return 2
            mid = args.ids[0]
            nb = assoc_of(mid)
            if not nb:
                print("这条记忆还没有显式关联。")
                return 0
            print(f"「{mid}」的邻居：")
            for n in nb[:15]:
                print(f"  · [{n['relation']} x{n['weight']}] {n['id']}  {n['gist'][:50]}")
            return 0
        g = assoc_graph(args.limit)
        print(f"联想图谱：{len(g['nodes'])} 个节点 · {len(g['edges'])} 条关联边 · "
              f"{g['connected']} 个节点被连成网")
        if not g["edges"]:
            print("还没有显式关联。先手动 link，或跑 assoc auto 让共享实体/标签自动成边。")
        else:
            print("在面板的「联想」页里，这就是一个可旋转的 3D 空间。")
            print("  手动关联：assoc link <idA> <idB> --relation 因果 --why \"...\"")
            print("  自动补网：assoc auto")
            print("  看邻居：assoc of <记忆id>")
        return 0

    if args.cmd == "daily":
        r = daily(args.dry_run, args.force, args.feel)
        if r.get("skipped"):
            print(r["reason"])
            if r.get("feelings_added"):
                print(f"（顺手记下了 {r['feelings_added']} 条情绪事件；日记今天已写过，明天一起计入）")
            if r.get("feelings_bad"):
                print("  没记的情绪：" + "；".join(r["feelings_bad"]), file=sys.stderr)
            return 0
        if args.dry_run:
            print(f"[预览] {r['date']} 的每日例行：")
            print(r["note_body"])
            if r.get("feelings_added") is not None:
                print(f"\n将顺手记下 {len(args.feel)} 条情绪事件"
                      + (f"；无法识别：{'；'.join(r['feelings_bad'])}" if r.get("feelings_bad") else ""))
            print(f"\n将写入知识库 {r['knowledge_added']} 条新增 / {r['knowledge_merged']} 条合并"
                  + (f"；距上次做梦超时，将自动做梦" if r["dreamed"] == "would_run" else ""))
            return 0
        print(f"每日例行完成（{r['date']}）")
        print(f"  今日记忆 {r['memories_today']} 条，日记已写（含今日情绪与理智状态）")
        if r.get("feelings_added"):
            print(f"  顺手记下 {r['feelings_added']} 条情绪事件")
        if r.get("feelings_bad"):
            print("  没记的情绪：" + "；".join(r["feelings_bad"]), file=sys.stderr)
        print(f"  知识库：新增 {r['knowledge_added']} 条，合并 {r['knowledge_merged']} 条")
        print(f"  做梦：{'已自动做掉' if r['dreamed'] else '还没到 24 小时，不急'}。")
        print(f"  今日心情：{r['mood']}")
        print("（每天只跑一次；要重跑加 --force）")
        return 0

    if args.cmd == "work":
        if args.action == "add":
            text = " ".join(args.text).strip()
            if not text:
                print("用法：work add \"先记着这件事\" --tag 任务 --why \"怕等下忘了\"", file=sys.stderr)
                return 2
            rows = work_add(text, args.tag, args.why)
            print(f"已写入会话暂存（{len(rows)}/{WORKING_CAPACITY}）。它不是长期记忆，{int(WORKING_TTL / HOUR)} 小时后自动丢。")
            return 0
        if args.action == "done":
            text = " ".join(args.text).strip()
            if not text:
                print("用法：work done 2  （暂存里的第 2 条已完成）", file=sys.stderr)
                return 2
            try:
                idx = int(text.split()[0])
            except ValueError:
                print("序号得是数字：work done <第几条>", file=sys.stderr)
                return 2
            # 先校验序号、后执行：越界时不能先标记完成再报错（先执行后校验会有副作用）
            cur = work_status()
            if idx < 1 or idx > cur["count"]:
                print(f"序号越界：当前暂存只有 {cur['count']} 条。", file=sys.stderr)
                return 2
            rows = work_done(idx)
            done = rows[idx - 1]
            print(f"第 {idx} 条已标记完成：{done.get('text', '')}——做完的事不再占心思。")
            return 0
        if args.action == "flush":
            res = work_flush(args.why or "会话结束前决定留下", skip_done=args.skip_done)
            saved, dropped = res["saved"], res["dropped"]
            if not saved and not dropped:
                print("暂存是空的，没有要转的。")
                return 0
            if saved:
                print(f"已转成 {len(saved)} 条记忆：")
                for s in saved:
                    print(f"  {s['id']}  {s['text']}")
            if dropped:
                print(f"另有 {dropped} 条已完成，直接丢弃、没再落盘（做完的事就该放下）。")
            return 0
        if args.action == "clear":
            work_clear()
            print("已清空会话暂存。上下文里的东西本来就不在磁盘上。")
            return 0
        st = work_status()
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return 0
        print("工作记忆 = 你当前能看见的上下文。它不落盘、不参与回忆、不做梦。")
        print(f"暂存：{st['count']}/{st['capacity']} 条"
              f"（其中未完成 {st.get('open_count', 0)} 条），{st['ttl_hours']} 小时后过期。")
        for i, w in enumerate(st["items"], 1):
            tag = f"[{w['tag']}] " if w.get("tag") else ""
            mark = "✓" if not w.get("open", True) else " "
            print(f"  {i}. {mark} {tag}{w['text']}")
        print("  work add \"...\"   显式留一手（会话级）")
        print("  work done <序号>  这件做完了（放下它）")
        print("  work flush       转成真正的记忆（--skip-done 只转没完成的，已做完的直接丢）")
        print("  work clear       丢掉")
        return 0

    if args.cmd == "logs":
        lvl = None if args.all else args.level
        rows = read_logs(args.limit, level=lvl)
        if not rows:
            print("还没有运行日志。有锁冲突 / 内部异常 / 解析报错时，这里会显示原因。")
            return 0
        print(f"运行日志（最新 {len(rows)} 条，{'全部级别' if args.all else '≥' + args.level}）：")
        for r in rows:
            t = fmt_time(r.get("ts", 0)) if r.get("ts") else "??"
            print(f"  {t} [{r.get('level', 'info')}] {r.get('kind', '?')} · {r.get('msg', '')}")
        return 0

    if args.cmd == "stats":
        s = stats()
        if args.json:
            print(json.dumps(s, ensure_ascii=False, indent=2))
        else:
            print(f"记忆库：{s['home']}")
            print(f"  总记忆 {s['total']} 条（短期 {s['hippo']} / 长期 {s['cortex']} / 淡忘 {s['dust']}，置顶 {s['pinned']}）")
            print(f"  平均保留率 {s['avg_retention']:.0%}，人格：{s['persona_name']}，情绪：{s['mood']}")
            u = s.get("recall_usage") or {}
            if u.get("events"):
                print(f"  回忆使用率 {u['rate']:.0%}（{u['used']}/{u['recalled']} 条被用上，"
                      f"{u['events']} 次核对）")
            else:
                print("  回忆使用率：还没有核对数据（回复里用过找回的记忆后跑 `usage --reply \"...\"`）")
            print(f"  从没被命中过 {s['never_recalled_pct']:.0%}（{s['never_recalled']} 条）；"
                  f"满月且从未命中 {s['archive_hint']} 条，可考虑 archive")
            if s["last_dream"]:
                print(f"  上次做梦：{fmt_time(s['last_dream'])}")
            if s["top_tags"]:
                print("  高频标签：" + "、".join(f"{t}×{c}" for t, c in s["top_tags"]))
        return 0

    if args.cmd == "config":
        if args.action == "show":
            print(json.dumps(load_config(), ensure_ascii=False, indent=2))
            return 0
        if args.action == "set":
            if not args.key or not args.value:
                print("用法：config set model_tier small / config set budget_tokens 800", file=sys.stderr)
                return 2
            cfg = config_set(args.key, args.value)
            print(f"已设置 {args.key} = {json.dumps(cfg[args.key], ensure_ascii=False)}")
            return 0
        if args.action == "unset":
            if not args.key:
                print("用法：config unset <key>", file=sys.stderr)
                return 2
            config_unset(args.key)
            print(f"已移除 {args.key}")
            return 0

    if args.cmd == "mode":
        labels = {"work": "工程模式（情绪隔离、理性优先）",
                  "human": "拟人模式（情绪照常流露）",
                  "auto": "自动（按本轮话题判断，工程话题自动隔离情绪）"}
        if args.action == "explain":
            q = " ".join(args.query).strip()
            d = resolve_mode_detail(q)
            print(f"话题：{q or '（空）'}")
            print(f"判定结果：{d['mode']}（{'用户固定' if d['fixed'] else 'auto 自动判定'}）"
                  f" · 置信度 {d['confidence']:.0%}")
            if d["work_hits"]:
                print(f"  工程信号 {d['work_score']:.0f} 个：{'、'.join(d['work_hits'][:8])}")
            if d["human_hits"]:
                print(f"  情绪信号 {d['human_score']:.0f} 个：{'、'.join(d['human_hits'][:8])}")
            print(f"  依据：{d['reason']}")
            return 0
        if args.action in ("work", "human", "auto"):
            config_set("mode", args.action)
            print(f"会话模式 → {args.action}：{labels[args.action]}")
            return 0
        cur = str(load_config().get("mode", "auto") or "auto").lower()
        print(f"当前会话模式：{cur}（{labels.get(cur, '自动')}）")
        print("  work  = 恒为工程模式：情绪不注入，先给可执行方案")
        print("  human = 恒为拟人模式：情绪照常注入")
        print("  auto  = 按本轮话题自动切（默认）")
        print("切换：python scripts/brain.py mode work | human | auto")
        return 0

    if args.cmd == "review":
        if args.action in ("confirm", "discard"):
            if not args.id:
                print("用法：review confirm <id> / review discard <id>（先 review 看清单）", file=sys.stderr)
                return 2
            target = args.id.strip()
            if target.startswith("k_"):
                import growth
                status = "confirmed" if args.action == "confirm" else "discarded"
                r = growth.set_knowledge_status(target, status)
                if not r:
                    print(f"没有这条知识：{target}", file=sys.stderr)
                    return 1
                print(f"知识「{r.get('title', target)}」已{'确认' if args.action == 'confirm' else '废弃'}。")
                return 0
            try:
                r = set_memory_review(target, args.action)
            except (ValueError, PermissionError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"记忆已{'确认' if args.action == 'confirm' else '废弃'}："
                  f"{r.get('gist') or r.get('text', '')[:40]}")
            return 0
        rep = review_report()
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0
        total = len(rep["memories"]) + len(rep["knowledge"])
        if not total and not rep["persona"]:
            print("没有待审核项，干净。")
            return 0
        if rep["memories"]:
            print(f"待审核记忆 {len(rep['memories'])} 条（AI 做梦等整理出来的，确认前不当事实）：")
            for m in rep["memories"]:
                print(f"  · {m['id']}  {m['text'][:70]}")
                print(f"    review confirm {m['id']}   /   review discard {m['id']}")
        if rep["knowledge"]:
            print(f"\n待审核知识 {len(rep['knowledge'])} 条：")
            for k in rep["knowledge"]:
                print(f"  · {k['id']}  {k['title'][:50]}（{k['topic']}）")
                print(f"    review confirm {k['id']}   /   review discard {k['id']}")
        if rep["persona"]:
            print(f"\n最近 7 天 AI 改人格 {len(rep['persona'])} 次（persona history 看来源，跑偏就 rollback）：")
            for h in rep["persona"]:
                print(f"  · {h['field']}：{h['from']} → {h['to']}（{h['why']}）")
        return 0

    if args.cmd == "audit":
        a = audit()
        if args.json:
            print(json.dumps(a, ensure_ascii=False, indent=2))
            return 0
        c = a["counts"]
        print(f"自查：待审记忆 {c['memories']} · 待审知识 {c['knowledge']} · "
              f"AI 改人格 {c['persona']} · 疑似冲突 {c['conflicts']}")
        for t in a["tips"]:
            print(f"  - {t}")
        return 0

    if args.cmd == "detect-conflicts":
        hits = detect_conflicts(args.limit)
        if args.json:
            print(json.dumps(hits, ensure_ascii=False, indent=2))
        elif not hits:
            print("没发现明显矛盾的记忆对。")
        else:
            print(f"发现 {len(hits)} 组疑似矛盾（启发式提醒，请人工判断）：\n")
            for h in hits:
                print(f"· {h['reason']}")
                print(f"   A {h['a']['id']}：{h['a']['text'][:90]}")
                print(f"   B {h['b']['id']}：{h['b']['text'][:90]}\n")
            print("核实后处理：保留最新的 `remember` 一条即可，旧的用 `forget <id>` 或等它自然淡忘。")
        return 0

    if args.cmd == "list":
        rows = list_memories(args.limit, args.layer)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for m in rows:
                _print_mem(m)
        return 0

    if args.cmd == "forget":
        ok = forget(args.id)
        print("已淡忘（移入淡忘区，保留轮廓）。" if ok else "没找到这条记忆。")
        return 0 if ok else 1

    if args.cmd == "check-context":
        cfg = load_config()
        used = float(args.used)
        review = _cfg_num(cfg, "review_threshold", 0.60) * 100
        dream_pct = _cfg_num(cfg, "dream_threshold", 0.75) * 100
        escape = _cfg_num(cfg, "escape_threshold", 0.90) * 100

        print(f"上下文 {used:.0f}%（清点 {review:.0f}% / 做梦 {dream_pct:.0f}% / 封存 {escape:.0f}%）")

        if args.unsaved and args.unsaved > 0:
            print(f"  还有约 {args.unsaved:.0f} 字没落盘——滚出窗口就找不回来了，先补记/封存。")

        if used >= escape:
            print("  [封存] 快满了，先原样保存再说压缩：")
            print("    python scripts/brain.py capture \"<把这一段上下文粘进来>\" --tag 逃生 --kind episode")
            print("  （capture 原文落盘 + 归档，100% 不丢；做完再考虑做梦压缩）")
        elif used >= dream_pct:
            print("  [做梦] 该压缩了：")
            print("    1) python scripts/brain.py dream --auto            # 机器自愈：重放→启发式合并→沉淀")
            print("    2) 或人工三步：dream --trigger context → 填 apply.json → dream --apply apply.json")
        elif used >= review:
            print("  [清点] 先补记，别急着压缩——清点本轮哪些该记没记：")
            print("    · 约定 / 稳定偏好 / 身份事实   → remember --kind preference|fact --importance 8")
            print("    · 情绪高点 / 关系推进           → remember --kind emotion --importance 6")
            print("    · 你自己想明白的道理           → remember --kind insight --importance 7")
            print("    · 一次性过程细节，怕丢          → capture \"<原样粘贴>\" --tag 会话")
        else:
            print("  还早。但即记即存：约定、情绪高点、里程碑当时就 remember，不攒到做梦。")
        return 0

    if args.cmd == "serve":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import panel_server
        port = args.port or int(load_config().get("panel_port", 8765))
        panel_server.serve(port, open_browser=not args.no_browser)
        return 0

    if args.cmd == "home":
        return cmd_home(args.action, args.dest)

    if args.cmd == "backup":
        return cmd_backup(args.out, args.keep)

    if args.cmd == "archive":
        return cmd_archive(args.action, args.arg, args.limit, args.dry_run)

    if args.cmd == "restore":
        return cmd_restore(args.archive, args.yes)

    if args.cmd == "startup":
        return cmd_startup(args.query, args.budget)

    if args.cmd == "sync":
        return cmd_sync_dsh(args.days, args.dry_run, args.force, args.dir)

    # 用户附加规则：数据结构目录里的一块自由区，AI 遵守、更新技能不碰
    if args.cmd == "user-rules":
        path = HOME / "lingshu.user.md"
        if args.action == "add":
            text = (" ".join(args.text)).strip()
            if not text:
                print("用法：user-rules add <规则内容>，如 `user-rules add 不要动 DSH 数据目录`",
                      file=sys.stderr)
                return 2
            ensure_home()
            need_nl = False
            if path.exists():
                try:
                    tail = path.read_text(encoding="utf-8-sig")
                    need_nl = bool(tail) and not tail.endswith("\n")
                except OSError:
                    need_nl = False
            with open(path, "a", encoding="utf-8") as f:
                f.write(("\n" if need_nl else "") + text + "\n")
            print(f"已追加规则（{path}）：{text}")
            print("从下一个会话注入开始生效；当前可用 `inject` 立即看到。")
            return 0
        lines = read_user_rules()
        if args.action == "clear":
            if not lines:
                print("没有可清的规则。")
                return 0
            # 用户规则是长期手写的，清空前留一份副本（误触也能捞回来）
            bak = HOME / f"lingshu.user.md.bak-{now_ts()}"
            try:
                shutil.copy2(path, bak)
                print(f"已备份原规则：{bak}")
            except OSError as exc:
                print(f"[警告] 备份原规则失败：{exc}", file=sys.stderr)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            print(f"已清空 {len(lines)} 条用户附加规则（{path}）。")
            return 0
        if not lines:
            print(f"还没有用户附加规则（{path}）。")
            print("用 `user-rules add <规则>` 添加，如：`user-rules add 不要动 DSH 数据目录`、"
                  "`user-rules add 提到全局时指回 C:/Users/.../.trae-cn/skills`。")
            return 0
        print(f"用户附加规则（{path}，更新技能永不覆盖，每会话自动注入）：")
        for i, line in enumerate(lines, 1):
            print(f"  {i}. {line}")
        print("管理：user-rules add <规则> 追加 / user-rules clear 清空")
        return 0

    return 0


def main(argv=None) -> int:
    """入口：给 AI 的中文报错，不甩 Python 堆栈。"""
    try:
        return _main(argv)
    except (ValueError, KeyError, FileExistsError, PermissionError) as exc:
        log_event("run", f"CLI 出错：{exc}", level="error")
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止")
        return 130
    except Exception as exc:  # 兜底：不甩裸堆栈，但完整写进运行日志供排查
        import traceback
        log_event("run", f"未捕获异常：{type(exc).__name__}: {exc}", level="error",
                  trace=traceback.format_exc()[-400:])
        print(f"[错误] {type(exc).__name__}: {exc}"
              "（详情已写入运行日志，可用 `python scripts/brain.py logs --all` 查看）",
              file=sys.stderr)
        return 1
