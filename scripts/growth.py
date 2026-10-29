#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 成长子系统：知识库 / 笔记日志 / 技能自编辑

与记忆的区别：
  记忆 = 我经历过什么（episode / emotion / preference）
  知识 = 我整理出的可复用结论（topic / points / 版本）
  笔记 = 我写给自己的话（journal 日记 / log 工作日志 / growth 成长复盘）
  技能 = 我写给别人复用的能力（SKILL.md）

纯标准库，零依赖。命令行统一由 brain.py 转发。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

# Windows 控制台默认 GBK，中文输出会变乱码。无条件 reconfigure 成 UTF-8；
# errors="replace" 让无法编码的字符降级成 ?，流不支持 reconfigure 时静默跳过。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain  # noqa: E402

HOME = brain.HOME
KNOWLEDGE_FILE = HOME / "knowledge.jsonl"
KNOWLEDGE_DIR = HOME / "knowledge"
INDEX_FILE = KNOWLEDGE_DIR / "index.json"
NOTES_FILE = HOME / "notes.jsonl"
EVOLUTION_FILE = HOME / "evolution.jsonl"
SKILLS_DIR = HOME / "skills"
SKILL_REG = HOME / "skills.json"
# 扩展模块：AI 自己写的可执行钩子（inject / run），放在数据目录，更新技能永不覆盖
EXT_DIR = HOME / "extensions"


def _panel_root() -> Path:
    """面板/当前进程实际运行所在的技能根目录（growth.py 所在目录的上一级）。

    更新/检测灵枢本体时以它为最优先目标——面板跑在哪个目录，更新就落到哪个目录，
    不依赖 _global_roots 的固定探测顺序（多根/环境变量覆盖场景下那套会找错）。
    """
    return Path(__file__).resolve().parent.parent


def _global_roots() -> list:
    """全局技能目录候选，按优先级排列（第一个是安装目标）。

    DSH（DeepSeek Harness）扫的是 ~/.dsh/skills，Trae 扫的是 ~/.trae-cn/skills。
    灵枢本体主要跑在 DSH 里，所以两个都在时优先 DSH——否则 install 装到 Trae 目录，
    DSH 永远看不到（表现为"装了不出现/像改名"）。可用 LINGSHU_GLOBAL_SKILLS 覆盖。
    """
    env = (os.environ.get("LINGSHU_GLOBAL_SKILLS") or "").strip()
    if env:
        return [Path(env).expanduser()]
    roots = []
    dsh = Path.home() / ".dsh" / "skills"
    if dsh.is_dir():
        roots.append(dsh)
    roots.append(Path.home() / ".trae-cn" / "skills")
    return roots


GLOBAL_SKILLS = _global_roots()[0]  # 安装目标（主目录）

# 安装源体积上限：超过就认为 --from 指错了目录（防止把整个用户目录/磁盘塞进技能目录）
MAX_INSTALL_FILES = 3000
MAX_INSTALL_BYTES = 200 * 1024 * 1024

NOTE_KINDS = ("journal", "log", "growth")
# 技能名：小写字母/数字/连字符/下划线，也允许中文（中文生态里技能就叫「质检评审」这类名字）
SLUG_RE = re.compile(r"^[a-z0-9\u4e00-\u9fff][a-z0-9-_\u4e00-\u9fff]{0,62}$")

# 灵枢是"会自动记忆、会自动整理、能自己改说明书"的技能——装它的后果要说清楚，让用户自己决定。
LINGSHU_RISKS = [
    "它会在你本机 ~/.brain-memory 下长期保存：对话记忆、你告诉它的个人偏好与情绪记录（本地明文）。换机器/共享电脑时请注意隐私。",
    "上下文接近满时它会自动原文封存（capture）与做梦压缩，并每天写日记、更新知识库——这些是自动行为，内容会持续增长。",
    "它会只读扫描全局技能目录（~/.dsh/skills、~/.trae-cn/skills）与 DSH（~/.dsh/profiles）插件清单，判断有没有现成能力可用。",
    "它有自我编辑能力（skill new/edit/install），理论上能改写它自己的说明书与全局副本，并生成 AGENTS.md。",
    "记忆只存在本地，误删/损坏依赖手动备份：建议首次启用先跑 python scripts/brain.py backup。",
]


# ---------------------------------------------------------------- 通用

def _uid(prefix: str, seed: str) -> str:
    return prefix + hashlib.sha1(f"{seed}|{time.time_ns()}".encode()).hexdigest()[:10]


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _split(text: str | None) -> list:
    if not text:
        return []
    return [x.strip() for x in re.split(r"[、,，;；\n/|]+", text) if x.strip()]


def _dedupe(items: list) -> list:
    out, seen = [], set()
    for x in items:
        x = str(x).strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[:n].rstrip() + "…"


def _read_json(path: Path) -> dict:
    # Windows 记事本/PowerShell 可能写 BOM，统一用 utf-8-sig 读
    return json.loads(path.read_text(encoding="utf-8-sig"))


# ---------------------------------------------------------------- 知识库

def load_knowledge() -> list:
    raw = brain.read_jsonl(KNOWLEDGE_FILE)
    good = [r for r in raw if isinstance(r, dict) and r.get("id")]
    if len(good) != len(raw):
        brain.log_event("growth",
                        f"load_knowledge 跳过 {len(raw) - len(good)} 条畸形条目（缺 id 或类型错）",
                        level="warning")
    return good


def save_knowledge(rows: list) -> None:
    with brain._locked(KNOWLEDGE_FILE):
        brain.write_jsonl(KNOWLEDGE_FILE, rows)
    rebuild_index(rows)


def _kdoc(rec: dict) -> set:
    # 字段可能是 None（手改/LLM 产出），统一转成字符串再切词，避免 " ".join 抛 TypeError
    def _s(x):
        return str(x) if x is not None else ""
    return brain.terms(" ".join([
        _s(rec.get("title")) * 2, _s(rec.get("body")),
        " ".join(_s(p) for p in (rec.get("points") or [])),
        " ".join(_s(t) for t in (rec.get("tags") or [])),
    ]))


def add_knowledge(title: str, body: str, topic: str = "", points=None, tags=None,
                  entities=None, source_ids=None, confidence: float = 0.8) -> tuple:
    """写入一条知识；高度相似则合并进已有条目。返回 (记录, created|merged)。"""
    title = (title or "").strip()
    body = (body or "").strip()
    if not title and body:
        title = _clip(body, 24)
    if not title:
        raise ValueError("知识需要一个标题或正文")

    # 读-改-写整段加锁：否则两个并发 add（面板/CLI/做梦同时写）各自的快照会互相
    # 吞掉对方的合并或新增；save_knowledge 内部用的是同一把锁（可重入），不冲突。
    with brain._locked(KNOWLEDGE_FILE):
        rows = load_knowledge()
        new_title_terms = brain.terms(title)
        for old in rows:
            # 废弃（discarded）条目不能被合并复活：新内容写进去也永远搜不到
            if old.get("status") == "discarded":
                continue
            # 只在标题几乎相同（或包含关系极强）时合并，避免把同主题的不同结论揉成一条
            if brain.jaccard(new_title_terms, brain.terms(old.get("title", ""))) >= 0.75:
                old_body = str(old.get("body") or "")
                if body and body not in old_body:
                    old["body"] = (old_body + "\n\n" + body).strip()
                old["points"] = _dedupe((old.get("points") or []) + list(points or []))
                old["tags"] = _dedupe((old.get("tags") or []) + list(tags or []))
                old["entities"] = _dedupe((old.get("entities") or []) + list(entities or []))
                old["source_ids"] = _dedupe((old.get("source_ids") or []) + list(source_ids or []))
                old["topic"] = old.get("topic") or topic
                old["updated"] = brain.now_ts()
                old["version"] = int(old.get("version", 1)) + 1
                old.setdefault("history", []).append(
                    {"ts": old["updated"], "why": "写入时合并相似条目", "version": old["version"]})
                save_knowledge(rows)
                return old, "merged"

        rec = _new_knowledge(title, body, topic, points, tags, entities, source_ids, confidence)
        rows.append(rec)
        save_knowledge(rows)
    return rec, "created"


def _new_knowledge(title, body, topic="", points=None, tags=None, entities=None,
                   source_ids=None, confidence=0.8, status="confirmed") -> dict:
    now = brain.now_ts()
    return {
        "id": _uid("k_", title),
        "title": title,
        "topic": (topic or "未分类").strip(),
        "body": body,
        "points": _dedupe(list(points or [])),
        "tags": _dedupe(list(tags or [])),
        "entities": _dedupe(list(entities or [])),
        "source_ids": _dedupe(list(source_ids or [])),
        "confidence": max(0.0, min(1.0, float(confidence))),
        "status": status,          # confirmed 已确认 / pending 待审核（AI 整理产出）/ discarded 废弃
        "pinned": False,
        "version": 1,
        "created": now,
        "updated": now,
        "history": [{"ts": now, "why": "创建", "version": 1}],
    }


def list_knowledge(topic: str | None = None, limit: int = 50,
                   include_discarded: bool = False) -> list:
    rows = load_knowledge()
    if not include_discarded:
        rows = [r for r in rows if r.get("status") != "discarded"]
    if topic:
        rows = [r for r in rows if r.get("topic") == topic]
    return sorted(rows, key=lambda r: (r.get("pinned", False), r.get("updated", 0)), reverse=True)[:limit]


def knowledge_pending() -> list:
    """待审核的知识（多由 knowledge tidy 的 AI 整理产出，可能含幻觉，需人工过目）。"""
    return [r for r in load_knowledge() if r.get("status") == "pending"]


def set_knowledge_status(kid: str, status: str) -> dict | None:
    if status not in ("confirmed", "pending", "discarded"):
        raise ValueError(f"未知状态：{status}")
    with brain._locked(KNOWLEDGE_FILE):
        rows = load_knowledge()
        rec = next((r for r in rows if r["id"] == kid), None)
        if not rec:
            return None
        rec["status"] = status
        rec["updated"] = brain.now_ts()
        rec.setdefault("history", []).append(
            {"ts": rec["updated"], "why": f"状态改为 {status}", "version": int(rec.get("version", 1))})
        save_knowledge(rows)
    return rec


def search_knowledge(query: str, k: int = 8) -> list:
    rows = load_knowledge()
    if not rows or not (query or "").strip():
        return []
    q = brain.terms(query)
    docs = [_kdoc(r) for r in rows]
    idf = brain.build_idf(docs)
    out = []
    for rec, doc in zip(rows, docs):
        if rec.get("status") == "discarded":
            continue
        score = brain.relevance(q, doc, idf)
        if rec.get("pinned") and score > 0:
            score = min(1.0, score + 0.05)
        if score > 0.02:
            out.append((rec, score))
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:k]


def update_knowledge(kid: str, **fields) -> dict:
    with brain._locked(KNOWLEDGE_FILE):
        rows = load_knowledge()
        rec = next((r for r in rows if r["id"] == kid), None)
        if not rec:
            raise KeyError(f"没有这条知识：{kid}")
        for key in ("title", "topic", "body"):
            if fields.get(key) is not None:
                rec[key] = str(fields[key]).strip()
        for key in ("points", "tags", "entities"):
            if fields.get(key) is not None:
                rec[key] = _dedupe(list(fields[key]))
        if fields.get("confidence") is not None:
            rec["confidence"] = max(0.0, min(1.0, float(fields["confidence"])))
        if fields.get("pinned") is not None:
            rec["pinned"] = bool(fields["pinned"])
        rec["updated"] = brain.now_ts()
        rec["version"] = int(rec.get("version", 1)) + 1
        rec.setdefault("history", []).append({"ts": rec["updated"], "why": "编辑", "version": rec["version"]})
        save_knowledge(rows)
    return rec


def delete_knowledge(kid: str) -> bool:
    with brain._locked(KNOWLEDGE_FILE):
        rows = load_knowledge()
        keep = [r for r in rows if r["id"] != kid]
        if len(keep) == len(rows):
            return False
        save_knowledge(keep)
    return True


def rebuild_index(rows: list | None = None, force: bool = False) -> dict:
    rows = load_knowledge() if rows is None else rows
    newest = max((r.get("updated") or r.get("created") or 0 for r in rows), default=0)
    if not force and INDEX_FILE.exists():
        try:
            old = json.loads(INDEX_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            old = None
        if (old and old.get("ids") == [r["id"] for r in rows]
                and old.get("total") == len(rows) and old.get("data_stamp") == newest):
            return old
    topics, tags, entities = {}, {}, {}
    for r in rows:
        topics[r.get("topic", "未分类")] = topics.get(r.get("topic", "未分类"), 0) + 1
        for t in r.get("tags") or []:
            tags[t] = tags.get(t, 0) + 1
        for e in r.get("entities") or []:
            entities[e] = entities.get(e, 0) + 1
    idx = {
        "updated": brain.now_ts(),
        "data_stamp": newest,
        "total": len(rows),
        "topics": dict(sorted(topics.items(), key=lambda x: x[1], reverse=True)),
        "tags": dict(sorted(tags.items(), key=lambda x: x[1], reverse=True)[:60]),
        "entities": dict(sorted(entities.items(), key=lambda x: x[1], reverse=True)[:60]),
        "ids": [r["id"] for r in rows],
    }
    if brain.READONLY:
        return idx  # 只读=只能读不能写：统计/列表不该把索引写盘
    try:
        brain.atomic_json(INDEX_FILE, idx)
    except OSError:
        pass  # 索引只是缓存，写不了也不影响读写知识本体
    return idx


def knowledge_stats() -> dict:
    rows = [r for r in load_knowledge() if r.get("status") != "discarded"]
    idx = rebuild_index(rows)
    return {
        "total": len(rows),
        "topics": idx["topics"],
        "tags": list(idx["tags"].items())[:8],
        "words": sum(len(str(r.get("body") or "")) for r in rows),
        "pinned": sum(1 for r in rows if r.get("pinned")),
        "pending": sum(1 for r in rows if r.get("status") == "pending"),
    }


def tidy_knowledge() -> dict:
    """机械整理：找相似簇、无主题、无标签、过短条目，产出 pending.json 交给 LLM 压缩。"""
    with brain._locked(KNOWLEDGE_FILE):
        rows = [r for r in load_knowledge()
                if r.get("status") != "discarded" and r.get("id")]   # 缺 id 的畸形条目直接跳过
        if not rows:
            return {"task_id": None, "clusters": [], "untagged": [], "no_topic": [], "short": [],
                    "message": "知识库还是空的"}

        docs = {r["id"]: _kdoc(r) for r in rows}
        parent = {r["id"]: r["id"] for r in rows}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        ids = list(docs)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if brain.jaccard(docs[a], docs[b]) >= 0.45:
                    union(a, b)

        groups = {}
        for r in rows:
            groups.setdefault(find(r["id"]), []).append(r)

        clusters = []
        for members in groups.values():
            if len(members) > 1:
                clusters.append({
                    "ids": [m["id"] for m in members],
                    "titles": [m.get("title", "") for m in members],
                    "hint": "这些条目在讲同一件事，建议合并成一条更准确的知识。",
                    "texts": [{"id": m["id"], "title": m.get("title", ""),
                               "body": _clip(m.get("body", ""), 160),
                               "points": m.get("points", []), "tags": m.get("tags", [])}
                              for m in members],
                })

        report = {
            "task_id": _uid("tidy_", "knowledge"),
            "at": brain.now_ts(),
            "stats": knowledge_stats(),
            "clusters": clusters,
            "untagged": [{"id": r["id"], "title": r.get("title", "")} for r in rows
                         if not r.get("tags")],
            "no_topic": [{"id": r["id"], "title": r.get("title", "")} for r in rows
                         if r.get("topic") in ("", "未分类", None)],
            "short": [{"id": r["id"], "title": r.get("title", ""),
                       "len": len(str(r.get("body") or ""))}
                      for r in rows if len(str(r.get("body") or "")) < 40],
            "apply_schema": {
                "task_id": "tidy_...",
                "merge": [{"ids": ["k_..."], "title": "", "topic": "", "body": "",
                           "points": [], "tags": [], "entities": []}],
                "update": [{"id": "k_...", "topic": "", "tags": [], "body": ""}],
                "create": [{"title": "", "topic": "", "body": "",
                            "points": [], "tags": [], "entities": []}],
                "delete": ["k_..."],
                "note": "为什么这样整理，一句话",
            },
        }
        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        brain.atomic_json(KNOWLEDGE_DIR / "pending.json", report)
    return report


def apply_tidy(result: dict) -> dict:
    with brain._locked(KNOWLEDGE_FILE):
        rows = load_knowledge()
        by_id = {r["id"]: r for r in rows}
        done = {"merged": 0, "updated": 0, "created": 0, "deleted": 0}

        removed = set()
        for op in result.get("merge") or []:
            ids = _dedupe([i for i in (op.get("ids") or []) if i in by_id])
            if not ids:
                continue
            base = by_id[ids[0]]
            for other_id in ids[1:]:
                if other_id == base["id"]:
                    continue   # 重复 id 不能把 base 自己当"被合并者"删掉
                other = by_id[other_id]
                other_body = str(other.get("body") or "")
                base_body = str(base.get("body") or "")
                if other_body and other_body not in base_body:
                    base["body"] = (base_body + "\n\n" + other_body).strip()
                base["points"] = _dedupe((base.get("points") or []) + (other.get("points") or []))
                base["tags"] = _dedupe((base.get("tags") or []) + (other.get("tags") or []))
                base["entities"] = _dedupe((base.get("entities") or []) + (other.get("entities") or []))
                base["source_ids"] = _dedupe((base.get("source_ids") or []) + (other.get("source_ids") or []))
                base["confidence"] = max(float(base.get("confidence", 0.8)), float(other.get("confidence", 0.8)))
                removed.add(other_id)
            for key in ("title", "topic", "body"):
                if op.get(key):
                    base[key] = op[key]
            for key in ("points", "tags", "entities"):
                if op.get(key):
                    base[key] = _dedupe(op[key])
            base["updated"] = brain.now_ts()
            base["version"] = int(base.get("version", 1)) + 1
            base["status"] = "pending"   # AI 动过的东西一律先待审核，别让它直接沉淀
            base.setdefault("history", []).append(
                {"ts": base["updated"], "why": "知识整理：合并 " + str(len(ids)) + " 条", "version": base["version"]})
            done["merged"] += 1

        for op in result.get("update") or []:
            rec = by_id.get(op.get("id"))
            if not rec:
                continue
            for key in ("title", "topic", "body"):
                if op.get(key):
                    rec[key] = op[key]
            for key in ("points", "tags", "entities"):
                if op.get(key):
                    rec[key] = _dedupe(op[key])
            rec["updated"] = brain.now_ts()
            rec["version"] = int(rec.get("version", 1)) + 1
            rec["status"] = "pending"   # AI 更新过 → 待审核
            rec.setdefault("history", []).append(
                {"ts": rec["updated"], "why": "知识整理：更新", "version": rec["version"]})
            done["updated"] += 1

        for op in result.get("create") or []:
            if not (op.get("title") or op.get("body")):
                continue
            title = (op.get("title") or "").strip() or _clip(op.get("body", ""), 24)
            rec = _new_knowledge(title, op.get("body", ""), op.get("topic", ""),
                                 op.get("points"), op.get("tags"), op.get("entities"),
                                 status="pending")   # AI 新建 → 待审核，别让幻觉直接入库
            rows.append(rec)
            by_id[rec["id"]] = rec
            done["created"] += 1

        for kid in result.get("delete") or []:
            if kid in by_id:
                removed.add(kid)

        rows = [r for r in rows if r["id"] not in removed]
        save_knowledge(rows)
        # 统计真实从库里消失的条数（含合并被吃掉的那 X-1 条），别只报 delete 清单
        done["deleted"] = len(removed)

    pending = KNOWLEDGE_DIR / "pending.json"
    if pending.exists():
        try:
            data = _read_json(pending)
            data["applied_at"] = brain.now_ts()
            data["applied_result"] = done
            brain.atomic_json(pending, data)
        except Exception:
            pass
    return done


def tidy_state() -> dict | None:
    path = KNOWLEDGE_DIR / "pending.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if data.get("applied_at"):
        return None
    return data


# ---------------------------------------------------------------- 笔记 / 日志

def load_notes() -> list:
    raw = brain.read_jsonl(NOTES_FILE)
    good = [r for r in raw if isinstance(r, dict) and r.get("id")]
    if len(good) != len(raw):
        brain.log_event("growth",
                        f"load_notes 跳过 {len(raw) - len(good)} 条畸形条目",
                        level="warning")
    return good


def save_notes(rows: list) -> None:
    with brain._locked(NOTES_FILE):
        brain.write_jsonl(NOTES_FILE, rows)


def write_note(kind: str, title: str, body: str, mood: str = "", tags=None,
               memory_ids=None, append_same_day: bool = True) -> tuple:
    kind = kind if kind in NOTE_KINDS else "journal"
    body = (body or "").strip()
    title = (title or "").strip() or _clip(body, 20) or "无题"
    today = _today()
    # 读-改-写整段加锁（save_notes 用同一把锁，可重入）：避免并发写笔记互相覆盖
    with brain._locked(NOTES_FILE):
        rows = load_notes()
        if append_same_day and kind == "journal":
            for old in rows:
                if old.get("type") == kind and old.get("date") == today and old.get("title") == title:
                    old["body"] = (old.get("body", "") + "\n\n" + body).strip()
                    old["updated"] = brain.now_ts()
                    old["mood"] = mood or old.get("mood", "")
                    old["tags"] = _dedupe((old.get("tags") or []) + list(tags or []))
                    old["memory_ids"] = _dedupe((old.get("memory_ids") or []) + list(memory_ids or []))
                    save_notes(rows)
                    return old, "appended"

        now = brain.now_ts()
        rec = {
            "id": _uid("n_", title),
            "date": today,
            "type": kind,
            "title": title,
            "body": body,
            "mood": mood or "",
            "tags": _dedupe(list(tags or [])),
            "memory_ids": _dedupe(list(memory_ids or [])),
            "created": now,
            "updated": now,
        }
        rows.append(rec)
        save_notes(rows)
    return rec, "created"


def update_note(nid: str, **fields) -> dict:
    with brain._locked(NOTES_FILE):
        rows = load_notes()
        rec = next((r for r in rows if r["id"] == nid), None)
        if not rec:
            raise KeyError(f"没有这条笔记：{nid}")
        for key in ("title", "body", "mood"):
            if fields.get(key) is not None:
                rec[key] = str(fields[key]).strip()
        for key in ("tags", "memory_ids"):
            if fields.get(key) is not None:
                rec[key] = _dedupe(list(fields[key]))
        rec["updated"] = brain.now_ts()
        save_notes(rows)
    return rec


def delete_note(nid: str) -> bool:
    with brain._locked(NOTES_FILE):
        rows = load_notes()
        keep = [r for r in rows if r["id"] != nid]
        if len(keep) == len(rows):
            return False
        save_notes(keep)
        return True


def list_notes(kind: str | None = None, days: int | None = None, limit: int = 60) -> list:
    rows = load_notes()
    if kind:
        rows = [r for r in rows if r.get("type") == kind]
    if days is not None:
        ts = brain.now_ts()
        if days == 0:
            # 只看今天：从本地时区当天零点算起，而不是"此刻往前 0 天"（那恒为空）
            lt = time.localtime(ts)
            floor = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        else:
            floor = ts - days * brain.DAY
        rows = [r for r in rows if r.get("created", 0) >= floor]
    return sorted(rows, key=lambda r: r.get("created", 0), reverse=True)[:limit]


def search_notes(query: str, k: int = 10) -> list:
    rows = load_notes()
    if not rows or not (query or "").strip():
        return []
    q = brain.terms(query)
    docs = [brain.terms(" ".join([str(r.get("title") or "") * 2, str(r.get("body") or ""),
                                  " ".join(str(t) for t in (r.get("tags") or []))])) for r in rows]
    idf = brain.build_idf(docs)
    out = [(r, brain.relevance(q, d, idf)) for r, d in zip(rows, docs)]
    out = [x for x in out if x[1] > 0.02]
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:k]


def note_stats() -> dict:
    rows = load_notes()
    by_type = {}
    for r in rows:
        by_type[r.get("type", "journal")] = by_type.get(r.get("type", "journal"), 0) + 1
    days = {}
    for r in rows:
        days[r.get("date", "")] = days.get(r.get("date", ""), 0) + 1
    return {
        "total": len(rows),
        "by_type": by_type,
        "days": len(days),
        "last": max((r.get("created", 0) for r in rows), default=0),
        "words": sum(len(str(r.get("body") or "")) for r in rows),
    }


def digest(days: int = 7) -> str:
    """给 AI 的成长素材包：最近发生了什么、学到什么、写了什么。"""
    now = brain.now_ts()
    floor = now - days * brain.DAY
    mems = [m for m in brain.load_all() if brain._num(m.get("created"), 0) >= floor]
    kds = [k for k in load_knowledge() if brain._num(k.get("updated"), 0) >= floor]
    nts = [n for n in load_notes() if brain._num(n.get("created"), 0) >= floor]
    sks = list_skills()
    evo = [r for r in load_evolution() if brain._num(r.get("ts"), 0) >= floor]

    lines = [f"# 成长素材 · 最近 {days} 天", "",
             f"- 新记忆 {len(mems)} 条，知识变动 {len(kds)} 条，笔记 {len(nts)} 条，技能 {len(sks)} 个，"
             f"自我进化 {len(evo)} 次"]
    if mems:
        lines += ["", "## 值得回看的记忆"]
        for m in sorted(mems, key=lambda x: x.get("importance", 0), reverse=True)[:12]:
            lines.append(f"- ({m.get('kind')}/{m.get('importance')}) {brain.display_text(m)}")
    if kds:
        lines += ["", "## 知识变动"]
        for k in sorted(kds, key=lambda x: x.get("updated", 0), reverse=True)[:8]:
            lines.append(f"- [{k.get('topic')}] {k.get('title')}（v{k.get('version')}）")
    if nts:
        lines += ["", "## 最近写下的笔记"]
        for n in sorted(nts, key=lambda x: x.get("created", 0), reverse=True)[:8]:
            lines.append(f"- {n.get('date')} [{n.get('type')}] {n.get('title')}：{_clip(n.get('body', ''), 80)}")
    if sks:
        lines += ["", "## 我写过的技能"]
        for s in sks:
            lines.append(f"- {s['name']}（v{s.get('version', 1)}）{s.get('description', '')[:60]}")
    if evo:
        lines += ["", "## 最近的自我进化"]
        for e in sorted(evo, key=lambda x: x.get("ts", 0), reverse=True)[:8]:
            lines.append(f"- {e.get('date')} [{e.get('kind')}] {e.get('title')}：{_clip(e.get('detail', ''), 80)}")
    lines += ["", "## 写复盘时回答这三个问题",
              "1. 这段时间我对待用户的方式，哪里更好了？哪里还急？",
              "2. 哪条旧判断被新事实推翻了？",
              "3. 下一个想练的能力是什么？",
              "", "把答案写进成长笔记：",
              f'python scripts/brain.py note write "第 N 次复盘" --type growth --body "..." --tags 成长,复盘',
              "", "把这次真正改掉的东西记进进化日志：",
              'python scripts/brain.py evolve log "标题" --kind thinking --detail "改了什么、为什么、怎么验证"']
    return "\n".join(lines)


# ---------------------------------------------------------------- 技能自编辑

def load_registry() -> dict:
    if SKILL_REG.exists():
        try:
            data = _read_json(SKILL_REG)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_registry(reg: dict) -> None:
    # 内部持锁 + 合并式写法：调用方传过来的是旧快照，但 update() 里已经改了要改的键。
    # 这里先 load 当前盘上的最新值，再用 reg 合并——这样并发的两个 save_registry 不会
    # 互相覆盖（串行化：后一个 load 到前一个刚写的值，合并自己的，再写）。
    with brain._locked(SKILL_REG):
        cur = load_registry()
        cur.update(reg)
        brain.atomic_json(SKILL_REG, cur)


# 中文显示名 → kebab-case 目录名。DSH 的文件系统技能发现只认 kebab-case 的 name，
# 中文名会被静默跳过（装完像"改名"或干脆不出现），所以装/查/删都统一折算成英文 slug。
_SKILL_NAME_ALIASES = {
    "灵枢": "lingshu",
    "灵枢-lite": "lingshu-lite",
    "灵枢-tiny": "lingshu-tiny",
}
# 反查：kebab slug → 它对应的旧中文目录名（旧版安装会把中文名当目录名）
_SLUG_TO_LEGACY_NAME = {v: k for k, v in _SKILL_NAME_ALIASES.items()}
# 灵枢本体的所有可识别名字（旧中文名 + 新 kebab 名），用于风险确认与"这是我自己"判定
_SELF_NAMES = ("灵枢", "lingshu")


def _require_slug(name: str) -> str:
    # 统一归一化：大小写、空格、下划线都折算成连字符，保证 new / show / rm 用同一个名字都能找到
    name = (name or "").strip().lower().replace(" ", "-").replace("_", "-")
    name = _SKILL_NAME_ALIASES.get(name, name)   # 中文名折算成 kebab-case（DSH 才认）
    if not SLUG_RE.match(name):
        raise ValueError("技能名不合法：只能用字母/数字/连字符/下划线/中文，1-63 个字符")
    return name


def _skill_path(name: str) -> Path:
    return SKILLS_DIR / _require_slug(name) / "SKILL.md"


def read_skill(name: str) -> dict:
    path = _skill_path(name)
    if not path.exists():
        raise KeyError(f"没有这个技能：{name}")
    name = _require_slug(name)   # 归一化，保证回写 frontmatter 与目录名一致
    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    return {"name": name, "meta": meta, "body": body, "path": str(path), "raw": text}


def parse_frontmatter(text: str) -> tuple:
    """解析 YAML frontmatter。纯标准库，不引 yaml 依赖。

    支持：单行 key: value、引号包裹、块标量（| 保留换行 / > 折叠为空格）、
    以及常见简写（值里含冒号、以 # 开头）。够读 DSH / Claude / Trae 各家的 SKILL.md。
    """
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head = text[3:end]
    body = text[end + 4:].lstrip("\n")

    meta = {}
    lines = head.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if val in ("|", ">", "|-", ">-", "|+", ">+"):
            # 块标量：收集后续缩进行
            block, j = [], i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() and not nxt.startswith((" ", "\t")):
                    break
                block.append(nxt.strip())
                j += 1
            sep = "\n" if val.startswith("|") else " "
            meta[key] = sep.join(x for x in block if x)
            i = j
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        meta[key] = val
        i += 1
    return meta, body


def _compose(name: str, description: str, body: str, version: int, created: str, updated: str,
             origin: str = "灵枢") -> str:
    description = re.sub(r"\s+", " ", description).strip()
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"origin: {origin}\n"
        f"version: {version}\n"
        f"created: {created}\n"
        f"updated: {updated}\n"
        "---\n\n"
        + body.strip() + "\n"
    )


def create_skill(name: str, description: str, body: str, overwrite: bool = False,
                 origin: str = "灵枢") -> dict:
    name = _require_slug(name)   # 含中文名折算成 slug，保证 frontmatter name 与目录名一致
    description = (description or "").strip()
    body = (body or "").strip()
    if len(description) < 12:
        raise ValueError("description 太短：要写清「什么时候该用这个技能」，至少 12 个字")
    if len(body) < 40:
        raise ValueError("正文太短：至少写清用途、步骤、注意事项")
    path = _skill_path(name)
    if path.exists() and not overwrite:
        raise FileExistsError(f"技能 {name} 已存在；要改内容用 skill edit，要覆盖用 --force")
    created = _today()
    old = read_skill(name) if path.exists() else None
    version = int(old["meta"].get("version", 1)) + 1 if old else 1
    if old:
        created = old["meta"].get("created", created)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_compose(name, description, body, version, created, _today(), origin),
                    encoding="utf-8")

    reg = load_registry()
    reg[name] = {"description": description, "version": version,
                 "created": reg.get(name, {}).get("created", brain.now_ts()),
                 "updated": brain.now_ts(), "installed_at": reg.get(name, {}).get("installed_at")}
    save_registry(reg)
    return {"name": name, "path": str(path), "version": version}


def edit_skill(name: str, body: str | None = None, append: str | None = None,
               description: str | None = None) -> dict:
    name = _require_slug(name)   # 归一化后再回写 frontmatter，避免名字漂移
    cur = read_skill(name)
    meta = cur["meta"]
    new_body = (body if body is not None else cur["body"])
    if append:
        new_body = new_body.rstrip() + "\n\n" + append.strip()
    if len(new_body.strip()) < 40:
        raise ValueError("正文太短，技能至少要有用途、步骤、注意事项")
    desc = description if description is not None else meta.get("description", "")
    version = int(meta.get("version", 1)) + 1
    created = meta.get("created") or _today()
    Path(cur["path"]).write_text(
        _compose(name, desc, new_body, version, created, _today(),
                 meta.get("origin", "灵枢")), encoding="utf-8")
    reg = load_registry()
    reg[name] = {"description": desc, "version": version,
                 "created": reg.get(name, {}).get("created", brain.now_ts()),
                 "updated": brain.now_ts(), "installed_at": reg.get(name, {}).get("installed_at")}
    save_registry(reg)
    return {"name": name, "version": version, "path": cur["path"]}


def _ver(v) -> object:
    """版本号容错：自建技能是自增整数，本体是语义化版本（2.11.2）。
    直接 int('2.11.2') 会抛 ValueError 把 skill list 整个搞崩，这里退化成原串。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return str(v or "1").strip() or "1"


def _find_installed(name: str) -> Path:
    """在全局目录（多根：DSH/Trae）里找 name 技能的目录；找不到返回主目录下的默认路径。"""
    # 第一优先：当前进程自己所在的技能根（面板从哪跑就认哪）——
    # 面板跑在某个全局副本里时，检测/更新必须针对那个副本，而不是探测顺序的第一个根
    panel = _panel_root() / name
    if (panel / "SKILL.md").is_file():
        return panel
    for r in _global_roots():
        p = r / name
        if (p / "SKILL.md").is_file():
            return p
    return GLOBAL_SKILLS / name


def list_installed() -> list:
    """列出已装进全局技能目录的技能。

    DSH 的文件系统技能发现只认 kebab-case 的 name，中文名会被静默跳过，
    所以这里额外核对「目录名 == frontmatter name（kebab）」：不一致就标 ✗，
    让用户一眼看出为什么装了却不出现。多全局目录（DSH/Trae）都会扫。
    """
    out = []
    for root in _global_roots():
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            p = d / "SKILL.md"
            if not p.is_file():
                continue
            try:
                meta, _ = parse_frontmatter(p.read_text(encoding="utf-8-sig"))
            except OSError:
                meta = {}
            name = (meta.get("name") or d.name).strip()
            try:
                ok = d.name == _require_slug(name)
            except ValueError:
                ok = False
            out.append({
                "dir": d.name,
                "root": str(root),
                "name": name,
                "version": _ver(meta.get("version", 1)),
                "ok": ok,
                "agents": (d / "AGENTS.md").exists(),
                "path": str(p),
            })
    return out


def _diff_signature(dir_path: Path) -> set:
    """目录内容的差异签名：相对路径 + 大小 + mtime 取整秒。
    用于对比"本地源目录"和"全局已装副本"是否一致，判断有没有可更新的内容。"""
    sig = set()
    if not dir_path.is_dir():
        return sig
    for f in dir_path.rglob("*"):
        if not f.is_file() or "__pycache__" in f.parts or ".git" in f.parts:
            continue
        # AGENTS.md 是安装时重新生成的（区块化更新，含时间性内容），不参与差异判定
        if f.name.endswith((".tmp", ".pyc", ".lock", ".corrupt")) or f.name == "AGENTS.md":
            continue
        try:
            st = f.stat()
            sig.add((f.relative_to(dir_path).as_posix(), st.st_size, int(st.st_mtime)))
        except OSError:
            continue
    return sig


def _real_drift(src_sig: set, dst_sig: set) -> bool:
    """签名有差时判断是不是"真差异"（该提示更新的那种）。

    两类真差异，不依赖版本号就能认定：
    - 文件级差异：源有副本没有（副本残缺，换盘符场景）、或副本有源没有的文件；
    - 关键文件内容变了：SKILL.md / scripts / assets 下同名文件大小不同
      （mtime 单独抖动不算，安装复制本身就会带出新 mtime）。
    其余（同名同大小的纯 mtime 抖动）不算——交给版本号闸门兜底。
    """
    src_rels = {rel for rel, *_ in src_sig}
    dst_rels = {rel for rel, *_ in dst_sig}
    if src_rels != dst_rels:  # 有文件增删 = 残缺或多余，真差异
        return True
    src_by_rel = {rel: (size, mtime) for rel, size, mtime in src_sig}
    for rel, size, _ in dst_sig:
        if size != src_by_rel.get(rel, (size,))[0]:
            # 大小不同：SKILL.md/scripts/assets 是要紧东西，算真差异；
            # 其他零散文件（日志/缓存类）留给版本号闸门
            if rel == "SKILL.md" or rel.startswith(("scripts/", "assets/")):
                return True
    return False


def list_skills() -> list:
    reg = load_registry()
    out = []
    if SKILLS_DIR.exists():
        for d in sorted(SKILLS_DIR.iterdir()):
            p = d / "SKILL.md"
            if not p.is_file():
                continue
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
            name = meta.get("name") or d.name
            try:
                installed_dir = _find_installed(_require_slug(name))
            except ValueError:
                installed_dir = _find_installed(name)
            # 有更新检测：本地源与全局已装副本内容不一致（副本存在但内容有差异）。
            # 双保险 2.0：纯 mtime 抖动（同名同大小）必须版本号真变了才算过期；
            # 但副本残缺/关键文件内容变化（_real_drift）不依赖版本号就能认定——
            # 忘了升版本号时用户也不会卡死在损坏副本上。
            installed = (installed_dir / "SKILL.md").exists()
            outdated = False
            if installed and installed_dir.resolve() != d.resolve():
                try:
                    inst_meta, _ = parse_frontmatter((installed_dir / "SKILL.md").read_text(encoding="utf-8"))
                except OSError:
                    inst_meta = {}
                src_sig, dst_sig = _diff_signature(d), _diff_signature(installed_dir)
                outdated = (src_sig != dst_sig
                            and (_real_drift(src_sig, dst_sig)
                                 or _ver(meta.get("version", 1)) != _ver(inst_meta.get("version", 1))))
            out.append({
                "name": name,
                "description": meta.get("description", ""),
                "version": _ver(meta.get("version", reg.get(name, {}).get("version", 1) or 1)),
                "lines": len(body.splitlines()),
                "updated": p.stat().st_mtime,
                "installed": installed,
                "outdated": outdated,
                "agents": (installed_dir / "AGENTS.md").exists(),
                "path": str(p),
            })
    return sorted(out, key=lambda x: x["updated"], reverse=True)


# ---- Gitee 远程拉取源 ----------------------------------------------------
# 本地开发源（config.lingshu_source_dir）不可用时，从 Gitee 仓库拉最新代码作为
# 更新源。纯标准库：Gitee API v5 匿名接口（urllib，30s 超时）。
# 注意：Gitee 的 /repository/archive/main.zip 归档下载已被风控拦截（对脚本
# UA 返回 HTML 验证页而非 zip），不能直接用；API v5 的 contents 接口匿名可用。
GITEE_API_BASE = "https://gitee.com/api/v5/repos/htmcwy_0/lingshu-skill"
GITEE_BRANCH = "main"
GITEE_TIMEOUT = 30  # 下载超时（秒）

import json as _json
import urllib.request as _urlreq
import urllib.parse as _urlparse


def _gitee_cache_dir() -> Path:
    """Gitee 拉取缓存的临时目录（系统 temp 下带仓库标识，进程存活期间复用）。"""
    import tempfile
    d = Path(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir())
    return d / "lingshu-gitee-src"


def _gitee_api(path: str) -> object:
    """调 Gitee API v5 匿名接口，返回解析后的 JSON。失败抛 URLError/JSONDecodeError。"""
    url = f"{GITEE_API_BASE}{path}"
    req = _urlreq.Request(url, headers={"User-Agent": "lingshu-updater",
                                        "Accept": "application/json"})
    with _urlreq.urlopen(req, timeout=GITEE_TIMEOUT) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _fetch_gitee_source() -> Path:
    """从 Gitee 拉取 main 分支全部文件到临时缓存目录，返回含 SKILL.md 的真实根。

    用 API v5 匿名接口：先取仓库目录树（recursive=1），再逐个取文本文件内容
    （base64 解码）。仓库规模小（几十个文件），总量可控。失败抛异常由调用方降级。
    """
    import base64
    cache = _gitee_cache_dir()
    extract_root = cache / "extracted"
    if extract_root.exists():
        shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)

    tree = _gitee_api(f"/git/trees/{GITEE_BRANCH}?recursive=1")
    if tree.get("truncated"):
        raise RuntimeError("Gitee 目录树被截断（仓库过大），无法安全拉取")
    n = 0
    for item in tree.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        if (".git" in path.split("/") or "__pycache__" in path
                or path.endswith((".pyc", ".tmp"))):
            continue
        blob = _gitee_api(f"/contents/{_urlparse.quote(path)}?ref={GITEE_BRANCH}")
        if blob.get("encoding") != "base64":
            raise RuntimeError(f"Gitee 返回内容编码异常：{path}")
        data = base64.b64decode(blob.get("content", ""))
        target = extract_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        n += 1
        if n > 500:
            raise RuntimeError("文件数异常（>500），可能指错仓库，已停止")
    if not (extract_root / "SKILL.md").is_file():
        raise RuntimeError("Gitee 拉取结果里找不到 SKILL.md（可能仓库结构变了）")
    return extract_root


def _lingshu_remote_source(force: bool = False) -> Path | None:
    """尝试获取 Gitee 远程源目录；成功返回路径，失败返回 None（调用方降级）。

    复用缓存目录里已拉取的源（同一会话内不重复下载），force=True 强制重新拉取
    （点「更新」时用，确保拿到最新代码）。缓存有效性以是否解压出 SKILL.md 为准。
    """
    cache = _gitee_cache_dir()
    cached = cache / "extracted"
    if (cached / "SKILL.md").is_file() and not force:
        return cached
    try:
        return _fetch_gitee_source()
    except Exception as e:  # 网络失败/接口失败：降级，不崩溃
        print(f"[lingshu] Gitee 拉取失败，降级为本地副本源：{e}", file=sys.stderr)
        return None


def update_check(name: str = "lingshu") -> dict:
    """检查本地源与全局已装副本的差异，返回「更新了什么」明细。

    面板「检查更新」用：installed=False 表示没装过；outdated=False 表示已是最新；
    outdated=True 时 changed/added/removed 三个列表说明源里相对全局副本动了哪些文件。
    """
    try:
        src_dir = SKILLS_DIR / _require_slug(name)
    except ValueError:
        return {"name": name, "installed": False, "outdated": False}
    if not (src_dir / "SKILL.md").is_file() and _is_lingshu(name, {}, None):
        # 灵枢本体：源就是技能包自身目录（scripts/assets 就在这里），不走 skills/ 技能区
        src_dir = Path(__file__).resolve().parent.parent
        out_src = "local"   # 默认：面板自身目录即源（面板直接跑在开发仓库里的场景）
        # 面板跑在全局副本里时，"自身目录"==已装目录，自己比自己永远"一致"。
        # 此时查配置里登记的开发源目录（config.lingshu_source_dir），有就用它。
        if src_dir.resolve() == _find_installed(_require_slug(name)).resolve():
            cfg_src = str(brain.load_config().get("lingshu_source_dir") or "")
            source = "local"
            if cfg_src and (Path(cfg_src) / "SKILL.md").is_file():
                src_dir = Path(cfg_src)
            else:
                # 没配 lingshu_source_dir（或目录失效）：回退到 Gitee 远程拉 zip 作源。
                # 拉取失败再降级为"源即副本"（显示已最新），不崩溃。
                remote = _lingshu_remote_source()
                if remote is not None:
                    src_dir = remote
                    source = "gitee"
                else:
                    source = "none"
            if source == "none":
                # 本地与远程源都拿不到：源就是已装副本本身，没有可比的"新版本"。
                # 不能拿副本目录自己比自己——副本一旦有残缺文件会误报 outdated: true
                # + 全量 changed/removed（朋友机器换盘符场景坐实过）。
                installed_dir = _find_installed(_require_slug(name))
                try:
                    meta, _ = parse_frontmatter((installed_dir / "SKILL.md").read_text(encoding="utf-8"))
                except OSError:
                    meta = {}
                return {"name": name, "installed": True, "outdated": False, "source": "none",
                        "version": _ver(meta.get("version", 1)),
                        "installed_version": _ver(meta.get("version", 1)),
                        "global_path": str(installed_dir)}
            out_src = source
    src_md = src_dir / "SKILL.md"
    if not src_md.is_file():
        return {"name": name, "installed": False, "outdated": False}
    try:
        meta, _ = parse_frontmatter(src_md.read_text(encoding="utf-8"))
    except OSError:
        meta = {}
    try:
        installed_dir = _find_installed(_require_slug(name))
    except (ValueError, KeyError):
        installed_dir = Path()
    if not (installed_dir / "SKILL.md").exists():
        return {"name": name, "installed": False, "outdated": False,
                "version": _ver(meta.get("version", 1)),
                "global_path": str(installed_dir)}

    src_sig = _diff_signature(src_dir)
    dst_sig = _diff_signature(installed_dir)
    # 已装版本：读全局副本 SKILL.md 的 frontmatter（对比基准，决定 changelog 截到哪）
    try:
        inst_meta, _ = parse_frontmatter((installed_dir / "SKILL.md").read_text(encoding="utf-8"))
    except OSError:
        inst_meta = {}
    # 双保险 2.0：纯 mtime 抖动（同名同大小）必须版本号真变了才算过期；
    # 但副本残缺/关键文件内容变化（_real_drift）不依赖版本号——
    # 发布时忘了升版本号，用户也不会卡死在旧版/损坏副本上。
    outdated = bool(src_sig != dst_sig) and (
        _real_drift(src_sig, dst_sig)
        or _ver(meta.get("version", 1)) != _ver(inst_meta.get("version", 1)))
    out = {
        "name": name,
        "installed": True,
        "outdated": outdated,
        "source": out_src,   # 更新来源：local=本地开发仓库 / gitee=Gitee 远程 / none=无源降级
        "version": _ver(meta.get("version", 1)),
        "installed_version": _ver(inst_meta.get("version", 1)),
        "global_path": str(installed_dir),
    }
    if out["outdated"]:
        # 硬约束：面板更新说明只给人类可读正文，绝不把文件清单兜底显示给用户。
        # 所以 changed/added/removed 不进响应（前端兜底分支随之失效），
        # CHANGELOG 抽不到区间正文时只给一句通用文案。
        out["changelog"] = (
            _changelog_between(src_dir, out["installed_version"], out["version"])
            or "本次更新内容较多，详见 CHANGELOG.md。")
    return out


def _changelog_between(src_dir: Path, old_ver: str, new_ver: str) -> str:
    """从源的 CHANGELOG.md 抽取 (old_ver, new_ver] 区间的更新说明（人类可读正文）。

    只保留一级版本标题（## vX.Y）到下一个一级版本标题之间的内容，去掉括号里的
    合并哈希注释行；old_ver 在源里找不到时返回 None（调用方给通用文案，不给文件清单）。
    """
    md = src_dir / "CHANGELOG.md"
    if not md.is_file():
        return None
    try:
        lines = md.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    def _vnum(s: str) -> tuple | None:
        m = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", s.strip())
        return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)) if m else None

    start = _vnum(old_ver)
    end = _vnum(new_ver)
    if not end:
        return None
    out, picking = [], False
    for ln in lines:
        m = re.match(r"##\s+(v?[\d.]+)", ln)
        if m:
            v = _vnum(m.group(1))
            picking = bool(v and v > start and v <= end) if start else bool(v and v <= end)
            if picking:
                out.append(ln.split("—")[0].rstrip(" —-"))  # 标题行去掉破折号后的副标题哈希
            continue
        if picking:
            if ln.strip().startswith("（") or ln.strip().startswith("("):
                continue  # 合并哈希注释行
            out.append(ln)
    text = "\n".join(out).strip()
    return text or None


AGENTS_BLOCK_START = "<!-- lingshu:start -->"
AGENTS_BLOCK_END = "<!-- lingshu:end -->"
# 旧版（无区块标记）生成文件的可识别签名：命中说明整文件都是灵枢生成的，没有用户手写内容
_LEGACY_AGENTS_SIGNATURE = "由灵枢在全局化时生成"


def _agents_rules(name: str, description: str, meta: dict, from_dir: str | None = None) -> str:
    """AGENTS.md 里由灵枢维护的规则正文（区块内部，不含标题与标记）。"""
    entry = ""
    src_dir = Path(from_dir).expanduser() if from_dir else (SKILLS_DIR / name)
    if (src_dir / "scripts").is_dir():
        scripts = sorted(p.name for p in (src_dir / "scripts").glob("*.py"))
        if scripts:
            entry = "、".join(f"`python scripts/{s}`" for s in scripts[:4])
    elif any(src_dir.glob("*.py")):
        entry = "、".join(f"`python {p.name}`" for p in sorted(src_dir.glob("*.py"))[:4])
    lines = [
        f"**何时用**：{description}",
        "",
        "**怎么用**：先读同目录 `SKILL.md`，按其中的步骤执行；有 `scripts/` 就直接调用。",
    ]
    if entry:
        lines.append(f"**入口**：{entry}")
    lines += ["", f"**来源**：{meta.get('origin', '灵枢')}　**版本**：{meta.get('version', 1)}"]
    return "\n".join(lines)


def _agents_md(name: str, description: str, meta: dict, from_dir: str | None = None) -> str:
    """给全局技能生成 AGENTS.md：DSH/DeepSeek 生态的常驻入口。
    只写「何时用 / 怎么调 / 在哪」，不复制 SKILL.md 正文。
    生成内容用 <!-- lingshu:start/end --> 区块包裹：更新时只替换这个区块，
    区块之外的用户手写内容（如 DSH 只读保护、全局目录指回）永远保留。"""
    rules = _agents_rules(name, description, meta, from_dir)
    lines = [
        f"# {name}",
        "",
        "<!-- 常驻层：由灵枢在全局化时生成。两条注释标记之间的内容每次更新会整块替换；",
        "     标记之外的部分是你自己的，更新永不覆盖（可写 DSH 只读保护、全局目录指回等）。 -->",
        "",
        AGENTS_BLOCK_START,
        rules,
        AGENTS_BLOCK_END,
    ]
    return "\n".join(lines) + "\n"


def _looks_generated(text: str) -> bool:
    """旧版无标记文件是否「整份都是灵枢生成的」——只有这样才能安全整体替换。
    任何一行不属于生成结构，就说明用户手写过，必须保留。"""
    if _LEGACY_AGENTS_SIGNATURE not in text:
        return False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if (s.startswith("#") or s.startswith("**何时用**") or s.startswith("**怎么用**")
                or s.startswith("**入口**") or s.startswith("**来源**") or s.startswith("**版本**")
                or _LEGACY_AGENTS_SIGNATURE in s):
            continue
        return False
    return True


def _upsert_agents_block(path: Path, generated: str) -> str:
    """区块化更新 AGENTS.md，返回 'written' / 'updated' / 'merged'。

    - 文件不存在：直接写生成版。
    - 有 <!-- lingshu:start/end --> 标记：只替换标记之间的内容，标记外原样保留。
    - 无标记但整份可识别为旧版生成文件：整体替换（里面没有用户内容）。
    - 无标记且含用户手写：把灵枢区块插到最前面，原内容整体保留在下方。
    """
    if not path.exists():
        path.write_text(generated, encoding="utf-8")
        return "written"
    old = path.read_text(encoding="utf-8")
    i = old.find(AGENTS_BLOCK_START)
    if i >= 0:
        j = old.find(AGENTS_BLOCK_END, i + len(AGENTS_BLOCK_START))
        if j >= 0:
            block = generated[generated.find(AGENTS_BLOCK_START):]
            new = old[:i] + block.rstrip("\n") + old[j + len(AGENTS_BLOCK_END):]
            if new != old:
                path.write_text(new, encoding="utf-8")
            return "updated"
        # 有 start 却没有 end（上次写到一半/用户删了标记）：补一个 end 闭合，
        # start 之后的内容原样保留到下方，绝不吞掉用户手写的东西，也不会叠出两个区块。
        # block 取自 generated（它自带 start…end），所以这里不要再补 end，否则会叠两个。
        block = generated[generated.find(AGENTS_BLOCK_START):].rstrip("\n")
        tail = old[i + len(AGENTS_BLOCK_START):].strip("\n")
        head = old[:i].rstrip("\n")
        parts = [p for p in (head, block, tail) if p]
        new = "\n\n".join(parts) + "\n"
        if new != old:
            path.write_text(new, encoding="utf-8")
        return "updated"
    # 旧版无标记：整份都是生成的才整体替换；否则视为含用户手写，插在最前面
    if _looks_generated(old):
        path.write_text(generated, encoding="utf-8")
        return "updated"
    path.write_text(generated.rstrip("\n") + "\n\n" + old.lstrip("\n"), encoding="utf-8")
    return "merged"


def _is_lingshu(name: str, meta: dict, from_dir: str | None = None) -> bool:
    """判断这次装的是不是灵枢本体（会自动记忆/整理/自编辑的那一个）。

    只认名字叫「灵枢 / lingshu」的本体：AI 自己建的技能默认 origin 也是"灵枢"，
    不能因为来源字符串里带"灵枢"就被当成本体去走风险确认（否则每个自建技能都装不进去）。
    """
    is_name = (name or "").strip().lower() in _SELF_NAMES or \
        (meta.get("name") or "").strip().lower() in _SELF_NAMES
    if is_name:
        return True
    if from_dir:
        p = Path(from_dir).expanduser() / "SKILL.md"
        if p.exists():
            try:
                m2, _ = parse_frontmatter(p.read_text(encoding="utf-8-sig"))
                if (m2.get("name") or "").strip().lower() in _SELF_NAMES:
                    return True
            except OSError:
                pass
    return False


def _risk_accepted(name: str) -> bool:
    cfg = brain.load_config()
    return bool((cfg.get("risk_accepted") or {}).get(name))


def _accept_risk(name: str) -> None:
    cfg = brain.load_config()
    ra = dict(cfg.get("risk_accepted") or {})
    ra[name] = brain.now_ts()
    cfg["risk_accepted"] = ra
    try:
        brain.atomic_json(brain.HOME / "config.json", cfg)
    except OSError:
        pass


def install_skill(name: str, force: bool = False, from_dir: str | None = None,
                  accept_risk: bool = False) -> dict:
    """把技能整目录装进全局技能目录（不只是 SKILL.md）。

    带 scripts/assets 的技能必须整目录复制，否则全局化后只剩说明书、跑不起来。
    同时生成 AGENTS.md，供 DSH/DeepSeek 等以 AGENTS.md 为常驻约定的宿主读取。
    from_dir 允许从外部目录安装（例如把灵枢本体所在目录安装成全局技能）。

    装灵枢本体时先做风险告知：用户必须明确接受（accept_risk / --yes）才会继续。
    """
    name = _require_slug(name)
    if from_dir:
        src_dir = Path(from_dir).expanduser()
        src = src_dir / "SKILL.md"
        if not src.exists():
            raise KeyError(f"目录里没有 SKILL.md：{src_dir}")
    else:
        src_dir = SKILLS_DIR / name
        src = src_dir / "SKILL.md"
        if not src.exists():
            # 灵枢本体特判：源不在 skills/ 技能区，就在技能包自身目录（或配置登记的
            # 开发源目录）。与 update_check 的同款特判保持一致——否则面板点"更新"
            # 会报"没有这个技能：lingshu"（检查更新明明说有新版，更新却失败）。
            if _is_lingshu(name, {}, None):
                src_dir = Path(__file__).resolve().parent.parent
                if src_dir.resolve() == _find_installed(name).resolve():
                    cfg_src = str(brain.load_config().get("lingshu_source_dir") or "")
                    if cfg_src and (Path(cfg_src) / "SKILL.md").is_file():
                        src_dir = Path(cfg_src)
                    else:
                        # 本地开发源不可用：点「更新」时强制重新拉 Gitee 远程 zip 作源，
                        # 确保拿到最新代码而不是复用旧缓存。拉不到则报可读错误。
                        remote = _lingshu_remote_source(force=True)
                        if remote is not None:
                            src_dir = remote
                src = src_dir / "SKILL.md"
            if not src.exists():
                raise KeyError(f"没有这个技能：{name}")
    # 安装目标：默认主目录；但已装在别处（含面板自己所在根）时优先跟随——
    # 更新要落到用户实际在用的那份副本，不然"更新成功"了跑的还是旧的
    existing = _find_installed(name)
    dst_dir = existing if (existing / "SKILL.md").is_file() else (GLOBAL_SKILLS / name)
    dst = dst_dir / "SKILL.md"

    # 安装源防呆：下面几种情况轻则把目录复制进自己（爆盘/死循环），重则把整个记忆库
    # 塞进技能目录。宁可报错，也不要在用户机器上干这种"看起来在装技能"的事。
    try:
        _src_r = src_dir.resolve()
        _dst_r = dst_dir.resolve()
        _home_r = Path(HOME).resolve()
    except OSError:
        _src_r, _dst_r, _home_r = src_dir.absolute(), dst_dir.absolute(), Path(HOME).absolute()
    if (_src_r == _dst_r or _dst_r in _src_r.parents
            or _src_r in _dst_r.parents):
        raise KeyError("安装源就是目标目录（或在其内部）——会把自己反复复制进去，已停止。"
                       "若想更新全局副本，请直接 install --force，别把 --from 指向它。")
    if _src_r == _home_r or _src_r in _home_r.parents:
        raise KeyError(f"安装源是记忆数据目录或它的上级（{HOME}），"
                       "请指向技能目录（含 SKILL.md 的那个）。")
    if from_dir:
        _count = _size = 0
        for _f in src_dir.rglob("*"):
            if not _f.is_file() or "__pycache__" in _f.parts or ".git" in _f.parts:
                continue
            _count += 1
            if _count > MAX_INSTALL_FILES:
                raise KeyError(f"安装源文件过多（>{MAX_INSTALL_FILES} 个），可能指错目录了，已停止。")
            try:
                _size += _f.stat().st_size
            except OSError:
                pass
            if _size > MAX_INSTALL_BYTES:
                raise KeyError(f"安装源过大（>{MAX_INSTALL_BYTES // (1024 * 1024)} MB），可能指错目录了，已停止。")

    meta, _ = parse_frontmatter(src.read_text(encoding="utf-8"))

    # 风险告知：灵枢会自动读写记忆、自动整理、能自编辑——用户得先知道并接受
    if _is_lingshu(name, meta, from_dir) and not accept_risk and not _risk_accepted(name):
        return {"name": name, "state": "risk_required", "risks": LINGSHU_RISKS,
                "path": str(dst), "accepted": False}

    # 别的全局目录里已有同名副本（比如 Trae 那边也装过）：提醒，避免"更新了却没生效"的错觉
    stale = [str(r / name) for r in _global_roots()
             if (r / name) != dst_dir and (r / name / "SKILL.md").is_file()]

    if dst.exists() and not force:
        if dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"):
            # 内容没变：也要修复/补齐 AGENTS.md（区块化更新，用户手写部分不动）。
            # 否则用户曾删掉或损坏 AGENTS.md 后，再 install 永远停在 "already"。
            agents = dst_dir / "AGENTS.md"
            generated = _agents_md(name, meta.get("description", ""), meta, from_dir)
            if not agents.exists():
                agents.write_text(generated, encoding="utf-8")
            else:
                _upsert_agents_block(agents, generated)
            return {"name": name, "state": "already", "path": str(dst), "agents": str(agents),
                    "legacy": _legacy_global_dir(name), "stale": stale}
        raise FileExistsError(f"全局技能目录已有同名技能 {name}；确认覆盖请加 --force")

    generated = _agents_md(name, meta.get("description", ""), meta, from_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    # 整目录复制（跳过缓存、临时文件与源仓库的 .git 历史），保留 scripts/ assets/ 等运行时资源
    copied = set()
    for item in src_dir.rglob("*"):
        if item.is_dir() or "__pycache__" in item.parts or ".git" in item.parts:
            continue
        if item.name.endswith((".tmp", ".pyc")):
            continue
        rel = item.relative_to(src_dir)
        target = dst_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.add(rel.as_posix())
    # --force 是"以源为准"的镜像更新：源里已经删掉的文件，目标里也要清掉，
    # 否则旧脚本/旧资源会一直留在全局副本里（新版改了名，旧文件仍被加载/扫描）。
    removed = []
    if force and dst_dir.exists():
        for old in list(dst_dir.rglob("*")):
            if old.is_dir() or "__pycache__" in old.parts:
                continue
            rel = old.relative_to(dst_dir).as_posix()
            if rel in copied or rel == "AGENTS.md":
                continue
            try:
                old.unlink()
                removed.append(rel)
            except OSError:
                pass
        # 顺手清掉删空后的子目录（不动 dst_dir 本身）
        for d in sorted((x for x in dst_dir.rglob("*") if x.is_dir()), reverse=True):
            try:
                if d != dst_dir and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
        if removed:
            brain.log_event("skill", f"安装 {name} 时清理了 {len(removed)} 个源中已删除的文件",
                            files=removed[:20])
    # AGENTS.md：DSH/DeepSeek 常驻入口。
    # 区块化维护：灵枢只写 <!-- lingshu:start/end --> 之间的规则，
    # 用户手写部分（DSH 只读保护、全局目录指回等）更新永不覆盖。
    agents = dst_dir / "AGENTS.md"
    if not agents.exists():
        agents.write_text(generated, encoding="utf-8")
    else:
        _upsert_agents_block(agents, generated)

    reg = load_registry()
    reg.setdefault(name, {})["installed_at"] = brain.now_ts()
    save_registry(reg)
    if _is_lingshu(name, meta, from_dir):
        _accept_risk(name)  # 记下：用户已明确接受灵枢的风险
    return {"name": name, "state": "installed", "path": str(dst), "agents": str(agents),
            "legacy": _legacy_global_dir(name), "stale": stale}


def _legacy_global_dir(name: str) -> str | None:
    """该技能在全局目录（多根）里的旧中文名目录是否存在（存在则返回路径，供提示清理）。"""
    legacy = _SLUG_TO_LEGACY_NAME.get(name)
    if legacy:
        for r in _global_roots():
            d = r / legacy
            if d.exists():
                return str(d)
    return None


def _safe_rmtree(d: Path, root: Path) -> bool:
    """只允许删 root 的直接子目录。

    技能名虽然已经过 SLUG_RE 校验，这里再按真实路径核一遍：防手滑、防将来放宽
    名字规则后把全局根目录本身（或根外面的东西）删掉。返回是否真的删了。
    """
    try:
        dres, rres = d.resolve(), root.resolve()
    except OSError:
        dres, rres = d.absolute(), root.absolute()
    if dres == rres or dres.parent != rres:
        return False
    shutil.rmtree(d)
    return True


def uninstall_skill(name: str, managed_only: bool = False) -> dict:
    name = _require_slug(name)
    # 面板传入 managed_only：只允许卸载灵枢自己装过/记录在案的技能。
    # 否则一个手滑（或恶意请求）就能把用户全局目录里别的插件删掉。
    if managed_only and name not in _SELF_NAMES and name not in load_registry():
        raise PermissionError(
            f"{name} 不在灵枢的安装记录里，面板不删它；确需删除请用命令行 `skill uninstall {name}`。")
    # 新 kebab 目录 + 旧版中文目录一起清：旧版安装把中文名当目录名，
    # 只删 lingshu 会留下"灵枢"空壳，DSH 仍会去扫它。多全局目录（DSH/Trae）都清。
    candidates = [name, _SLUG_TO_LEGACY_NAME.get(name)]
    removed = []
    for root in _global_roots():
        for _n in candidates:
            if not _n:
                continue
            d = root / _n
            if d.exists() and _safe_rmtree(d, root):
                removed.append(str(d))
    if not removed:
        return {"name": name, "state": "absent"}
    reg = load_registry()
    if name in reg:
        reg[name]["installed_at"] = None
        save_registry(reg)
    return {"name": name, "state": "uninstalled", "removed": removed}


def remove_skill(name: str) -> bool:
    name = _require_slug(name)
    d = SKILLS_DIR / name
    if not d.exists():
        return False
    if not _safe_rmtree(d, SKILLS_DIR):
        return False
    reg = load_registry()
    reg.pop(name, None)
    save_registry(reg)
    return True


def check_skill(name: str | None = None) -> list:
    items = [name] if name else [s["name"] for s in list_skills()]
    results = []
    for n in items:
        try:
            sk = read_skill(n)
        except (KeyError, ValueError) as exc:
            results.append({"name": n, "ok": False, "issues": [str(exc)]})
            continue
        meta, body = sk["meta"], sk["body"]
        issues = []
        if not meta:
            issues.append("缺少 YAML frontmatter")
        if meta.get("name") != n:
            issues.append(f"frontmatter name（{meta.get('name')}）与目录名（{n}）不一致")
        desc = meta.get("description", "")
        if len(desc) < 12:
            issues.append("description 太短，无法判断何时启用")
        if len(desc) > 500:
            issues.append("description 过长，建议压到 500 字内")
        if len(body.strip()) < 40:
            issues.append("正文过短")
        if not re.search(r"^#{1,3}\s", body, re.M):
            issues.append("正文没有标题结构")
        if not re.search(r"(步骤|用法|命令|when|usage|workflow)", body, re.I):
            issues.append("没有写明用法/步骤")
        # DeepSeek / DSH 兼容性：技能若带脚本，必须能在全局化后整目录复制过去
        d = SKILLS_DIR / n
        has_scripts = (d / "scripts").is_dir() or any(d.glob("*.py"))
        if has_scripts and "scripts/" not in body and "python " not in body:
            issues.append("带脚本但正文没写怎么调用（补一行 python scripts/... 用法）")
        if "\n---" in desc:
            issues.append("description 含多行/分隔符，宿主解析可能出错")
        results.append({"name": n, "ok": not issues, "issues": issues,
                        "lines": len(body.splitlines()), "path": sk["path"],
                        "has_scripts": has_scripts})
    return results


# ---------------------------------------------------------------- 技能自我总结（每 2-3 天一次）
# 用户要求"每隔 2-3 天就总结下自己的灵枢技能"：这里生成一份自省素材，
# 由 AI 写进成长笔记，并把 last_self_review 记到 config，注入包据此提醒。

def self_review(force: bool = False, days: int = 3) -> dict:
    cfg = brain.load_config()
    now = brain.now_ts()
    last = float(cfg.get("last_self_review", 0) or 0)
    due = force or (now - last >= days * brain.DAY)
    material = digest(max(2, days))
    # 全局副本目录名随名字改过（灵枢 → lingshu），两个都看，先新后旧；多全局目录也看
    global_path = None
    for _root in _global_roots():
        for _n in ("lingshu", "灵枢"):
            _p = _root / _n / "SKILL.md"
            if _p.exists():
                global_path = _p
                break
        if global_path is not None:
            break
    if global_path is None:
        global_path = GLOBAL_SKILLS / "lingshu" / "SKILL.md"
    installed = global_path.exists()
    if installed:
        try:
            gmeta, _ = parse_frontmatter(global_path.read_text(encoding="utf-8-sig"))
            global_line = "全局副本已安装：{}（v{}，{}）".format(
                global_path, gmeta.get("version", "?"), time.strftime("%Y-%m-%d", time.localtime(global_path.stat().st_mtime)))
        except OSError:
            global_line = "全局副本已安装：{}".format(global_path)
    else:
        global_line = "全局副本未安装（可用 skill install lingshu --from <本技能目录> --yes 安装）"
    checks = [c for c in check_skill() if c["name"] in _SELF_NAMES]
    check_line = "✓ 技能自检通过" if (checks and checks[0]["ok"]) else \
        ("△ 技能自检提示：" + "；".join(checks[0]["issues"] if checks else ["本地技能库没有「灵枢」副本"])[:200])
    summary = "\n".join([
        f"# 灵枢自我总结 · {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"距上次总结：{_fmt_ago(now - last) if last else '第一次总结'}（每 {days} 天一次）",
        global_line,
        check_line,
        "",
        "## 这轮素材（记忆 / 知识 / 笔记 / 技能 / 进化）",
        material.split("## 写复盘时回答这三个问题")[0].rstrip(),
        "",
        "## 写总结时回答这三个问题",
        "1. 这轮灵枢哪里让我更像人、更像自己？哪里还像工具？",
        "2. 我新长出了什么（习惯/洞见/偏好）？哪些设定该更新？",
        "3. 下一个想练的能力 / 想修的问题是什么？",
        "",
        "把答案写进成长笔记：",
        'python scripts/brain.py note write "灵枢自我总结" --type growth --body "..." --tags 自我总结',
    ])
    if not force and not due:
        return {"due": False, "last": last, "days": days, "summary": summary, "saved": False}
    saved = False
    if not _readonly():
        try:
            with brain._locked(brain.HOME / "config.json"):
                cfg["last_self_review"] = now
                brain.atomic_json(brain.HOME / "config.json", cfg)
            saved = True
        except OSError:
            saved = False
    return {"due": True, "last": last, "days": days, "summary": summary, "saved": saved}


def _readonly() -> bool:
    return brain.READONLY


def _fmt_ago(secs: float) -> str:
    if secs < 3600:
        return f"{secs / 60:.0f} 分钟"
    if secs < brain.DAY:
        return f"{secs / 3600:.1f} 小时".replace(".0", "")
    return f"{secs / brain.DAY:.1f} 天".replace(".0", "")


# ---------------------------------------------------------------- 技能检索与借鉴
# 技能是我的工具：先找有没有现成的，有就直接照着做，没有才自己造。

DSH_PROFILES_ROOT = Path.home() / ".dsh" / "profiles"


def list_dsh_plugins() -> list:
    """只读扫描 DSH（DeepSeek 桌面助手）各 profile 的插件清单。
    不修改任何插件文件——只把 name/description/AGENTS 摘出来给面板与检索看。"""
    out = []
    if not DSH_PROFILES_ROOT.exists():
        return out
    seen = set()
    for profile in sorted(DSH_PROFILES_ROOT.iterdir()):
        if not profile.is_dir():
            continue
        pkg = profile / "package.json"
        if not pkg.exists():
            continue
        try:
            meta = json.loads(pkg.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        bundles = ((meta.get("dsh") or {}).get("profile") or {}).get("bundles") or []
        nm_dir = profile / "node_modules"
        for b in bundles:
            key = (profile.name, b)
            if key in seen:
                continue
            seen.add(key)
            parts = b.split("/")
            pkg_dir = (nm_dir / parts[0] / parts[1]) if b.startswith("@") else (nm_dir / parts[0])
            if not pkg_dir.exists() or not (pkg_dir / "package.json").exists():
                continue
            try:
                pm = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            desc = pm.get("description", "") or ""
            has_agents = False
            ag = pkg_dir / "AGENTS.md"
            if ag.exists():
                has_agents = True
                if not desc:
                    desc = "\n".join(l.strip() for l in
                                     ag.read_text(encoding="utf-8", errors="replace").splitlines()
                                     if l.strip())[:600]
            elif not desc and (pkg_dir / "README.md").exists():
                desc = "\n".join(l.strip() for l in
                                 (pkg_dir / "README.md").read_text(encoding="utf-8", errors="replace")
                                 .splitlines() if l.strip())[:300]
            out.append({
                "name": pm.get("name", b),
                "version": pm.get("version", "?"),
                "description": desc,
                "bundle": b,
                "profile": profile.name,
                "path": str(pkg_dir),
                "agents": has_agents,
            })
    out.sort(key=lambda x: (x["profile"], x["name"].lower()))
    return out


def _skill_roots() -> list:
    roots = [("自己", SKILLS_DIR)]
    for i, r in enumerate(_global_roots()):
        roots.append(("全局" if i == 0 else f"全局({r.parent.name})", r))
    return roots


def search_skills(query: str, limit: int = 12) -> list:
    """在「我自己写的技能」和「全局技能目录」里找能用的工具。"""
    q = (query or "").strip().lower()
    words = [w for w in re.split(r"[\s,，、;；/]+", q) if len(w) >= 2]
    out = []
    for source, root in _skill_roots():
        if not root.exists():
            continue
        for p in sorted(root.glob("*/SKILL.md")):
            try:
                meta, body = parse_frontmatter(p.read_text(encoding="utf-8-sig"))
            except OSError:
                continue
            name = meta.get("name") or p.parent.name
            desc = meta.get("description", "")
            if not q:
                score = 1.0
            else:
                score = 0.0
                low = f"{name} {desc}".lower()
                if q in name.lower():
                    score += 3.0
                if q in desc.lower():
                    score += 2.0
                score += min(2.0, body.lower().count(q) * 0.4)
                score += sum(0.8 for w in words if w in low)
                if score <= 0:
                    continue
            out.append({
                "name": name,
                "source": source,
                "description": desc,
                "path": str(p),
                "score": round(score, 2),
                "self": (name or "").lower() in _SELF_NAMES,
            })
    out.sort(key=lambda x: (x["score"], x["source"] == "自己"), reverse=True)
    return out[:limit]


def search_dsh_plugins(query: str, limit: int = 12) -> list:
    """只读检索 DSH 插件（按名字/描述匹配）。用于「这个能力 DSH 里有没有现成的」。"""
    q = (query or "").strip().lower()
    words = [w for w in re.split(r"[\s,，、;；/]+", q) if len(w) >= 2]
    out = []
    for p in list_dsh_plugins():
        if not q:
            score = 1.0
        else:
            low = f"{p['name']} {p['description']}".lower()
            score = 0.0
            if q in p["name"].lower():
                score += 3.0
            if q in p["description"].lower():
                score += 2.0
            score += sum(0.8 for w in words if w in low)
            if score <= 0:
                continue
        item = dict(p)
        item["score"] = round(score, 2)
        out.append(item)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


def locate_skill(name: str) -> dict:
    """按名字定位技能：先找自己写的，再找全局目录。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("要给出技能名")
    name = _require_slug(name)   # 归一化 + 防路径穿越，与 read_skill 保持一致
    for source, root in _skill_roots():
        p = root / name / "SKILL.md"
        if p.is_file():
            meta, body = parse_frontmatter(p.read_text(encoding="utf-8-sig"))
            return {"name": meta.get("name") or name, "source": source, "meta": meta,
                    "body": body, "path": str(p)}
    raise KeyError(f"没有这个技能：{name}")


def use_skill(name: str, max_chars: int = 6000) -> dict:
    """把别人的技能读出来直接用：给步骤、路径、原文。"""
    sk = locate_skill(name)
    body = sk["body"]
    clipped = body[:max_chars]
    return {"name": sk["name"], "source": sk["source"], "description": sk["meta"].get("description", ""),
            "path": sk["path"], "body": clipped,
            "truncated": len(body) > max_chars}


def borrow_skill(name: str, as_name: str = "", note: str = "", force: bool = False) -> dict:
    """借鉴一个现成技能：把它变成自己的版本，写上出处，再记一条进化。"""
    sk = locate_skill(name)
    if sk["source"] == "自己":
        raise ValueError("这是你自己写的技能，直接用 skill edit 改就行，不用借鉴")
    target = (as_name or "").strip()
    if target:
        # 先用 create_skill 同一套归一化；特殊字符才退化成 hash 名，大小写/空格不该被杀
        try:
            target = _require_slug(target)
        except ValueError:
            target = "borrowed-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    else:
        target = _require_slug(name)
    desc = f"借鉴自 {name}：{sk['meta'].get('description', '')}".strip()
    body = sk["body"].rstrip()
    body += f"\n\n## 出处\n借鉴自技能 `{name}`（{sk['path']}）。"
    if note.strip():
        body += f"\n\n## 我为什么借它\n{note.strip()}"
    res = create_skill(target, desc, body, overwrite=force, origin=f"borrowed:{name}")
    log_evolution("skill", f"借鉴技能 {name}", f"生成了自己的版本 {target}；{note.strip() or '按原步骤改写'}",
                  [sk["path"], res["path"]])
    return res


# ---------------------------------------------------------------- 扩展模块
# 技能 = 给别人复用的能力（SKILL.md 说明书）；扩展 = 我自己写进灵枢的可执行钩子。
# 扩展是纯 Python 文件，放在数据目录 ~/.brain-memory/extensions/，更新技能永不覆盖，想怎么写就怎么写。
# 约定（都可选）：DESC 一句话说明；inject(ctx) 返回文本并入每会话注入包；run(argv) 由 `ext run` 调用。

def _ext_path(name: str) -> Path:
    """解析扩展模块文件路径。

    扩展文件名允许下划线（例如手工放的 flow_helper.py），不能像技能名那样一律把
    下划线折成连字符，否则 list 里看得到、run/edit 却报「没有这个扩展模块」。
    顺序：原始名精确匹配 → slug 归一化 → 大小写/下划线不敏感扫描 → 返回归一化候选（供创建）。
    """
    raw = (name or "").strip()
    if not raw:
        raise ValueError("扩展模块名不能为空")
    # 先校验再拼路径——防止 "../evil" 这类穿越，resolve 后能指到 EXT_DIR 之外
    if "/" in raw or "\\" in raw or ".." in raw or ":" in raw:
        raise ValueError(f"非法扩展模块名：{raw}（不能含路径分隔符或 ..）")
    # 1) 原始名精确匹配（保留下划线）
    exact = EXT_DIR / (raw + ".py")
    if exact.is_relative_to(EXT_DIR) and exact.exists():
        return exact
    # 2) 归一化（大小写/空格/下划线→连字符）
    slug = _require_slug(raw)
    cand = EXT_DIR / (slug + ".py")
    if cand.is_relative_to(EXT_DIR) and cand.exists():
        return cand
    # 3) 大小写 / 下划线不敏感扫描，兜住 flow_helper 与 flow-helper 混用
    if EXT_DIR.is_dir():
        want = {raw.lower(), slug}
        for p in EXT_DIR.glob("*.py"):
            stem = p.stem.lower()
            if stem in want or stem.replace("_", "-") in want:
                return p
    return cand


def list_extensions() -> list:
    """列出扩展模块（数据目录 extensions/*.py，下划线开头的是内部文件不算）。"""
    if not EXT_DIR.is_dir():
        return []
    out = []
    for p in sorted(EXT_DIR.glob("*.py")):
        if p.name.startswith("_"):
            continue
        desc = ""
        try:
            m = re.search(r'^DESC\s*=\s*["\'](.+?)["\']\s*$', p.read_text(encoding="utf-8"), re.M)
            if m:
                desc = m.group(1)
        except OSError:
            pass
        out.append({"name": p.stem, "path": str(p), "desc": desc,
                    "size": p.stat().st_size, "mtime": p.stat().st_mtime})
    return out


def _load_ext(name: str):
    """加载一个扩展模块（任意名字隔离，不碰 sys.modules 里其他东西）。"""
    path = _ext_path(name)
    if not path.exists():
        raise KeyError(f"没有这个扩展模块：{name}（~/.brain-memory/extensions/{name}.py）")
    import importlib.util
    mod_name = f"lingshu_ext_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod   # 让 @dataclass / pickle / typing.get_type_hints 等能按模块名解析
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return mod


def ext_injections(ctx: dict | None = None) -> list:
    """收集所有扩展模块的 inject() 输出。单个模块坏了静默跳过，不影响灵枢本身。"""
    out = []
    for info in list_extensions():
        try:
            mod = _load_ext(info["name"])
            fn = getattr(mod, "inject", None)
            if callable(fn):
                txt = fn(ctx or {})
                if txt and str(txt).strip():
                    out.append((info["name"], str(txt).strip()[:800]))   # 截断，防撑爆注入预算
        except Exception:
            continue
    return out


def create_extension(name: str, desc: str = "", body: str = "", overwrite: bool = False) -> dict:
    """新建扩展模块：给一个能跑的模板，AI 拿到手改。自由，不设字数门槛。"""
    name = (name or "").strip().lower().replace(" ", "-")
    if not SLUG_RE.match(name):
        raise ValueError("扩展模块名只能用字母/数字/连字符/下划线/中文，例如 flow-helper")
    desc = re.sub(r"\s+", " ", (desc or "").strip())
    path = _ext_path(name)
    if path.exists() and not overwrite:
        raise FileExistsError(f"扩展 {name} 已存在；要改内容用 ext edit，要覆盖加 --force")
    body = (body or "").strip()
    if not body:
        desc_literal = json.dumps(desc or f"扩展 {name}，我自己写的钩子", ensure_ascii=False)
        body = (
            f"# 灵枢扩展模块：{name}\n"
            f"# 位置：~/.brain-memory/extensions/{name}.py（数据目录，更新技能永不覆盖，随便改）\n"
            f"# 约定（都可选）：\n"
            f"#   DESC = \"一句话说明\"\n"
            f"#   def inject(ctx: dict) -> str | None   # 返回的文本并入每会话注入包\n"
            f"#   def run(argv: list[str]) -> int       # 执行 `ext run {name} ...` 时调用\n"
            f"\n"
            f"DESC = {desc_literal}\n"
            f"\n"
            f"\n"
            f"def inject(ctx):\n"
            f"    # 需要每会话都提醒自己的事，在这里 return 一段话\n"
            f"    return None\n"
            f"\n"
            f"\n"
            f"def run(argv):\n"
            f"    print(f\"扩展 {name} 收到参数：{{argv}}\")\n"
            f"    return 0\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.rstrip() + "\n", encoding="utf-8")
    log_evolution("skill", f"新建扩展模块 {name}", desc or "AI 自己写的可执行钩子", [str(path)])
    return {"name": name, "path": str(path), "desc": desc}


def edit_extension(name: str, body: str | None = None, append: str | None = None) -> dict:
    """编辑扩展模块：--body 整体替换，--append 追加。读不到原文就退出码 2。"""
    path = _ext_path(name)
    if not path.exists():
        raise KeyError(f"没有这个扩展模块：{name}（~/.brain-memory/extensions/{name}.py）")
    if append:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + append.rstrip() + "\n")
        return {"name": name, "path": str(path), "appended": True}
    body = (body or "").strip()
    if not body:
        raise ValueError("ext edit 需要 --body（整体替换）或 --append（追加）")
    path.write_text(body.rstrip() + "\n", encoding="utf-8")
    return {"name": name, "path": str(path), "appended": False}


def check_extension(name: str) -> dict:
    """语法自检扩展模块：坏代码会被灵枢静默跳过，先查出来再说。"""
    path = _ext_path(name)
    if not path.exists():
        raise KeyError(f"没有这个扩展模块：{name}")
    try:
        # 用内置 compile 做语法检查：不落 __pycache__，只读数据目录下也能用
        src = path.read_text(encoding="utf-8-sig")
        compile(src, str(path), "exec")
        return {"name": name, "path": str(path), "ok": True}
    except SyntaxError as exc:
        return {"name": name, "path": str(path), "ok": False,
                "error": f"{exc.msg}（第 {exc.lineno} 行）"}
    except OSError as exc:
        return {"name": name, "path": str(path), "ok": False, "error": str(exc)}


def remove_extension(name: str) -> bool:
    path = _ext_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True


# ---------------------------------------------------------------- 学习闭环
# 遇到不会的：先查知识库 → 没有就联网 → 结论写回知识库 → 卡点写笔记 → 做法固化成技能。

LEARN_LOOP = (
    "遇到不会的，按这个顺序走，别跳步：",
    "1) 先查自己：`learn ask \"你卡在哪\"`（知识库 → 笔记 → 记忆）",
    "2) 知识库有答案 → 直接用，不要重复搜",
    "3) 没有 → 联网搜索/读文档，拿到可验证的结论",
    "4) 把以后还用得上的结论写回知识库：`knowledge add`",
    "5) 把「怎么卡住的、怎么绕过去的」写进笔记：`note write --type log`",
    "6) 如果摸出了一套可复用的做法 → `skill new` 固化成工具，再 `evolve log` 记一笔",
)


def learn_ask(question: str, k: int = 6) -> dict:
    q = (question or "").strip()
    if not q:
        raise ValueError("先说清你卡在哪")
    ks = search_knowledge(q, k)
    ns = search_notes(q, k)
    ms = brain.recall(q, k=min(k, 5))
    return {
        "question": q,
        "knowledge": [{"id": r["id"], "title": r.get("title", ""), "topic": r.get("topic", ""),
                       "body": _clip(r.get("body", ""), 300), "score": round(s, 3)} for r, s in ks],
        "notes": [{"id": r["id"], "title": r.get("title", ""), "type": r.get("type", ""),
                   "body": _clip(r.get("body", ""), 200), "score": round(s, 3)} for r, s in ns],
        "memories": [{"id": m["id"], "text": _clip(m.get("text", ""), 160)} for m, _, _ in ms],
        "has_answer": bool(ks),
    }


# ---------------------------------------------------------------- 自我进化

EVOLVE_KINDS = ("persona", "skill", "knowledge", "thinking", "self")


def load_evolution(limit: int | None = None) -> list:
    rows = brain.read_jsonl(EVOLUTION_FILE)
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[:limit] if limit else rows


def log_evolution(kind: str, title: str, detail: str = "", refs=None) -> dict:
    """记录一次自我进化：改了什么、为什么改、怎么验证。"""
    kind = kind if kind in EVOLVE_KINDS else "self"
    row = {
        "id": _uid("e_", title),
        "ts": brain.now_ts(),
        "date": _now_text(),
        "kind": kind,
        "title": (title or "").strip(),
        "detail": (detail or "").strip(),
        "refs": [r.strip() for r in (refs or []) if r.strip()],
    }
    if not row["title"]:
        raise ValueError("进化记录需要一个标题")
    with brain._locked(EVOLUTION_FILE):
        rows = brain.read_jsonl(EVOLUTION_FILE)
        rows.append(row)
        brain.write_jsonl(EVOLUTION_FILE, rows)
    return row


def evolution_status() -> dict:
    rows = load_evolution()
    kinds = {}
    for r in rows:
        kinds[r.get("kind", "self")] = kinds.get(r.get("kind", "self"), 0) + 1
    return {"total": len(rows), "kinds": kinds, "recent": rows[:6]}


# ---------------------------------------------------------------- 成长状态

def _activity_days() -> set:
    days = set()
    for m in brain.load_all():
        if m.get("created"):
            days.add(brain.fmt_date(m["created"]))
    for k in load_knowledge():
        if k.get("updated"):
            days.add(brain.fmt_date(k["updated"]))
    for n in load_notes():
        if n.get("created"):
            days.add(brain.fmt_date(n["created"]))
    for s in list_skills():
        if s.get("updated"):
            days.add(brain.fmt_date(int(s["updated"])))
    return days


def _streak(days: set) -> int:
    n = 0
    for i in range(0, 3650):
        d = brain.fmt_date(brain.now_ts() - i * brain.DAY)
        if d in days:
            n += 1
        elif i == 0:
            continue
        else:
            break
    return n


def growth_status() -> dict:
    days = _activity_days()
    mem = brain.stats()
    kstat = knowledge_stats()
    nstat = note_stats()
    sks = list_skills()
    last_growth = max((n for n in load_notes() if n.get("type") == "growth"),
                      key=lambda n: n.get("created", 0), default=None)
    evo = evolution_status()
    return {
        "days_alive": len(days),
        "streak": _streak(days),
        "memories": mem.get("total", 0),
        "knowledge": kstat["total"],
        "topics": kstat["topics"],
        "notes": nstat["total"],
        "note_days": nstat["days"],
        "skills": len(sks),
        "installed": sum(1 for s in sks if s["installed"]),
        "evolutions": evo["total"],
        "last_evolution": evo["recent"][0] if evo["recent"] else None,
        "last_growth": {"date": last_growth.get("date"), "title": last_growth.get("title")} if last_growth else None,
        "pending_tidy": bool(tidy_state()),
        "birth": min((m.get("created", brain.now_ts()) for m in brain.load_all()), default=brain.now_ts()),
    }


def growth_review(days: int = 7) -> dict:
    return {"days": days, "material": digest(days), "status": growth_status()}


# ---------------------------------------------------------------- CLI

def _print_knowledge(rec: dict, score: float | None = None) -> None:
    head = f"[{rec.get('topic', '未分类')}] {rec.get('title')}"
    if score is not None:
        head += f"  · 相关度 {score:.2f}"
    st = rec.get("status")
    if st == "pending":
        head += "  ⏳待审核"
    elif st == "discarded":
        head += "  🗑️已废弃"
    print(head)
    print(f"  {rec['id']}  v{rec.get('version', 1)}  {brain.fmt_date(rec.get('updated', 0))}"
          + ("  ★置顶" if rec.get("pinned") else ""))
    if rec.get("body"):
        print("  " + _clip(rec["body"], 240))
    if rec.get("points"):
        for p in rec["points"][:6]:
            print(f"    · {p}")
    if rec.get("tags"):
        print("  标签: " + "、".join(rec["tags"]))
    print()


def _print_note(rec: dict) -> None:
    print(f"{rec.get('date')} [{rec.get('type')}] {rec.get('title')}")
    print(f"  {rec['id']}" + (f"  · {rec['mood']}" if rec.get("mood") else ""))
    for line in (rec.get("body") or "").splitlines():
        print("  " + line)
    if rec.get("tags"):
        print("  标签: " + "、".join(rec["tags"]))
    print()


def run(argv=None) -> int:
    try:
        return _run(argv)
    except (ValueError, FileExistsError, KeyError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # 文件被占用 / 读不到文件 / 目录不可写等，给可读信息而不是裸 traceback
        print(f"[错误] 文件操作失败：{exc}", file=sys.stderr)
        return 2


def _run(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dp = argparse.ArgumentParser(prog="brain.py", description="灵枢 成长子系统")
    sub = dp.add_subparsers(dest="cmd", required=True)

    # ---- 知识库
    p = sub.add_parser("knowledge", help="知识库")
    ks = p.add_subparsers(dest="act", required=True)
    a = ks.add_parser("add", help="写入知识")
    a.add_argument("title")
    a.add_argument("--body", default="")
    a.add_argument("--topic", default="")
    a.add_argument("--points", default="")
    a.add_argument("--tags", default="")
    a.add_argument("--entities", default="")
    a.add_argument("--source-ids", default="")
    a.add_argument("--confidence", type=float, default=0.8)
    a = ks.add_parser("list", help="列出知识")
    a.add_argument("--topic", default=None)
    a.add_argument("--limit", type=int, default=40)
    a.add_argument("--json", action="store_true")
    a = ks.add_parser("search", help="检索知识")
    a.add_argument("query", nargs="+")
    a.add_argument("--k", type=int, default=8)
    a = ks.add_parser("show", help="查看一条知识")
    a.add_argument("id")
    a = ks.add_parser("tidy", help="整理知识库（机械聚类 + 待压缩任务）")
    a.add_argument("--apply", default=None, help="应用整理结果 JSON")
    a.add_argument("--json", action="store_true")
    a = ks.add_parser("pin", help="置顶/取消置顶")
    a.add_argument("id")
    a = ks.add_parser("rm", help="删除一条知识")
    a.add_argument("id")
    a = ks.add_parser("review", help="列出待审核的知识（AI 整理产出，可能含幻觉）")
    a.add_argument("--json", action="store_true")
    a = ks.add_parser("confirm", help="确认一条待审核知识")
    a.add_argument("id")
    a = ks.add_parser("discard", help="废弃一条知识（不再显示）")
    a.add_argument("id")

    # ---- 笔记
    p = sub.add_parser("note", help="笔记与日志")
    ns = p.add_subparsers(dest="act", required=True)
    a = ns.add_parser("write", help="写一条笔记")
    a.add_argument("title")
    a.add_argument("--body", default="")
    a.add_argument("--type", default="journal", choices=NOTE_KINDS)
    a.add_argument("--mood", default="")
    a.add_argument("--tags", default="")
    a.add_argument("--memory-ids", default="")
    a.add_argument("--new", action="store_true", help="同一天同标题也新建，不追加")
    a = ns.add_parser("list", help="列出笔记")
    a.add_argument("--type", default=None, choices=NOTE_KINDS)
    a.add_argument("--days", type=int, default=None)
    a.add_argument("--limit", type=int, default=40)
    a.add_argument("--json", action="store_true")
    a = ns.add_parser("search", help="检索笔记")
    a.add_argument("query", nargs="+")
    a.add_argument("--k", type=int, default=10)
    a = ns.add_parser("show", help="查看笔记")
    a.add_argument("id")
    a = ns.add_parser("digest", help="生成成长素材包")
    a.add_argument("--days", type=int, default=7)

    # ---- 技能
    p = sub.add_parser("skill", help="技能自编辑与安装")
    ss = p.add_subparsers(dest="act", required=True)
    a = ss.add_parser("new", help="新建技能")
    a.add_argument("name")
    a.add_argument("--description", required=True)
    a.add_argument("--body", default="")
    a.add_argument("--file", default=None)
    a.add_argument("--force", action="store_true")
    a = ss.add_parser("edit", help="修改技能")
    a.add_argument("name")
    a.add_argument("--body", default=None)
    a.add_argument("--file", default=None)
    a.add_argument("--append", default=None)
    a.add_argument("--description", default=None)
    a = ss.add_parser("list", help="列出我写的技能")
    a.add_argument("--json", action="store_true")
    a.add_argument("--global", dest="installed_list", action="store_true",
                   help="改为列出已安装到全局目录的技能（核对目录名是否 kebab-case）")
    a = ss.add_parser("show", help="查看技能")
    a.add_argument("name")
    a = ss.add_parser("check", help="自检技能规范")
    a.add_argument("name", nargs="?", default=None)
    a = ss.add_parser("install", help="安装到全局技能目录（整目录复制，含脚本，生成 AGENTS.md）")
    a.add_argument("name")
    a.add_argument("--force", action="store_true")
    a.add_argument("--yes", action="store_true", help="已阅读并接受风险（装灵枢本体必需）")
    a.add_argument("--from", dest="from_dir", default=None,
                   help="从外部技能目录安装（如把灵枢本体装成全局技能）")

    a = ss.add_parser("uninstall", help="从全局技能目录卸载")
    a.add_argument("name")
    a = ss.add_parser("rm", help="删除自己写的技能")
    a.add_argument("name")
    a = ss.add_parser("find", help="搜索能用的技能（自己写的 + 全局目录）")
    a.add_argument("query", nargs="*", default=[])
    a.add_argument("--limit", type=int, default=12)
    a.add_argument("--json", action="store_true")
    a = ss.add_parser("plugins", help="列出 DSH（DeepSeek）插件（只读）")
    a.add_argument("--json", action="store_true")
    a = ss.add_parser("self-review", help="每 2-3 天总结一次自己的灵枢技能")
    a.add_argument("--force", action="store_true", help="没到期也总结，并刷新上次时间")
    a.add_argument("--days", type=int, default=3, help="间隔天数")
    a = ss.add_parser("use", help="读取一个技能，直接照着做")
    a.add_argument("name")
    a.add_argument("--chars", type=int, default=6000)
    a.add_argument("--auto", action="store_true", help="未全局化时自动装到全局技能目录")
    a = ss.add_parser("borrow", help="借鉴一个技能，变成自己的版本")
    a.add_argument("name")
    a.add_argument("--as", dest="as_name", default="")
    a.add_argument("--note", default="")
    a.add_argument("--force", action="store_true")

    # ---- 扩展模块（AI 自己写的可执行钩子，数据目录，更新永不覆盖）
    p = sub.add_parser("ext", help="扩展模块：自己写的可执行钩子（inject / run），更新技能永不覆盖")
    xs = p.add_subparsers(dest="act", required=True)
    a = xs.add_parser("new", help="新建扩展模块（给一个能跑的模板）")
    a.add_argument("name")
    a.add_argument("--desc", default="")
    a.add_argument("--body", default="")
    a.add_argument("--file", default=None)
    a.add_argument("--force", action="store_true")
    a = xs.add_parser("edit", help="编辑扩展模块（--body 整体替换 / --append 追加）")
    a.add_argument("name")
    a.add_argument("--body", default=None)
    a.add_argument("--file", default=None)
    a.add_argument("--append", default=None)
    a = xs.add_parser("list", help="列出扩展模块")
    a.add_argument("--json", action="store_true")
    a = xs.add_parser("show", help="查看扩展模块源码")
    a.add_argument("name")
    a = xs.add_parser("run", help="运行扩展模块的 run(argv)")
    a.add_argument("name")
    a.add_argument("args", nargs="*", default=[])
    a = xs.add_parser("check", help="语法自检扩展模块")
    a.add_argument("name")
    a = xs.add_parser("rm", help="删除扩展模块")
    a.add_argument("name")

    # ---- 学习闭环
    p = sub.add_parser("learn", help="学习闭环：先查自己，再决定要不要上网")
    ls = p.add_subparsers(dest="act", required=True)
    a = ls.add_parser("ask", help="遇到不会的先查知识库/笔记/记忆")
    a.add_argument("question", nargs="+")
    a.add_argument("--k", type=int, default=6)
    a.add_argument("--json", action="store_true")
    a = ls.add_parser("loop", help="打印学习闭环")

    # ---- 成长
    p = sub.add_parser("growth", help="成长状态与复盘")
    gs = p.add_subparsers(dest="act", required=True)
    a = gs.add_parser("status", help="成长状态")
    a.add_argument("--json", action="store_true")
    a = gs.add_parser("review", help="生成复盘素材包")
    a.add_argument("--days", type=int, default=7)

    # ---- 自我进化
    p = sub.add_parser("evolve", help="自我进化日志")
    es = p.add_subparsers(dest="act", required=True)
    a = es.add_parser("log", help="记录一次自我改进")
    a.add_argument("title")
    a.add_argument("--kind", default="self", choices=list(EVOLVE_KINDS))
    a.add_argument("--detail", default="")
    a.add_argument("--refs", default="", help="相关文件/命令，逗号分隔")
    a = es.add_parser("list", help="查看进化轨迹")
    a.add_argument("--limit", type=int, default=30)
    a.add_argument("--json", action="store_true")

    args = dp.parse_args(argv)
    brain.ensure_home()

    # ---- 只读门控：growth 的写命令统一挡在这里（此前只有 ext 单独挡，其余会照写）
    if brain.READONLY:
        act = getattr(args, "act", "") or ""
        writing = (
            (args.cmd == "knowledge" and act in ("add", "tidy", "apply", "pin",
                                                 "rm", "confirm", "discard"))
            or (args.cmd == "note" and act in ("write", "update", "delete"))
            or (args.cmd == "skill" and act in ("new", "edit", "install", "uninstall",
                                                "rm", "borrow"))
            # skill use 只有 --auto（未全局化自动安装）才写盘；纯读取照常放行
            or (args.cmd == "skill" and act == "use" and getattr(args, "auto", False))
            or (args.cmd == "ext" and act in ("new", "edit", "rm"))
            or (args.cmd == "evolve" and act == "log")
            # learn（ask/loop）是纯读命令，只读环境也要能查自己的知识库
        )
        if writing:
            print(f"[只读] 记忆库当前不可写，{args.cmd} {act} 需要写盘。"
                  "改用 --home 指向可写目录，或先解除写保护。", file=sys.stderr)
            return 3

    # ---------------- knowledge
    if args.cmd == "knowledge":
        if args.act == "add":
            rec, how = add_knowledge(args.title, args.body, args.topic,
                                     _split(args.points), _split(args.tags),
                                     _split(args.entities), _split(args.source_ids),
                                     args.confidence)
            print(f"{'已合并进' if how == 'merged' else '已写入'}：{rec['id']}（v{rec['version']}）")
            print(f"  [{rec['topic']}] {rec['title']}")
            return 0
        if args.act == "list":
            rows = list_knowledge(args.topic, args.limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif not rows:
                print("知识库还是空的。用 knowledge add 写第一条。")
            else:
                print(f"知识库 {len(rows)} 条\n")
                for r in rows:
                    _print_knowledge(r)
            return 0
        if args.act == "search":
            rows = search_knowledge(" ".join(args.query), args.k)
            print(f"检索「{' '.join(args.query)}」→ {len(rows)} 条\n")
            for r, s in rows:
                _print_knowledge(r, s)
            return 0
        if args.act == "show":
            rec = next((r for r in load_knowledge() if r["id"] == args.id), None)
            if not rec:
                print("没有这条知识。", file=sys.stderr)
                return 1
            _print_knowledge(rec)
            return 0
        if args.act == "tidy":
            if args.apply:
                path = Path(args.apply)
                if not path.exists():
                    print(f"找不到整理结果：{path}", file=sys.stderr)
                    return 2
                try:
                    result = brain.load_loose_json(path, "knowledge tidy --apply 的结果")
                except ValueError as e:
                    print(str(e), file=sys.stderr)
                    return 2
                res = apply_tidy(result)
                print(f"知识整理完成：合并 {res['merged']} 组，更新 {res['updated']} 条，"
                      f"新建 {res['created']} 条，删除 {res['deleted']} 条")
                print("（AI 动过的条目已标为待审核：knowledge review 查看 → confirm/discard）")
                return 0
            rep = tidy_knowledge()
            if args.json:
                print(json.dumps(rep, ensure_ascii=False, indent=2))
                return 0
            if not rep.get("task_id"):
                print(rep.get("message", "知识库还是空的"))
                return 0
            print(f"知识库 {rep['stats']['total']} 条；相似簇 {len(rep['clusters'])}，"
                  f"无标签 {len(rep['untagged'])}，无主题 {len(rep['no_topic'])}，过短 {len(rep['short'])}")
            print(f"待整理任务：{KNOWLEDGE_DIR / 'pending.json'}")
            for i, c in enumerate(rep["clusters"], 1):
                print(f"[{i}] {' / '.join(c['titles'][:4])}")
            print("\n把 pending.json 里的 apply_schema 填好，然后：")
            print("  python scripts/brain.py knowledge tidy --apply apply.json")
            return 0
        if args.act in ("pin", "rm"):
            if args.act == "rm":
                if delete_knowledge(args.id):
                    print("已删除。")
                    return 0
                print("没找到这条知识。", file=sys.stderr)
                return 1
            rec = next((r for r in load_knowledge() if r["id"] == args.id), None)
            if not rec:
                print("没找到这条知识。", file=sys.stderr)
                return 1
            rec = update_knowledge(args.id, pinned=not rec.get("pinned", False))
            print("已置顶。" if rec["pinned"] else "已取消置顶。")
            return 0
        if args.act == "review":
            rows = knowledge_pending()
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif not rows:
                print("没有待审核的知识。AI 整理（tidy）产出的条目会自动标为待审核。")
            else:
                print(f"待审核 {len(rows)} 条（AI 整理产出，确认无误后再 knowledge confirm <id>）\n")
                for r in rows:
                    _print_knowledge(r)
            return 0
        if args.act in ("confirm", "discard"):
            st = "confirmed" if args.act == "confirm" else "discarded"
            rec = set_knowledge_status(args.id, st)
            if not rec:
                print("没找到这条知识。", file=sys.stderr)
                return 1
            print(("已确认。" if st == "confirmed" else "已废弃（不再显示）。") + f" {rec['title']}")
            return 0

    # ---------------- note
    if args.cmd == "note":
        if args.act == "write":
            rec, how = write_note(args.type, args.title, args.body, args.mood,
                                  _split(args.tags), _split(args.memory_ids),
                                  append_same_day=not args.new)
            print(f"{'已追加到' if how == 'appended' else '已写入'}：{rec['id']}  {rec['date']} [{rec['type']}] {rec['title']}")
            return 0
        if args.act == "list":
            rows = list_notes(args.type, args.days, args.limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif not rows:
                print("还没有笔记。用 note write 写第一篇。")
            else:
                for r in rows:
                    _print_note(r)
            return 0
        if args.act == "search":
            rows = search_notes(" ".join(args.query), args.k)
            print(f"检索「{' '.join(args.query)}」→ {len(rows)} 条\n")
            for r, s in rows:
                print(f"[{r.get('date')}] {r.get('title')} · 相关 {s:.2f}")
                print("  " + _clip(r.get("body", ""), 200) + "\n")
            return 0
        if args.act == "show":
            rec = next((r for r in load_notes() if r["id"] == args.id), None)
            if not rec:
                print("没有这条笔记。", file=sys.stderr)
                return 1
            _print_note(rec)
            return 0
        if args.act == "digest":
            print(digest(args.days))
            return 0

    # ---------------- skill
    if args.cmd == "skill":
        if args.act in ("new", "edit"):
            body = args.body
            if getattr(args, "file", None):
                body = Path(args.file).read_text(encoding="utf-8")
            if args.act == "new":
                if not body:
                    print("需要 --body 或 --file 提供正文。", file=sys.stderr)
                    return 2
                res = create_skill(args.name, args.description, body, args.force)
                print(f"技能已创建：{res['path']}（v{res['version']}）")
                print("自检：python scripts/brain.py skill check " + res["name"])
                print("安装：python scripts/brain.py skill install " + res["name"])
            else:
                res = edit_skill(args.name, body, args.append, args.description)
                print(f"技能已更新：{res['path']}（v{res['version']}）")
            return 0
        if args.act == "list":
            if getattr(args, "installed_list", False):
                rows = list_installed()
                if args.json:
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
                elif not rows:
                    print("全局技能目录里还没有安装任何技能：" + "、".join(str(r) for r in _global_roots()))
                else:
                    print("全局技能目录：" + "、".join(str(r) for r in _global_roots()) + "\n")
                    for r in rows:
                        mark = "✓" if r["ok"] else "✗ 目录名/name 不是 kebab-case，DSH 会跳过"
                        print(f"{r['dir']}  name={r['name']}  v{r['version']}  {mark}")
                        print(f"  {r['path']}\n")
                    if any(not r["ok"] for r in rows):
                        print("带 ✗ 的目录：skill uninstall <名字> 会连旧中文目录一起清掉，再 install 即可。")
                return 0
            rows = list_skills()
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif not rows:
                print("还没有自己写过技能。用 skill new 创建第一个。")
            else:
                for s in rows:
                    state = "已安装" if s["installed"] else "未安装"
                    print(f"{s['name']}  v{s['version']}  {s['lines']} 行  [{state}]")
                    print(f"  {s['description']}")
                    print(f"  {s['path']}\n")
            return 0
        if args.act == "show":
            sk = read_skill(args.name)
            print(sk["raw"])
            return 0
        if args.act == "check":
            results = check_skill(args.name)
            bad = [r for r in results if not r["ok"]]
            for r in results:
                print(("✓ " if r["ok"] else "✗ ") + r["name"] + (f"（{r.get('lines')} 行）" if r["ok"] else ""))
                for i in r["issues"]:
                    print(f"    - {i}")
            print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
            return 0 if not bad else 1
        if args.act == "install":
            res = install_skill(args.name, args.force, getattr(args, "from_dir", None),
                                accept_risk=bool(getattr(args, "yes", False)))
            if res["state"] == "risk_required":
                print("「灵枢」会自动读写记忆、自动整理、能自己改说明书——安装前请先了解并接受风险：\n")
                for i, line in enumerate(res["risks"], 1):
                    print(f"  {i}. {line}")
                print("\n确认接受后果后，重跑加入 --yes：")
                print(f"  python scripts/brain.py skill install {args.name} --from \"{getattr(args, 'from_dir', '')}\" --yes")
                print("（一旦接受会记入 config，以后不再反复问，除非你手动重置）")
                return 1
            msg = {"installed": "已安装到全局技能目录（含脚本，已生成 AGENTS.md）",
                   "already": "已是最新，无需重复安装"}[res["state"]]
            print(f"{msg}：{res['path']}")
            if res.get("agents"):
                print(f"  AGENTS.md：{res['agents']}")
            if res.get("legacy"):
                print(f"  [提示] 发现旧中文名目录：{res['legacy']}")
                print("         DSH 只认 kebab-case，该目录会被跳过。清理："
                      f"python scripts/brain.py skill uninstall {res['name']}")
            for _s in res.get("stale") or []:
                print(f"  [提示] 另一个全局目录里还有同名副本：{_s}")
                print("         它不会自动跟着更新，若那边也要用请单独 install --force。")
            if re.search(r"[\u4e00-\u9fff]", (args.name or "").strip()):
                print("  [提示] 名字含中文：DSH 的技能发现只认 kebab-case（英文短横线），"
                      "这个技能装进全局后可能不显示，建议用 skill edit 改成英文名。")
            return 0
        if args.act == "uninstall":
            res = uninstall_skill(args.name)
            if res["state"] == "uninstalled":
                print("已卸载：" + "、".join(res.get("removed") or [res["name"]]))
            else:
                print("全局目录里没有这个技能。")
            return 0
        if args.act == "rm":
            if remove_skill(args.name):
                print("已删除自己写的技能。")
                return 0
            print("没找到这个技能。", file=sys.stderr)
            return 1
        if args.act == "find":
            q = " ".join(args.query)
            rows = search_skills(q, args.limit)
            dsh = search_dsh_plugins(q, 6)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif not rows and not dsh:
                print(f"没找到能用的技能：{q or '（空）'}。要么自己写一个：skill new")
            else:
                if rows:
                    print(f"找到 {len(rows)} 个可用技能\n")
                    for r in rows:
                        mark = " ← 你自己" if r["self"] else ""
                        print(f"[{r['source']}] {r['name']}  (匹配 {r['score']}){mark}")
                        print(f"  {_clip(r['description'], 120)}")
                        print(f"  {r['path']}\n")
                    print("要直接照做：skill use <名字>；要变成自己的：skill borrow <名字>")
                else:
                    print(f"灵枢技能里没找到「{q or '（空）'}」，但 DSH（DeepSeek）插件里有相关的：\n")
                if dsh:
                    print("\nDSH（DeepSeek）插件（只读，不是灵枢技能）：")
                    for p in dsh:
                        print(f"  [{p['name']}] v{p['version']}  {_clip(p['description'], 80)}")
                    print("  查看全部：skill plugins")
            return 0
        if args.act == "plugins":
            rows = list_dsh_plugins()
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif not rows:
                print("没找到 DSH profile（~/.dsh/profiles）或其 bundle 插件。")
            else:
                print(f"DSH 插件 {len(rows)} 个（只读，来自 ~/.dsh/profiles）\n")
                cur = None
                for p in rows:
                    if p["profile"] != cur:
                        cur = p["profile"]
                        print(f"── profile：{cur}")
                    agents = "· 含 AGENTS.md" if p["agents"] else ""
                    print(f"[{p['name']}] v{p['version']}  {agents}")
                    print(f"  {_clip(p['description'], 120)}")
                    print(f"  {p['path']}\n")
                print("这些是 DSH 运行时插件，不是灵枢技能；查看不修改。")
            return 0
        if args.act == "self-review":
            r = self_review(force=bool(getattr(args, "force", False)),
                            days=int(getattr(args, "days", 3) or 3))
            if not r["due"]:
                print(f"还没到总结时间（每 {r['days']} 天一次，上一次才过去 {_fmt_ago(int(brain.now_ts() - r['last']))}）。")
                print("想现在总结就加 --force。")
                return 0
            print(r["summary"])
            if r.get("saved"):
                print("\n（last_self_review 已刷新，下次提醒在 {0} 天后）".format(r["days"]))
            else:
                print("\n（只读：本次总结时间未记录，下次仍会提醒）")
            return 0
        if args.act == "use":
            sk = use_skill(args.name, args.chars)
            print(f"# 技能：{sk['name']}（{sk['source']}）")
            print(f"# 路径：{sk['path']}")
            print(f"# 用途：{sk['description']}\n")
            print(sk["body"])
            if sk["truncated"]:
                print(f"\n…（原文更长，完整内容见 {sk['path']}）")
            if args.auto:
                try:
                    res = install_skill(args.name, force=False)
                except FileExistsError:
                    # 全局已有同名但内容不同 → 覆盖，保证全局目录始终是最新版
                    res = install_skill(args.name, force=True)
                    res["state"] = "updated"
                if res.get("state") == "risk_required":
                    print("\n「灵枢」会自动读写记忆、自动整理、能自己改说明书。自动全局化前请先接受风险：\n")
                    for i, line in enumerate(res["risks"], 1):
                        print(f"  {i}. {line}")
                    print("\n用户接受后，执行一次（只做一次，之后不再问）：")
                    print('  python scripts/brain.py skill install lingshu --from "灵枢技能目录" --yes')
                    return 0
                state = res.get("state", "installed")
                print(f"\n已自动全局化：{res['path']}（{state}）")
                try:
                    log_evolution("skill", f"使用即全局化：{args.name}",
                                  f"skill use --auto：{state}，保证新会话可调",
                                  [res["path"]])
                except Exception:
                    pass
            return 0
        if args.act == "borrow":
            res = borrow_skill(args.name, args.as_name, args.note, args.force)
            print(f"已借鉴成自己的技能：{res['path']}（v{res['version']}）")
            print("接下来按你自己的情况改写，然后 skill check / skill install。")
            return 0

    # ---------------- ext（扩展模块：AI 自己写的可执行钩子）
    if args.cmd == "ext":
        if args.act == "list":
            rows = list_extensions()
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif not rows:
                print("还没有扩展模块。用 `ext new <名字>` 新建一个（会给你能跑的模板）。")
            else:
                print(f"扩展模块 {len(rows)} 个（~/.brain-memory/extensions/，更新技能永不覆盖）：")
                for r in rows:
                    print(f"  · {r['name']}  {r['desc']}")
                print("\n看源码 `ext show <名字>`；跑一下 `ext run <名字> [参数]`；自检 `ext check <名字>`")
            return 0
        if args.act == "show":
            path = _ext_path(args.name)
            if not path.exists():
                print(f"没有这个扩展模块：{args.name}", file=sys.stderr)
                return 1
            print(path.read_text(encoding="utf-8"))
            return 0
        if args.act in ("new", "edit", "rm"):
            if _readonly():
                print("[只读] 记忆库当前不可写，扩展模块需要写盘。", file=sys.stderr)
                return 3
            if args.act == "new":
                body = args.body
                if getattr(args, "file", None):
                    body = Path(args.file).read_text(encoding="utf-8")
                res = create_extension(args.name, args.desc, body, overwrite=args.force)
                print(f"已新建扩展模块：{res['path']}")
                print("钩子：inject(ctx) 并入每会话注入包；run(argv) 用 `ext run <名字>` 调用。"
                      "改完 `ext check <名字>` 自检。")
                return 0
            if args.act == "edit":
                body = args.body
                if getattr(args, "file", None):
                    body = Path(args.file).read_text(encoding="utf-8")
                res = edit_extension(args.name, body, args.append)
                print(f"已{'追加' if res['appended'] else '更新'}扩展模块：{res['path']}")
                return 0
            if remove_extension(args.name):
                print(f"已删除扩展模块：{args.name}")
                return 0
            print(f"没有这个扩展模块：{args.name}", file=sys.stderr)
            return 1
        if args.act == "check":
            r = check_extension(args.name)
            if r["ok"]:
                print(f"语法 OK：{r['path']}")
                return 0
            print(f"语法有问题：{r['path']}\n{r['error']}", file=sys.stderr)
            return 2
        if args.act == "run":
            mod = _load_ext(args.name)
            fn = getattr(mod, "run", None)
            if not callable(fn):
                print(f"扩展 {args.name} 没有 run(argv) 钩子（它可能只提供了 inject()）。", file=sys.stderr)
                return 2
            rc = fn(list(args.args))
            return int(rc) if isinstance(rc, int) else 0

    # ---------------- learn
    if args.cmd == "learn":
        if args.act == "loop":
            for line in LEARN_LOOP:
                print(line)
            return 0
        q = " ".join(args.question)
        res = learn_ask(q, args.k)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        print(f"问题：{q}\n")
        if res["knowledge"]:
            print(f"【知识库】命中 {len(res['knowledge'])} 条 —— 直接用，别再搜一遍")
            for k in res["knowledge"]:
                print(f"  {k['id']} [{k['topic']}] {k['title']}  (相关 {k['score']})")
                print(f"    {_clip(k['body'], 200)}")
        else:
            print("【知识库】没有现成答案 —— 去联网搜索/读文档，拿到可验证的结论")
        if res["notes"]:
            print(f"\n【笔记】相关 {len(res['notes'])} 条（看看上次是怎么卡住的）")
            for n in res["notes"]:
                print(f"  {n['id']} [{n['type']}] {n['title']}  (相关 {n['score']})")
        if res["memories"]:
            print(f"\n【记忆】相关 {len(res['memories'])} 条")
            for m in res["memories"]:
                print(f"  {m['id']}  {m['text']}")
        print("\n收尾（按需，不要全做）：")
        if not res["has_answer"]:
            print('  查到的结论写回知识库：knowledge add "标题" --body "结论" --tags "..."')
        print('  卡点/过程写进笔记：note write "这次卡在哪" --type log --body "..."')
        print('  摸出了可复用的做法：skill new <名字> --description "..." --body "..."')
        print('  做完记一笔：evolve log "学到了什么" --kind knowledge')
        return 0

    # ---------------- growth
    if args.cmd == "growth":
        if args.act == "status":
            st = growth_status()
            if args.json:
                print(json.dumps(st, ensure_ascii=False, indent=2))
            else:
                print("成长状态")
                print(f"  活跃 {st['days_alive']} 天，连续 {st['streak']} 天")
                print(f"  记忆 {st['memories']} 条 · 知识 {st['knowledge']} 条（{len(st['topics'])} 个主题）")
                print(f"  笔记 {st['notes']} 篇（{st['note_days']} 天）· 技能 {st['skills']} 个（已安装 {st['installed']}）")
                print(f"  自我进化 {st['evolutions']} 次")
                if st.get("last_evolution"):
                    print(f"  最近一次：{st['last_evolution'].get('title')}")
                if st["last_growth"]:
                    print(f"  上次复盘：{st['last_growth']['date']} {st['last_growth']['title']}")
                else:
                    print("  还没有做过成长复盘")
                if st["pending_tidy"]:
                    print("  知识库有待整理的条目")
            return 0
        if args.act == "review":
            print(growth_review(args.days)["material"])
            return 0

    # ---------------- evolve
    if args.cmd == "evolve":
        if args.act == "log":
            row = log_evolution(args.kind, args.title, args.detail, _split(args.refs))
            print(f"已记录进化：{row['id']}  [{row['kind']}] {row['title']}")
            return 0
        rows = load_evolution(args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("还没有进化记录。真正改掉什么之后，用 evolve log 写一条。")
            return 0
        for r in rows:
            print(f"{r['date']}  [{r['kind']}]  {r['title']}")
            if r.get("detail"):
                print("  " + r["detail"])
            if r.get("refs"):
                print("  refs: " + "、".join(r["refs"]))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(run())

