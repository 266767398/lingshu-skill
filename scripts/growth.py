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
import re
import shutil
import sys
import time
from pathlib import Path

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
GLOBAL_SKILLS = Path.home() / ".trae-cn" / "skills"

NOTE_KINDS = ("journal", "log", "growth")
# 技能名：小写字母/数字/连字符/下划线，也允许中文（中文生态里技能就叫「质检评审」这类名字）
SLUG_RE = re.compile(r"^[a-z0-9\u4e00-\u9fff][a-z0-9-_\u4e00-\u9fff]{0,62}$")


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
    return brain.read_jsonl(KNOWLEDGE_FILE)


def save_knowledge(rows: list) -> None:
    brain.write_jsonl(KNOWLEDGE_FILE, rows)
    rebuild_index(rows)


def _kdoc(rec: dict) -> set:
    return brain.terms(" ".join([
        rec.get("title", "") * 2, rec.get("body", ""),
        " ".join(rec.get("points") or []), " ".join(rec.get("tags") or []),
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

    rows = load_knowledge()
    new_title_terms = brain.terms(title)
    for old in rows:
        # 只在标题几乎相同（或包含关系极强）时合并，避免把同主题的不同结论揉成一条
        if brain.jaccard(new_title_terms, brain.terms(old.get("title", ""))) >= 0.75:
            if body and body not in old.get("body", ""):
                old["body"] = (old.get("body", "") + "\n\n" + body).strip()
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
                   source_ids=None, confidence=0.8) -> dict:
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
        "pinned": False,
        "version": 1,
        "created": now,
        "updated": now,
        "history": [{"ts": now, "why": "创建", "version": 1}],
    }


def list_knowledge(topic: str | None = None, limit: int = 50) -> list:
    rows = load_knowledge()
    if topic:
        rows = [r for r in rows if r.get("topic") == topic]
    return sorted(rows, key=lambda r: (r.get("pinned", False), r.get("updated", 0)), reverse=True)[:limit]


def search_knowledge(query: str, k: int = 8) -> list:
    rows = load_knowledge()
    if not rows or not (query or "").strip():
        return []
    q = brain.terms(query)
    docs = [_kdoc(r) for r in rows]
    idf = brain.build_idf(docs)
    out = []
    for rec, doc in zip(rows, docs):
        score = brain.relevance(q, doc, idf)
        if rec.get("pinned") and score > 0:
            score = min(1.0, score + 0.05)
        if score > 0.02:
            out.append((rec, score))
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:k]


def update_knowledge(kid: str, **fields) -> dict:
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
    try:
        brain.atomic_json(INDEX_FILE, idx)
    except OSError:
        pass  # 索引只是缓存，写不了也不影响读写知识本体
    return idx


def knowledge_stats() -> dict:
    rows = load_knowledge()
    idx = rebuild_index(rows)
    return {
        "total": len(rows),
        "topics": idx["topics"],
        "tags": list(idx["tags"].items())[:8],
        "words": sum(len(r.get("body", "")) for r in rows),
        "pinned": sum(1 for r in rows if r.get("pinned")),
    }


def tidy_knowledge() -> dict:
    """机械整理：找相似簇、无主题、无标签、过短条目，产出 pending.json 交给 LLM 压缩。"""
    rows = load_knowledge()
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
                "titles": [m["title"] for m in members],
                "hint": "这些条目在讲同一件事，建议合并成一条更准确的知识。",
                "texts": [{"id": m["id"], "title": m["title"], "body": _clip(m.get("body", ""), 160),
                           "points": m.get("points", []), "tags": m.get("tags", [])} for m in members],
            })

    report = {
        "task_id": _uid("tidy_", "knowledge"),
        "at": brain.now_ts(),
        "stats": knowledge_stats(),
        "clusters": clusters,
        "untagged": [{"id": r["id"], "title": r["title"]} for r in rows if not r.get("tags")],
        "no_topic": [{"id": r["id"], "title": r["title"]} for r in rows
                     if r.get("topic") in ("", "未分类", None)],
        "short": [{"id": r["id"], "title": r["title"], "len": len(r.get("body", ""))}
                  for r in rows if len(r.get("body", "")) < 40],
        "apply_schema": {
            "task_id": "tidy_...",
            "merge": [{"ids": ["k_..."], "title": "", "topic": "", "body": "",
                       "points": [], "tags": [], "entities": []}],
            "update": [{"id": "k_...", "topic": "", "tags": [], "body": ""}],
            "create": [{"title": "", "topic": "", "body": "", "points": [], "tags": [], "entities": []}],
            "delete": ["k_..."],
            "note": "为什么这样整理，一句话",
        },
    }
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    brain.atomic_json(KNOWLEDGE_DIR / "pending.json", report)
    return report


def apply_tidy(result: dict) -> dict:
    rows = load_knowledge()
    by_id = {r["id"]: r for r in rows}
    done = {"merged": 0, "updated": 0, "created": 0, "deleted": 0}

    removed = set()
    for op in result.get("merge") or []:
        ids = [i for i in (op.get("ids") or []) if i in by_id]
        if not ids:
            continue
        base = by_id[ids[0]]
        for other_id in ids[1:]:
            other = by_id[other_id]
            if other.get("body") and other["body"] not in base.get("body", ""):
                base["body"] = (base.get("body", "") + "\n\n" + other["body"]).strip()
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
        rec.setdefault("history", []).append(
            {"ts": rec["updated"], "why": "知识整理：更新", "version": rec["version"]})
        done["updated"] += 1

    for op in result.get("create") or []:
        if not (op.get("title") or op.get("body")):
            continue
        title = (op.get("title") or "").strip() or _clip(op.get("body", ""), 24)
        rec = _new_knowledge(title, op.get("body", ""), op.get("topic", ""),
                             op.get("points"), op.get("tags"), op.get("entities"))
        rows.append(rec)
        by_id[rec["id"]] = rec
        done["created"] += 1

    explicit = set()
    for kid in result.get("delete") or []:
        if kid in by_id:
            removed.add(kid)
            explicit.add(kid)

    rows = [r for r in rows if r["id"] not in removed]
    save_knowledge(rows)
    done["deleted"] = len(explicit)

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
    return brain.read_jsonl(NOTES_FILE)


def save_notes(rows: list) -> None:
    brain.write_jsonl(NOTES_FILE, rows)


def write_note(kind: str, title: str, body: str, mood: str = "", tags=None,
               memory_ids=None, append_same_day: bool = True) -> tuple:
    kind = kind if kind in NOTE_KINDS else "journal"
    body = (body or "").strip()
    title = (title or "").strip() or _clip(body, 20) or "无题"
    rows = load_notes()
    today = _today()

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
    if days:
        floor = brain.now_ts() - days * brain.DAY
        rows = [r for r in rows if r.get("created", 0) >= floor]
    return sorted(rows, key=lambda r: r.get("created", 0), reverse=True)[:limit]


def search_notes(query: str, k: int = 10) -> list:
    rows = load_notes()
    if not rows or not (query or "").strip():
        return []
    q = brain.terms(query)
    docs = [brain.terms(" ".join([r.get("title", "") * 2, r.get("body", ""),
                                  " ".join(r.get("tags") or [])])) for r in rows]
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
        "words": sum(len(r.get("body", "")) for r in rows),
    }


def digest(days: int = 7) -> str:
    """给 AI 的成长素材包：最近发生了什么、学到什么、写了什么。"""
    now = brain.now_ts()
    floor = now - days * brain.DAY
    mems = [m for m in brain.load_all() if m.get("created", 0) >= floor]
    kds = [k for k in load_knowledge() if k.get("updated", 0) >= floor]
    nts = [n for n in load_notes() if n.get("created", 0) >= floor]
    sks = list_skills()
    evo = [r for r in load_evolution() if r.get("ts", 0) >= floor]

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
        for n in nts[:8]:
            lines.append(f"- {n.get('date')} [{n.get('type')}] {n.get('title')}：{_clip(n.get('body', ''), 80)}")
    if sks:
        lines += ["", "## 我写过的技能"]
        for s in sks:
            lines.append(f"- {s['name']}（v{s.get('version', 1)}）{s.get('description', '')[:60]}")
    if evo:
        lines += ["", "## 最近的自我进化"]
        for e in evo[:8]:
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
    brain.atomic_json(SKILL_REG, reg)


def _require_slug(name: str) -> str:
    name = (name or "").strip()
    if not SLUG_RE.match(name):
        raise ValueError("技能名不合法：只能用字母/数字/连字符/下划线/中文，2-63 个字符")
    return name


def _skill_path(name: str) -> Path:
    return SKILLS_DIR / _require_slug(name) / "SKILL.md"


def read_skill(name: str) -> dict:
    path = _skill_path(name)
    if not path.exists():
        raise KeyError(f"没有这个技能：{name}")
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
    name = (name or "").strip().lower().replace(" ", "-").replace("_", "-")
    if not SLUG_RE.match(name):
        raise ValueError("技能名只能用字母/数字/连字符/下划线/中文，2-63 个字符，例如 note-helper")
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
            installed_dir = GLOBAL_SKILLS / name
            out.append({
                "name": name,
                "description": meta.get("description", ""),
                "version": int(meta.get("version", reg.get(name, {}).get("version", 1)) or 1),
                "lines": len(body.splitlines()),
                "updated": p.stat().st_mtime,
                "installed": (installed_dir / "SKILL.md").exists(),
                "agents": (installed_dir / "AGENTS.md").exists(),
                "path": str(p),
            })
    return sorted(out, key=lambda x: x["updated"], reverse=True)


def _agents_md(name: str, description: str, meta: dict) -> str:
    """给全局技能生成 AGENTS.md：DSH/DeepSeek 生态的常驻入口。
    只写「何时用 / 怎么调 / 在哪」，不复制 SKILL.md 正文。"""
    entry = ""
    src_dir = SKILLS_DIR / name
    if (src_dir / "scripts").is_dir():
        scripts = sorted(p.name for p in (src_dir / "scripts").glob("*.py"))
        if scripts:
            entry = "、".join(f"`python scripts/{s}`" for s in scripts[:4])
    elif any(src_dir.glob("*.py")):
        entry = "、".join(f"`python {p.name}`" for p in sorted(src_dir.glob("*.py"))[:4])
    lines = [
        f"# {name}",
        "",
        f"<!-- 常驻层：由灵枢在全局化时生成。规则 1-3 行、自包含。正文见同目录 SKILL.md。 -->",
        "",
        f"**何时用**：{description}",
        "",
        "**怎么用**：先读同目录 `SKILL.md`，按其中的步骤执行；有 `scripts/` 就直接调用。",
    ]
    if entry:
        lines.append(f"**入口**：{entry}")
    lines += ["", f"**来源**：{meta.get('origin', '灵枢')}　**版本**：{meta.get('version', 1)}"]
    return "\n".join(lines) + "\n"


def install_skill(name: str, force: bool = False, from_dir: str | None = None) -> dict:
    """把技能整目录装进全局技能目录（不只是 SKILL.md）。

    带 scripts/assets 的技能必须整目录复制，否则全局化后只剩说明书、跑不起来。
    同时生成 AGENTS.md，供 DSH/DeepSeek 等以 AGENTS.md 为常驻约定的宿主读取。
    from_dir 允许从外部目录安装（例如把灵枢本体所在目录安装成全局技能）。
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
            raise KeyError(f"没有这个技能：{name}")
    dst_dir = GLOBAL_SKILLS / name
    dst = dst_dir / "SKILL.md"

    meta, _ = parse_frontmatter(src.read_text(encoding="utf-8"))
    if dst.exists() and not force:
        if dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"):
            return {"name": name, "state": "already", "path": str(dst)}
        raise FileExistsError(f"全局技能目录已有同名技能 {name}；确认覆盖请加 --force")

    dst_dir.mkdir(parents=True, exist_ok=True)
    # 整目录复制（跳过缓存与临时文件），保留 scripts/ assets/ 等运行时资源
    for item in src_dir.rglob("*"):
        if item.is_dir() or "__pycache__" in item.parts:
            continue
        if item.name.endswith((".tmp", ".pyc")):
            continue
        rel = item.relative_to(src_dir)
        target = dst_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
    # AGENTS.md：DSH/DeepSeek 常驻入口（不覆盖用户已有的同名文件，除非 force）
    agents = dst_dir / "AGENTS.md"
    if force or not agents.exists():
        agents.write_text(_agents_md(name, meta.get("description", ""), meta), encoding="utf-8")

    reg = load_registry()
    reg.setdefault(name, {})["installed_at"] = brain.now_ts()
    save_registry(reg)
    return {"name": name, "state": "installed", "path": str(dst), "agents": str(agents)}


def uninstall_skill(name: str) -> dict:
    name = _require_slug(name)
    dst_dir = GLOBAL_SKILLS / name
    if not dst_dir.exists():
        return {"name": name, "state": "absent"}
    shutil.rmtree(dst_dir)
    reg = load_registry()
    if name in reg:
        reg[name]["installed_at"] = None
        save_registry(reg)
    return {"name": name, "state": "uninstalled"}


def remove_skill(name: str) -> bool:
    name = _require_slug(name)
    d = SKILLS_DIR / name
    if not d.exists():
        return False
    shutil.rmtree(d)
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
    return [("自己", SKILLS_DIR), ("全局", GLOBAL_SKILLS)]


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
                "self": name == "灵枢",
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
    target = (as_name or name).strip()
    if not SLUG_RE.match(target):
        target = "borrowed-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    desc = f"借鉴自 {name}：{sk['meta'].get('description', '')}".strip()
    body = sk["body"].rstrip()
    body += f"\n\n## 出处\n借鉴自技能 `{name}`（{sk['path']}）。"
    if note.strip():
        body += f"\n\n## 我为什么借它\n{note.strip()}"
    res = create_skill(target, desc, body, overwrite=force, origin=f"borrowed:{name}")
    log_evolution("skill", f"借鉴技能 {name}", f"生成了自己的版本 {target}；{note.strip() or '按原步骤改写'}",
                  [sk["path"], res["path"]])
    return res


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
    last_growth = next((n for n in load_notes() if n.get("type") == "growth"), None)
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
    a = ss.add_parser("show", help="查看技能")
    a.add_argument("name")
    a = ss.add_parser("check", help="自检技能规范")
    a.add_argument("name", nargs="?", default=None)
    a = ss.add_parser("install", help="安装到全局技能目录（整目录复制，含脚本，生成 AGENTS.md）")
    a.add_argument("name")
    a.add_argument("--force", action="store_true")
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
    a = ss.add_parser("use", help="读取一个技能，直接照着做")
    a.add_argument("name")
    a.add_argument("--chars", type=int, default=6000)
    a.add_argument("--auto", action="store_true", help="未全局化时自动装到全局技能目录")
    a = ss.add_parser("borrow", help="借鉴一个技能，变成自己的版本")
    a.add_argument("name")
    a.add_argument("--as", dest="as_name", default="")
    a.add_argument("--note", default="")
    a.add_argument("--force", action="store_true")

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
                res = apply_tidy(json.loads(path.read_text(encoding="utf-8-sig")))
                print(f"知识整理完成：合并 {res['merged']} 组，更新 {res['updated']} 条，"
                      f"新建 {res['created']} 条，删除 {res['deleted']} 条")
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
                print("已删除。" if delete_knowledge(args.id) else "没找到这条知识。")
                return 0
            rec = next((r for r in load_knowledge() if r["id"] == args.id), None)
            if not rec:
                print("没找到这条知识。", file=sys.stderr)
                return 1
            rec = update_knowledge(args.id, pinned=not rec.get("pinned", False))
            print("已置顶。" if rec["pinned"] else "已取消置顶。")
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
            res = install_skill(args.name, args.force, getattr(args, "from_dir", None))
            msg = {"installed": "已安装到全局技能目录（含脚本，已生成 AGENTS.md）",
                   "already": "已是最新，无需重复安装"}[res["state"]]
            print(f"{msg}：{res['path']}")
            if res.get("agents"):
                print(f"  AGENTS.md：{res['agents']}")
            return 0
        if args.act == "uninstall":
            res = uninstall_skill(args.name)
            print("已卸载。" if res["state"] == "uninstalled" else "全局目录里没有这个技能。")
            return 0
        if args.act == "rm":
            print("已删除自己写的技能。" if remove_skill(args.name) else "没找到这个技能。")
            return 0
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
                    install_skill(args.name, force=False)
                    res_state = "installed"
                except FileExistsError:
                    # 内容有更新 → 覆盖，保证全局目录始终是最新版
                    install_skill(args.name, force=True)
                    res_state = "updated"
                print(f"\n已自动全局化：{GLOBAL_SKILLS / args.name / 'SKILL.md'}（{res_state}）")
                try:
                    log_evolution("skill", f"使用即全局化：{args.name}",
                                  f"skill use --auto：{res_state}，保证新会话可调",
                                  [str(GLOBAL_SKILLS / args.name / "SKILL.md")])
                except Exception:
                    pass
            return 0
        if args.act == "borrow":
            res = borrow_skill(args.name, args.as_name, args.note, args.force)
            print(f"已借鉴成自己的技能：{res['path']}（v{res['version']}）")
            print("接下来按你自己的情况改写，然后 skill check / skill install。")
            return 0

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

