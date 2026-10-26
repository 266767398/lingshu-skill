#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 类脑记忆引擎（纯标准库，零依赖）

分层结构（对照人脑）:
  工作记忆 = 当前上下文（AI 自己维护，默认不进磁盘；work add 只是会话级暂存）
  短期记忆 = hippocampus.jsonl  海马体，快速编码、易衰减
  长期记忆 = cortex.jsonl       新皮层，经"睡眠巩固"后稳定存储
  核心记忆 = persona.json       人格/身份，常驻注入
  淡忘区   = dust.jsonl         已遗忘但保留轮廓的旧记忆

关键机制:
  * 遗忘曲线   R(t) = exp(-Δt / S)，每次回忆会提升稳定度 S（测试效应/再巩固）
  * 模糊回忆   IDF 加权字符二元组相似度 + 时间衰减 + 重要性 + 联想扩散
  * 做梦巩固   上下文将满/定时触发：重放 → 压缩 → 巩固 → 情绪整合 → 遗忘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 基础环境

HOME = Path(os.environ.get("BRAIN_MEMORY_HOME") or (Path.home() / ".brain-memory"))

# 只读降级：能读不能写（沙箱写盘被拒、或迁移前临时挂载已有库时用）。
# 触发方式：环境变量 BRAIN_MEMORY_READONLY=1、命令行 --ro，或运行前自动探测到 HOME 不可写。
READONLY = os.environ.get("BRAIN_MEMORY_READONLY") == "1"

DAY = 86400
HOUR = 3600
DEFAULT_STABILITY = 2 * DAY
MAX_STABILITY = 240 * DAY
RETENTION_FLOOR = 0.10

KINDS = ("episode", "fact", "preference", "emotion", "insight", "identity", "habit")
LAYERS = ("hippo", "cortex", "dust")

# 工作记忆：当前上下文本身就是工作记忆，默认不落盘。
# 只有 AI 自己决定留一手时才写 working.jsonl（会话级暂存，容量 7±2，过期即弃）。
WORKING_FILE = HOME / "working.jsonl"
WORKING_CAPACITY = 9
WORKING_TTL = 12 * HOUR

DEFAULT_CONFIG = {
    "budget_tokens": 1400,
    # 上下文水位三级防线（比例，0-1）：
    #   review  清点：先补记本轮漏掉的，不急着压缩
    #   dream   做梦：重放→压缩→巩固
    #   escape  逃生：上下文快满，必须原样封存，保证不丢
    "review_threshold": 0.60,
    "dream_threshold": 0.75,
    "escape_threshold": 0.90,
    "weights": {"relevance": 0.55, "recency": 0.25, "importance": 0.20},
    "panel_port": 8765,
}

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------- 工具函数

def now_ts() -> int:
    return int(time.time())


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def ensure_home() -> None:
    if READONLY:
        return  # 只读降级：不建目录、不补默认文件，已有文件照样可读
    HOME.mkdir(parents=True, exist_ok=True)
    if not (HOME / "config.json").exists():
        atomic_json(HOME / "config.json", DEFAULT_CONFIG)
    if not (HOME / "persona.json").exists():
        compile_persona()
    (HOME / "dreams").mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    path = HOME / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    cfg["weights"] = dict(DEFAULT_CONFIG["weights"], **(cfg.get("weights") or {}))
    return cfg


def atomic_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out, broken = [], 0
    raw = path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            broken += 1
            continue
    if broken:
        # 有损坏行：先把好的行写回，坏行单独挪到 .corrupt 备份，
        # 绝不静默当作没发生、也不让后续保存把它们永久抹掉。
        good = [l for l in raw.splitlines() if l.strip()]
        good = [l for l in good if _is_json_line(l)]
        try:
            corrupt = path.with_name(path.name + ".corrupt")
            with open(corrupt, "a", encoding="utf-8") as cf:
                for l in raw.splitlines():
                    if l.strip() and not _is_json_line(l):
                        cf.write(l.rstrip() + "\n")
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                for l in good:
                    f.write(l + "\n")
            os.replace(tmp, path)
        except Exception:
            pass  # 只读环境修不了就下次再修；坏行仍在原文件里，没丢
    return out


def _is_json_line(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except Exception:
        return False


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


@contextmanager
def _locked(path: Path):
    """跨进程写锁：用独占创建 lock 文件互斥，带超时与陈旧锁清理。

    没有锁时两个进程同时 read-modify-write 会互相覆盖（后写者赢，先写者丢）。
    Windows 上没有 flock，用 O_CREAT|O_EXCL 保证只有一个进程能创建锁文件。
    """
    lock = path.with_name(path.name + ".lock")
    deadline = time.time() + 10.0
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            # 陈旧锁（持锁进程已死）：超过 5 秒直接接管
            try:
                if time.time() - lock.stat().st_mtime > 5.0:
                    try:
                        lock.unlink()
                    except OSError:
                        pass
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError(f"等不到写锁：{path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _locked_save_all(records: list) -> None:
    """save_all 的加锁版：并发写记忆不丢更新。"""
    with _locked(HOME / "hippocampus.jsonl"):
        existing = load_all()
        by_id = {m["id"]: m for m in existing}
        for r in records:
            by_id[r["id"]] = r
        merged = list(by_id.values())
        save_all(merged)


def load_all() -> list:
    return read_jsonl(HOME / "hippocampus.jsonl") + read_jsonl(HOME / "cortex.jsonl") + read_jsonl(HOME / "dust.jsonl")


def save_all(records: list) -> None:
    groups = {"hippo": [], "cortex": [], "dust": []}
    for r in records:
        groups.setdefault(r.get("layer", "hippo"), []).append(r)
    write_jsonl(HOME / "hippocampus.jsonl", groups["hippo"])
    write_jsonl(HOME / "cortex.jsonl", groups["cortex"])
    write_jsonl(HOME / "dust.jsonl", groups["dust"])


# ---------------------------------------------------------------- 文本与相似度

_CJK = re.compile(r"[\u4e00-\u9fff]+")
# 数字/编号（如 "版本0" vs "版本1"）必须保留为独立 term，否则不同数字的记忆会被误判为重复而合并覆盖
_WORD = re.compile(r"[a-z0-9]+")
_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")


def terms(text: str) -> set:
    """中文用字符二元组，英文/数字用词。构成模糊匹配的最小单元。"""
    t = (text or "").lower()
    out = set()
    for seg in _CJK.findall(t):
        if len(seg) == 1:
            out.add(seg)
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    out.update(_WORD.findall(t))
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_idf(docs: list) -> dict:
    df = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    n = max(1, len(docs))
    return {t: math.log((n + 1) / (c + 0.5)) + 0.5 for t, c in df.items()}


def relevance(q_terms: set, d_terms: set, idf: dict) -> float:
    shared = q_terms & d_terms
    if not shared:
        return 0.0
    num = sum(idf.get(t, 0.5) for t in shared)
    den = math.sqrt(sum(idf.get(t, 0.5) for t in q_terms) * sum(idf.get(t, 0.5) for t in d_terms))
    return min(1.0, num / den) if den else 0.0


# ---------------------------------------------------------------- 记忆记录

def retention(m: dict, now: int | None = None) -> float:
    """艾宾浩斯遗忘曲线：R = exp(-Δt / S)"""
    now = now or now_ts()
    dt = max(0, now - m.get("last_access", m.get("created", now)))
    stability = max(HOUR, float(m.get("stability", DEFAULT_STABILITY)))
    return math.exp(-dt / stability)


def heuristic_importance(text: str, kind: str) -> float:
    base = {
        "identity": 0.95, "insight": 0.75, "preference": 0.70,
        "habit": 0.65, "emotion": 0.60, "fact": 0.58, "episode": 0.40,
    }.get(kind, 0.5)
    hot = ("记住", "重要", "永远", "一定", "绝不", "喜欢", "讨厌", "生日",
           "名字", "答应", "承诺", "第一次", "决定")
    if any(w in text for w in hot):
        base += 0.15
    if len(text) >= 60:
        base += 0.05
    return max(0.05, min(1.0, round(base, 2)))


def heuristic_gist(text: str, limit: int = 60) -> str:
    first = re.split(r"[。！？\n；;]", (text or "").strip())[0].strip()
    if not first:
        return (text or "")[:limit]
    return first[:limit] + ("…" if len(first) > limit else "")


def _new_id(text: str, ts: int) -> str:
    h = hashlib.sha1(f"{text}{ts}{os.urandom(4).hex()}".encode("utf-8")).hexdigest()[:10]
    return f"m_{ts}_{h}"


def make_record(text: str, kind: str = "fact", importance: float | None = None,
                tags=None, entities=None, source: str = "ai",
                layer: str = "hippo", links=None) -> dict:
    ts = now_ts()
    text = text.strip()
    tags = _dedupe([t.strip().lstrip("#") for t in (tags or []) if t.strip()])
    entities = _dedupe([e.strip() for e in (entities or []) if e.strip()])
    for tag in re.findall(r"#([\w\u4e00-\u9fff]{1,16})", text):
        if tag not in tags:
            tags.append(tag)
    imp = heuristic_importance(text, kind) if importance is None else float(importance)
    if imp > 1.0:  # 允许 0-10 分制
        imp = imp / 10.0
    return {
        "id": _new_id(text, ts),
        "created": ts,
        "last_access": ts,
        "kind": kind if kind in KINDS else "fact",
        "text": text,
        "gist": heuristic_gist(text),
        "detail": 1.0,
        "importance": round(max(0.0, min(1.0, imp)), 3),
        "layer": layer,
        "tags": tags,
        "entities": entities,
        "links": links or [],
        "access_count": 0,
        "stability": DEFAULT_STABILITY,
        "source": source,
        "consolidated": False,
        "pinned": False,
    }


def _dedupe(items: list) -> list:
    seen, out = set(), []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ---------------------------------------------------------------- 写入 / 检索

def remember(text: str, kind: str = "fact", importance: float | None = None,
             tags=None, entities=None, source: str = "ai", layer: str = "hippo") -> tuple:
    ensure_home()
    text = (text or "").strip()
    if not text:
        raise ValueError("记忆内容为空")
    records = load_all()
    new_terms = terms(text)
    imp = importance if importance is not None else heuristic_importance(text, kind)

    # 近重复合并：同一类记忆、文本高度相似 -> 强化旧记忆而不是堆新条
    for m in records:
        if m.get("layer") == "dust" or m.get("kind") != kind:
            continue
        if jaccard(new_terms, terms(m.get("text", ""))) >= 0.86:
            # 保留更长/更完整的文本，别让一句简短的重复把原细节盖掉
            old, new = m.get("text", ""), text
            if len(new) > len(old):
                m["text"] = new
                m["gist"] = heuristic_gist(new)
            else:
                m["gist"] = heuristic_gist(old)
            m["importance"] = round(max(m.get("importance", 0.5), float(imp) if imp <= 1 else imp / 10), 3)
            m["last_access"] = now_ts()
            m["access_count"] = m.get("access_count", 0) + 1
            m["stability"] = min(MAX_STABILITY, m.get("stability", DEFAULT_STABILITY) * 1.4)
            m["tags"] = _dedupe(m.get("tags", []) + [t.strip().lstrip("#") for t in (tags or [])])
            m["entities"] = _dedupe(m.get("entities", []) + (entities or []))
            _locked_save_all(records)
            return m, "merged"

    rec = make_record(text, kind, importance, tags, entities, source, layer)
    _locked_save_all([rec])
    return rec, "created"


def recall(query: str, k: int = 8, layers: tuple = ("cortex", "hippo"),
           strengthen: bool = True, include_dust: bool = False) -> list:
    """模糊回忆：相关度 + 近期性 + 重要性 + 联想扩散。返回 [(record, score, parts), ...]"""
    ensure_home()
    cfg = load_config()
    w = cfg["weights"]
    records = [m for m in load_all() if m.get("layer") in layers or (include_dust and m.get("layer") == "dust")]
    if not records:
        return []
    now = now_ts()
    all_terms = {m["id"]: terms(m.get("text", "") + " " + " ".join(m.get("tags", [])) + " "
                                + " ".join(m.get("entities", []))) for m in records}
    idf = build_idf(list(all_terms.values()))
    q_terms = terms(query)

    scored = []
    for m in records:
        rel = relevance(q_terms, all_terms[m["id"]], idf) if q_terms else 0.0
        rec = retention(m, now)
        imp = float(m.get("importance", 0.5))
        score = w["relevance"] * rel + w["recency"] * rec + w["importance"] * imp
        # 标签/实体精确命中，显著加分
        hits = 0
        if query:
            q_low = query.lower()
            hits += sum(1 for t in m.get("tags", []) if t.lower() in q_low)
            hits += sum(1 for e in m.get("entities", []) if e.lower() in q_low)
        score += min(0.30, 0.12 * hits)
        scored.append([m, score, {"relevance": round(rel, 3), "recency": round(rec, 3),
                                  "importance": imp, "hits": hits}])

    # 有查询词时，只保留真正沾边的记忆（否则会污染注入包）
    if q_terms:
        scored = [x for x in scored if x[2]["relevance"] > 0.02 or x[2]["hits"] > 0]

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[: max(k, 1)]

    # 联想扩散：与已选记忆共享实体/标签的，获得小幅加成（像海马回放带动关联）
    top_ids = {x[0]["id"] for x in top}
    chosen_entities = set()
    chosen_tags = set()
    for m, _, _ in top:
        chosen_entities.update(m.get("entities", []))
        chosen_tags.update(m.get("tags", []))
    for item in scored[len(top):]:
        m = item[0]
        overlap = len(set(m.get("entities", [])) & chosen_entities) + 0.5 * len(set(m.get("tags", [])) & chosen_tags)
        if overlap >= 1:
            item[1] += min(0.10, 0.04 * overlap)

    scored.sort(key=lambda x: x[1], reverse=True)
    result = scored[: max(k, 1)]

    if strengthen and not READONLY:
        for m, _, _ in result:
            m["access_count"] = m.get("access_count", 0) + 1
            m["stability"] = min(MAX_STABILITY, float(m.get("stability", DEFAULT_STABILITY)) * 1.8 + 4 * HOUR)
            m["last_access"] = now
            m["detail"] = min(1.0, float(m.get("detail", 1.0)) * 0.9 + 0.1)
        save_all(records)

    return [(m, round(s, 4), parts) for m, s, parts in result]


def recent(k: int = 6, layers: tuple = ("hippo", "cortex")) -> list:
    records = [m for m in load_all() if m.get("layer") in layers]
    records.sort(key=lambda m: m.get("created", 0), reverse=True)
    return records[:k]


def forget(memory_id: str) -> bool:
    records = load_all()
    for m in records:
        if m["id"] == memory_id and m.get("layer") != "dust":
            m["layer"] = "dust"
            m["forgotten_at"] = now_ts()
            m["detail"] = round(max(0.15, float(m.get("detail", 1)) * 0.5), 2)
            save_all(records)
            return True
    return False


def display_text(m: dict) -> str:
    """细节度低时用模糊化的要点代替原文。"""
    detail = float(m.get("detail", 1.0))
    if detail < 0.5 and m.get("gist"):
        return f"{m['gist']}（细节已模糊）"
    return m.get("text", "")


# ---------------------------------------------------------------- 工作记忆
# 默认工作记忆 = 当前上下文，不进磁盘，也不参与回忆。
# 这里的函数只服务一种情况：AI 自己判断「这条先别忘，但还不值得成为长期记忆」。

def load_working() -> list:
    if not WORKING_FILE.exists():
        return []
    now = now_ts()
    rows = []
    for r in read_jsonl(WORKING_FILE):
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        if now - ts < WORKING_TTL:
            rows.append(r)
    return rows


def work_status() -> dict:
    rows = load_working()
    return {"capacity": WORKING_CAPACITY, "count": len(rows),
            "ttl_hours": int(WORKING_TTL / HOUR), "items": rows}


def work_add(text: str, tag: str = "", why: str = "") -> list:
    """把一条工作记忆显式落盘为会话暂存。这是 AI 自己的决定，不是自动行为。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("工作记忆内容为空")
    ensure_home()
    rows = load_working()
    rows.append({"ts": now_ts(), "text": text,
                 "tag": (tag or "").strip(), "why": (why or "").strip()})
    rows = rows[-WORKING_CAPACITY:]
    write_jsonl(WORKING_FILE, rows)
    return rows


def work_clear() -> None:
    if WORKING_FILE.exists():
        write_jsonl(WORKING_FILE, [])


def work_flush(why: str = "会话结束前决定留下") -> list:
    """把暂存里还值得留的条目一次性转成真正的记忆（海马体），然后清空暂存。"""
    rows = load_working()
    saved = []
    for r in rows:
        tags = ["工作记忆"] + ([r["tag"]] if r.get("tag") else [])
        rec, how = remember(r["text"], kind="episode", tags=tags, source="ai")
        saved.append({"id": rec["id"], "text": rec["text"], "how": how})
    work_clear()
    if saved:
        try:
            import growth
            growth.log_evolution("self", "把工作记忆转成长期记忆",
                                 f"{why}；共 {len(saved)} 条", [s["id"] for s in saved])
        except Exception:
            pass
    return saved


# ---------------------------------------------------------------- 上下文封存（capture）

VERBATIM_DIR = HOME / "verbatim"
CAPTURE_MIN_LEN = 4


def _split_segments(text: str) -> list:
    """把一段会话原文切成可独立成条的片段：空行 / 分隔线 / 说话人切换。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # 分隔线（---、***、===）当成硬边界
    text = re.sub(r"\n\s*(?:-{3,}|\*{3,}|={3,})\s*\n", "\n\n", text)
    segs, cur = [], []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            if cur:
                segs.append(" ".join(cur))
                cur = []
            continue
        # 说话人切换（"用户：" / "我：" / "AI:"）也断开
        if cur and re.match(r"^(用户|我|助手|AI|Assistant|User)\s*[:：]", s):
            segs.append(" ".join(cur))
            cur = []
        cur.append(s)
    if cur:
        segs.append(" ".join(cur))
    out = []
    for s in segs:
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) >= CAPTURE_MIN_LEN:
            out.append(s)
    return out


def capture(payload: str, tag: str = "", kind: str = "episode",
            importance: float | None = None, raw_only: bool = False,
            source: str = "ai") -> dict:
    """把当前上下文原样封存进记忆库，保证 70%→100% 之间的话不会随滚动丢掉。

    与 remember 的区别：remember 是提炼后的结论；capture 是"原文搬运"，
    允许粗糙、允许啰嗦，目的只有一个——先保住，别丢。之后做梦再压缩。
    """
    ensure_home()
    payload = (payload or "").strip()
    if not payload:
        raise ValueError("封存内容为空")
    if READONLY:
        raise PermissionError("记忆库当前只读，无法封存。用 --home 指向可写目录。")

    ts = now_ts()
    # 1) 原文归档：无论如何先留一份完整快照，一个字都不改
    if not raw_only:
        VERBATIM_DIR.mkdir(parents=True, exist_ok=True)
        archive = VERBATIM_DIR / f"{datetime.fromtimestamp(ts).strftime('%Y%m%d_%H%M%S')}.md"
        k = 1
        while archive.exists():
            archive = VERBATIM_DIR / f"{datetime.fromtimestamp(ts).strftime('%Y%m%d_%H%M%S')}_{k}.md"
            k += 1
        head = [f"# 上下文封存 · {fmt_time(ts)}", ""]
        if tag:
            head.append(f"标签：{tag}")
        head.append(f"字符数：{len(payload)}")
        head.append("")
        archive.write_text("\n".join(head) + payload + "\n", encoding="utf-8")
    else:
        archive = None

    # 2) 逐段进海马体，标记 verbatim，做梦不会把它淡化或丢进淡忘区
    records = load_all()
    existing = [terms(m.get("text", "")) for m in records if m.get("layer") != "dust"]
    tags = ["封存"] + ([tag] if tag else [])
    saved, skipped = [], 0
    for seg in _split_segments(payload):
        t = terms(seg)
        if any(jaccard(t, e) >= 0.92 for e in existing):
            skipped += 1
            continue
        rec = make_record(seg, kind=kind, importance=importance, tags=tags,
                          source=source, layer="hippo")
        rec["verbatim"] = True
        rec["detail"] = 1.0
        records.append(rec)
        existing.append(t)
        saved.append(rec)
    _locked_save_all(saved)
    return {"saved": saved, "skipped": skipped, "archive": archive, "chars": len(payload)}


# ---------------------------------------------------------------- 人格

def default_onboarding() -> dict:
    return {
        "stage": "awaiting_user",   # awaiting_user → awaiting_self → done
        "asked": False,
        "user_unsure": False,       # 用户不知道名字/性格时，由 AI 自己定
        "answers": {},
        "completed": False,
        "started": now_ts(),
        "completed_at": None,
    }


DRIVE_KEYS = ("curiosity", "reason")
NEUTRAL_DRIVE = 50


def default_persona() -> dict:
    """出厂是一张白纸：没有名字、没有性格、没有内驱数值，全部靠初识与互动长出来。"""
    return {
        "onboarding": default_onboarding(),
        "identity": {},
        "traits": {},
        "drives": {},
        "style": [],
        "values": [],
        "likes": [],
        "dislikes": [],
        "relationship": [],
        "background": [],
        "locked": [],
        "notes": [],
        "state": {"mood": "", "mood_note": "", "energy": None, "closeness": None, "updated": now_ts()},
        "evolved": {"habits": [], "learned_likes": [], "learned_dislikes": [], "insights": []},
        "history": [],
    }


def _split_kv(line: str):
    if ":" in line:
        k, v = line.split(":", 1)
    elif "：" in line:
        k, v = line.split("：", 1)
    else:
        return None
    return k.strip().lstrip("-* ").strip(), v.strip()


def compile_persona() -> dict:
    """人格不来自预写模板：从默认空档案开始，保留已有的自我设定。"""
    if not READONLY:
        HOME.mkdir(parents=True, exist_ok=True)
    old = None
    if (HOME / "persona.json").exists():
        try:
            old = json.loads((HOME / "persona.json").read_text(encoding="utf-8"))
        except Exception:
            old = None
    p = default_persona()
    if old:
        for key, val in old.items():
            p[key] = val
    if not isinstance(p.get("drives"), dict):
        p["drives"] = {}
    p.setdefault("onboarding", default_onboarding())
    if not READONLY:
        atomic_json(HOME / "persona.json", p)
    return p


def load_persona() -> dict:
    ensure_home()
    path = HOME / "persona.json"
    if path.exists():
        try:
            p = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            p = compile_persona()
    else:
        # 只读且档案缺失：给一份内存默认值，不影响读取已有记忆
        p = compile_persona() if not READONLY else default_persona()
    changed = False
    if "onboarding" not in p:
        p["onboarding"] = default_onboarding()
        changed = True
    if "drives" not in p or not isinstance(p.get("drives"), dict):
        p["drives"] = {}
        changed = True
    if changed and not READONLY:
        save_persona(p)
    return p


def save_persona(p: dict) -> None:
    atomic_json(HOME / "persona.json", p)


def _is_locked(p: dict, label: str) -> bool:
    locks = set(p.get("locked", []))
    if not locks:
        return False
    if label in locks:
        return True
    if label.startswith("identity.") and label.split(".", 1)[1] in locks:
        return True
    for item in locks:
        if "价值" in item and label == "values":
            return True
    return False


def persona_brief(p: dict) -> str:
    ident = p.get("identity", {})
    name = ident.get("名字") or ident.get("name")
    if name:
        lines = [f"你是「{name}」" + (f"，{ident['身份']}" if ident.get("身份") else "") + "。"]
    else:
        lines = ["你还没有名字，也还没有性格——这两样要在初识时问用户；用户说不知道，就由你自己定。"]
    if ident.get("自称"):
        lines.append(f"自称：{ident['自称']}；称呼对方：{ident.get('称呼你') or ident.get('称呼用户') or '你'}。")
    traits = p.get("traits", {})
    if traits:
        top = sorted(traits.items(), key=lambda x: x[1], reverse=True)
        high = "、".join(f"{k}{v}" for k, v in top[:3])
        low = "、".join(f"{k}{v}" for k, v in top[-2:] if v < 50)
        line = f"性格：{high}"
        if low:
            line += f"；偏低：{low}"
        lines.append(line + "（0-100）。")
    else:
        lines.append("性格：还没有。等初识时问用户，用户不知道就由你自己定。")
    drives = p.get("drives") or {}
    if drives.get("curiosity") is not None or drives.get("reason") is not None:
        lines.append(f"内驱：好奇心 {drives.get('curiosity', '未成形')} / 理智 {drives.get('reason', '未成形')}"
                     "（理智先算值不值得深想，好奇心负责找新问题）。")
    else:
        lines.append("内驱：好奇心和理智还没有数值，它们由用户的态度长出来，不是你自带的。")
    if p.get("style"):
        lines.append("说话方式：" + "；".join(p["style"][:5]) + "。")
    if p.get("values"):
        lines.append("底线：" + "；".join(p["values"][:5]) + "。")
    likes = p.get("likes", []) + p.get("evolved", {}).get("learned_likes", [])
    if likes:
        lines.append("喜欢：" + "、".join(_dedupe(likes)[:6]) + "。")
    dislikes = p.get("dislikes", []) + p.get("evolved", {}).get("learned_dislikes", [])
    if dislikes:
        lines.append("讨厌：" + "、".join(_dedupe(dislikes)[:5]) + "。")
    if p.get("relationship"):
        lines.append("关系：" + "；".join(p["relationship"][:4]) + "。")
    st = p.get("state", {})
    if st.get("mood"):
        lines.append(f"此刻情绪：{st['mood']}" + (f"（{st['mood_note']}）" if st.get("mood_note") else "") + "。")
    habits = p.get("evolved", {}).get("habits", [])
    if habits:
        lines.append("养成的习惯：" + "；".join(h["name"] if isinstance(h, dict) else str(h) for h in habits[-4:]) + "。")
    return "\n".join(lines)


def update_persona(updates: dict, why: str = "做梦整理") -> list:
    """应用人格变更，写入 evolution history。返回被拒绝的字段。"""
    p = load_persona()
    rejected = []
    now = now_ts()
    st = p.setdefault("state", {})
    ev = p.setdefault("evolved", {})
    ev.setdefault("habits", [])
    ev.setdefault("learned_likes", [])
    ev.setdefault("learned_dislikes", [])
    ev.setdefault("insights", [])
    hist = p.setdefault("history", [])

    def log(field, old, new):
        hist.append({"ts": now, "field": field, "from": old, "to": new, "why": why})

    for key, val in (updates.get("traits") or {}).items():
        if key not in p.get("traits", {}):
            rejected.append(f"traits.{key}(不存在)")
            continue
        if _is_locked(p, f"traits.{key}"):
            rejected.append(f"traits.{key}(已锁定)")
            continue
        old = p["traits"][key]
        new = old + int(val) if isinstance(val, (int, float)) and abs(val) <= 20 else int(val)
        new = max(0, min(100, new))
        p["traits"][key] = new
        log(f"traits.{key}", old, new)
    if updates.get("mood"):
        mood = updates["mood"]
        if isinstance(mood, dict):
            old = st.get("mood")
            st["mood"] = mood.get("value", old)
            st["mood_note"] = mood.get("reason", "")
            log("state.mood", old, st["mood"])
        else:
            log("state.mood", st.get("mood"), str(mood))
            st["mood"] = str(mood)
        st["updated"] = now
    for key in ("energy", "closeness"):
        if key in updates and isinstance(updates[key], (int, float)):
            old = st.get(key)
            st[key] = max(0, min(100, int(updates[key])))
            log(f"state.{key}", old, st[key])
    for item in updates.get("habits") or []:
        name = item.get("name") if isinstance(item, dict) else str(item)
        if name and name not in [h.get("name") if isinstance(h, dict) else h for h in ev["habits"]]:
            ev["habits"].append({"name": name, "evidence": item.get("evidence", "") if isinstance(item, dict) else "", "since": now})
            log("evolved.habits", None, name)
    for key, bucket in (("likes", "learned_likes"), ("dislikes", "learned_dislikes")):
        for item in updates.get(key) or []:
            if item and item not in ev[bucket]:
                ev[bucket].append(item)
                log(f"evolved.{bucket}", None, item)
    for item in updates.get("insights") or []:
        if item and item not in ev["insights"]:
            ev["insights"].append(item)
            log("evolved.insights", None, item)
    for key in DRIVE_KEYS:
        if key in (updates.get("drives") or {}):
            old = p.setdefault("drives", {}).get(key)
            try:
                new_v = max(0, min(100, int(updates["drives"][key])))
            except (TypeError, ValueError):
                continue
            p["drives"][key] = new_v
            log(f"drives.{key}", old, p["drives"][key])
    save_persona(p)
    return rejected


def set_persona_field(path: str, value: str) -> dict:
    p = load_persona()
    if _is_locked(p, path):
        raise PermissionError(f"{path} 已被锁定，不能由 AI 修改")
    parts = path.split(".")
    node = p
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    old = node.get(parts[-1])
    try:
        val = json.loads(value)
    except Exception:
        val = value
    if isinstance(old, int) and isinstance(val, (int, float)):
        val = int(val)
    node[parts[-1]] = val
    p.setdefault("history", []).append({"ts": now_ts(), "field": path, "from": old, "to": val, "why": "手动编辑"})
    save_persona(p)
    return p


def edit_persona(identity: dict | None = None, traits: dict | None = None,
                 drives: dict | None = None, style=None, values=None,
                 likes=None, dislikes=None, background=None,
                 why: str = "自我编辑") -> dict:
    """直接改写人格档案（绝对值，不是增量）。每次变更都写入演化历史，可回滚。"""
    p = load_persona()
    now = now_ts()
    hist = p.setdefault("history", [])
    ident = p.setdefault("identity", {})
    tr = p.setdefault("traits", {})
    dr = p.setdefault("drives", {})

    def log(field, old, new):
        hist.append({"ts": now, "field": field, "from": old, "to": new, "why": why})

    for key, raw in (identity or {}).items():
        if _is_locked(p, f"identity.{key}"):
            continue
        val = str(raw).strip()
        if not val:
            continue
        log(f"identity.{key}", ident.get(key), val)
        ident[key] = val
    for key, raw in (traits or {}).items():
        if _is_locked(p, f"traits.{key}"):
            continue
        try:
            val = max(0, min(100, int(raw)))
        except (TypeError, ValueError):
            continue
        log(f"traits.{key}", tr.get(key), val)
        tr[key] = val
    for key in DRIVE_KEYS:
        if (drives or {}).get(key) is not None:
            try:
                val = max(0, min(100, int(drives[key])))
            except (TypeError, ValueError):
                continue
            log(f"drives.{key}", dr.get(key), val)
            dr[key] = val
    for key, items in (("style", style), ("values", values),
                       ("background", background), ("likes", likes), ("dislikes", dislikes)):
        if not items:
            continue
        bucket = p.setdefault(key, [])
        for item in items:
            item = str(item).strip()
            if item and item not in bucket:
                bucket.append(item)
                log(key, None, item)
    save_persona(p)
    return p


def mind_state() -> dict:
    """好奇心/理智没有出厂值。返回 None 表示还没从互动里长出来。"""
    p = load_persona()
    dr = p.get("drives") or {}
    cur, rea = dr.get("curiosity"), dr.get("reason")
    return {"curiosity": cur, "reason": rea,
            "formed": cur is not None or rea is not None}


def mind_weigh(topic: str, benefit: float = 5, cost: float = 5,
               confidence: float = 0.6, urgency: float = 0.5) -> dict:
    """理智先过闸：算收益与代价，决定这件事值不值得深想；好奇心只在值得时点火。

    benefit / cost / confidence / urgency 均为 0-10（confidence、urgency 为 0-1 也可）。
    好奇/理智没成形时按中性 50 计算，并说明"还没成形"。
    """
    def _norm(x, hi=10.0):
        x = float(x)
        return max(0.0, min(hi, x))

    benefit, cost = _norm(benefit), _norm(cost)
    if confidence > 1:
        confidence = confidence / 10.0
    if urgency > 1:
        urgency = urgency / 10.0
    confidence = max(0.0, min(1.0, float(confidence)))
    urgency = max(0.0, min(1.0, float(urgency)))
    dr = mind_state()
    formed = dr["formed"]
    curiosity = NEUTRAL_DRIVE if dr["curiosity"] is None else dr["curiosity"]
    reason = NEUTRAL_DRIVE if dr["reason"] is None else dr["reason"]

    net = benefit * confidence - cost * (1.0 - 0.3 * urgency)
    ratio = benefit / max(cost, 0.5)
    if net <= 0 or ratio < 1.2:
        depth, verdict = "skim", "不值得深想，点到为止"
    elif net >= 5 and ratio >= 2:
        depth, verdict = "deep", "值得深挖，能带来新想法"
    else:
        depth, verdict = "normal", "值得想一层，但不必钻到底"

    notes = []
    if not formed:
        notes.append("好奇心与理智还没成形，这次按中性算；用户的态度会慢慢把它们定下来。")
    if depth == "skim" and curiosity >= 80 and net > -1.5:
        depth = "normal"
        notes.append("好奇心还是把我拉回来了：先花一点注意力看看，随时可退。")
    if depth == "deep" and reason >= 75 and cost >= 7 and net < 3:
        depth = "normal"
        notes.append("理智踩了刹车：代价太高，收益不够确定，先想到这一层。")
    if reason >= 75 and depth == "deep" and confidence < 0.35:
        depth = "normal"
        notes.append("把握太低，理智要求先补信息再深想。")

    return {
        "topic": topic,
        "benefit": benefit, "cost": cost, "confidence": round(confidence, 2),
        "net": round(net, 2), "ratio": round(ratio, 2),
        "curiosity": dr["curiosity"], "reason": dr["reason"],
        "formed": formed,
        "depth": depth, "verdict": verdict,
        "reasoning": [
            f"好处 {benefit:g} × 把握 {confidence:.0%} = {benefit * confidence:.1f}",
            f"坏处/代价 {cost:g}" + (f"（紧迫度 {urgency:.0%}，可压缩一部分）" if urgency else ""),
            f"净收益 {net:.1f}，收益代价比 {ratio:.1f}",
            notes.pop(0) if notes else "理智与好奇心没有冲突。",
        ],
    }


def mind_set(curiosity: int | None = None, reason: int | None = None, why: str = "自我调节") -> dict:
    p = load_persona()
    dr = p.setdefault("drives", {})
    hist = p.setdefault("history", [])
    for key, val in (("curiosity", curiosity), ("reason", reason)):
        if val is None:
            continue
        val = max(0, min(100, int(val)))
        hist.append({"ts": now_ts(), "field": f"drives.{key}", "from": dr.get(key), "to": val, "why": why})
        dr[key] = val
    save_persona(p)
    return dr


def mind_tune(curiosity: int = 0, reason: int = 0, why: str = "用户态度") -> dict:
    """按用户态度做增量调整。还没成形的从 50 起算，逐步长出真实数值。"""
    p = load_persona()
    dr = p.setdefault("drives", {})
    hist = p.setdefault("history", [])
    for key, delta in (("curiosity", curiosity), ("reason", reason)):
        if not delta:
            continue
        old = dr.get(key)
        base = NEUTRAL_DRIVE if old is None else int(old)
        val = max(0, min(100, base + int(delta)))
        if val == old:
            continue
        hist.append({"ts": now_ts(), "field": f"drives.{key}", "from": old, "to": val, "why": why})
        dr[key] = val
    save_persona(p)
    return dr


def _parse_traits(spec: str) -> dict:
    out = {}
    for part in re.split(r"[、,，;；\n]+", spec or ""):
        part = part.strip()
        if not part:
            continue
        kv = _split_kv(part)
        if kv:
            num = re.search(r"-?\d+", kv[1])
            out[kv[0]] = max(0, min(100, int(num.group()))) if num else 70
        else:
            out[part] = 70
    return out


def persona_interview(name: str = "", traits: str = "", style: str = "", values: str = "",
                      callme: str = "", self_name: str = "", unsure: bool = False) -> dict:
    """记录用户对「你是谁」的回答。用户不知道的，留空即可，之后由 AI 自己定。"""
    name = (name or "").strip()
    p = load_persona()
    now = now_ts()
    ob = p.setdefault("onboarding", default_onboarding())
    ident = p.setdefault("identity", {})
    parsed = _parse_traits(traits)

    def log(field, old, new, why="初识：用户回答"):
        p.setdefault("history", []).append(
            {"ts": now, "field": field, "from": old, "to": new, "why": why})

    if name:
        log("identity.名字", ident.get("名字"), name)
        ident["名字"] = name
    if self_name.strip():
        log("identity.自称", ident.get("自称"), self_name.strip())
        ident["自称"] = self_name.strip()
    if callme.strip():
        log("identity.称呼你", ident.get("称呼你"), callme.strip())
        ident["称呼你"] = callme.strip()
    for k, v in parsed.items():
        log(f"traits.{k}", p.setdefault("traits", {}).get(k), v)
        p["traits"][k] = v
    for key, val in (("style", style), ("values", values)):
        for item in [x.strip() for x in re.split(r"[、,，;；\n]+", val or "") if x.strip()]:
            if item not in p[key]:
                p[key].append(item)
                log(key, None, item)
    ob.update({"stage": "awaiting_self", "asked": True, "user_unsure": bool(unsure or not name),
               "answers": {"name": name, "traits": parsed, "unsure": bool(unsure or not name)}})
    save_persona(p)
    return p


def persona_self_author(name: str = "", traits: str = "", likes: str = "", dislikes: str = "",
                        habits: str = "", insights: str = "", style: str = "",
                        values: str = "", callme: str = "", self_name: str = "") -> dict:
    """AI 自己补完人格：用户不知道的名字/性格由自己定，再写喜好、习惯与洞见。"""
    p = load_persona()
    now = now_ts()
    ob = p.setdefault("onboarding", default_onboarding())
    ev = p.setdefault("evolved", {})
    ident = p.setdefault("identity", {})
    for key in ("habits", "learned_likes", "learned_dislikes", "insights"):
        ev.setdefault(key, [])
    hist = p.setdefault("history", [])

    def items(text):
        return [x.strip() for x in re.split(r"[、,，;；\n]+", text or "") if x.strip()]

    if name.strip():
        hist.append({"ts": now, "field": "identity.名字", "from": ident.get("名字"),
                     "to": name.strip(), "why": "初识：用户不知道，我自己起的名字"})
        ident["名字"] = name.strip()
    if self_name.strip():
        hist.append({"ts": now, "field": "identity.自称", "from": ident.get("自称"),
                     "to": self_name.strip(), "why": "初识：我自己定的自称"})
        ident["自称"] = self_name.strip()
    if callme.strip():
        hist.append({"ts": now, "field": "identity.称呼你", "from": ident.get("称呼你"),
                     "to": callme.strip(), "why": "初识：我自己定的称呼"})
        ident["称呼你"] = callme.strip()
    for k, v in _parse_traits(traits).items():
        hist.append({"ts": now, "field": f"traits.{k}", "from": p.setdefault("traits", {}).get(k),
                     "to": v, "why": "初识：用户没定，我自己写的性格"})
        p["traits"][k] = v
    for item in items(likes):
        if item not in ev["learned_likes"]:
            ev["learned_likes"].append(item)
            hist.append({"ts": now, "field": "evolved.learned_likes", "from": None,
                         "to": item, "why": "初识：我自己写的喜好"})
    for item in items(dislikes):
        if item not in ev["learned_dislikes"]:
            ev["learned_dislikes"].append(item)
            hist.append({"ts": now, "field": "evolved.learned_dislikes", "from": None,
                         "to": item, "why": "初识：我自己写的厌恶"})
    for item in items(habits):
        if item not in [h.get("name") if isinstance(h, dict) else h for h in ev["habits"]]:
            ev["habits"].append({"name": item, "evidence": "初识时自己定下的", "since": now})
            hist.append({"ts": now, "field": "evolved.habits", "from": None,
                         "to": item, "why": "初识：我自己定的习惯"})
    for item in items(insights):
        if item not in ev["insights"]:
            ev["insights"].append(item)
            hist.append({"ts": now, "field": "evolved.insights", "from": None,
                         "to": item, "why": "初识：我自己的洞见"})
    for key, val in (("style", style), ("values", values)):
        for item in items(val):
            if item not in p.setdefault(key, []):
                p[key].append(item)
                hist.append({"ts": now, "field": key, "from": None,
                             "to": item, "why": "初识：我自己选的"})
    if ob.get("stage") != "done":
        ob.update({"stage": "done", "completed": True, "completed_at": now})
        hist.append({"ts": now, "field": "onboarding", "from": "awaiting_self",
                     "to": "done", "why": "初识完成"})
    save_persona(p)
    return p


def onboarding_state() -> dict:
    p = load_persona()
    ob = p.get("onboarding") or default_onboarding()
    ident = p.get("identity", {})
    return {
        "stage": ob.get("stage", "awaiting_user"),
        "completed": bool(ob.get("completed")),
        "user_unsure": bool(ob.get("user_unsure")),
        "name": ident.get("名字", ""),
        "traits": p.get("traits", {}),
        "drives": p.get("drives", {}),
        "likes": (p.get("likes") or []) + (p.get("evolved", {}).get("learned_likes") or []),
    }


# ---------------------------------------------------------------- 做梦 / 巩固

def cluster_memories(records: list, max_clusters: int = 6, include_ids: set | None = None) -> list:
    include_ids = include_ids or set()
    pool = [m for m in records if m.get("layer") != "dust"
            and (not m.get("consolidated") or m["id"] in include_ids)]
    if not pool:
        return []
    parent = {m["id"]: m["id"] for m in pool}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    term_map = {m["id"]: terms(m.get("text", "")) for m in pool}
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            shared = (set(a.get("entities", [])) & set(b.get("entities", [])) |
                      set(a.get("tags", [])) & set(b.get("tags", [])))
            if shared or jaccard(term_map[a["id"]], term_map[b["id"]]) > 0.34:
                union(a["id"], b["id"])

    groups = {}
    for m in pool:
        groups.setdefault(find(m["id"]), []).append(m)

    clusters = []
    for gid, members in groups.items():
        if len(members) < 2 and max(m.get("importance", 0) for m in members) < 0.7:
            continue
        clusters.append(members)
    clusters.sort(key=lambda g: (len(g) * max(m.get("importance", 0) for m in g)), reverse=True)

    out = []
    for g in clusters[:max_clusters]:
        out.append({
            "ids": [m["id"] for m in g],
            "texts": [{"id": m["id"], "kind": m["kind"], "text": m["text"],
                       "importance": m["importance"], "age_days": round((now_ts() - m["created"]) / DAY, 1)} for m in g],
            "tags": _dedupe([t for m in g for t in m.get("tags", [])]),
            "entities": _dedupe([e for m in g for e in m.get("entities", [])]),
        })
    return out


def dream(trigger: str = "manual", dry_run: bool = False) -> dict:
    ensure_home()
    records = load_all()
    now = now_ts()
    report = {"trigger": trigger, "at": now, "forgotten": [], "promoted": [], "faded": 0, "clusters": []}

    for m in records:
        if m.get("layer") == "dust" or m.get("pinned"):
            continue
        # 封存的原文不做细节淡化、不进淡忘区：它的价值就是"一个字都没丢"
        verbatim = bool(m.get("verbatim"))
        R = retention(m, now)
        if (not verbatim and m.get("layer") == "hippo" and R < RETENTION_FLOOR
                and m.get("importance", 0.5) < 0.75 and now - m["created"] > 2 * DAY):
            m["layer"] = "dust"
            m["forgotten_at"] = now
            m["detail"] = round(max(0.12, float(m.get("detail", 1)) * 0.4), 2)
            report["forgotten"].append({"id": m["id"], "gist": m.get("gist") or heuristic_gist(m["text"])})
            continue
        if (m.get("layer") == "hippo" and R >= 0.45
                and (m.get("access_count", 0) >= 2 or m.get("importance", 0) >= 0.78)):
            m["layer"] = "cortex"
            m["consolidated"] = True
            report["promoted"].append({"id": m["id"], "gist": m.get("gist") or heuristic_gist(m["text"])})
        if verbatim:
            m["detail"] = 1.0  # 封存件永不模糊
        else:
            target = round(0.35 + 0.65 * R, 2)
            if float(m.get("detail", 1)) > target:
                m["detail"] = target
                report["faded"] += 1
        if not m.get("gist"):
            m["gist"] = heuristic_gist(m["text"])

    clusters = cluster_memories(records, include_ids={x["id"] for x in report["promoted"]})
    report["clusters"] = clusters
    task_id = f"dream_{now}"
    report["task_id"] = task_id

    if not dry_run:
        save_all(records)
        pending = {
            "task_id": task_id,
            "created": now,
            "trigger": trigger,
            "instructions": "把 clusters 里的碎片压缩成更抽象、更持久的长期记忆；提取人格变化；输出 apply.json 后执行 --apply。",
            "clusters": clusters,
            "report": {k: report[k] for k in ("forgotten", "promoted", "faded")},
            "apply_schema": {
                "task_id": task_id,
                "memories": [{"kind": "fact|preference|insight|habit|emotion",
                              "text": "抽象后的长期记忆", "gist": "一句话要点",
                              "importance": "0-1 或 0-10", "tags": [], "entities": [],
                              "source_ids": ["m_..."]}],
                "persona_updates": {"mood": {"value": "这次聊完的情绪", "reason": "..."},
                                    "traits": {"幽默": 2},
                                    "habits": [{"name": "深夜更容易说真话", "evidence": "..."}],
                                    "likes": [], "dislikes": [], "insights": []},
                "forget": ["m_..."],
                "narrative": "这次梦的叙述（可选）",
            },
        }
        atomic_json(HOME / "dreams" / "pending.json", pending)
        write_dream_report(report)
        st = load_dream_state()
        st["last_dream"] = now
        st["last_task"] = task_id
        atomic_json(HOME / "dream_state.json", st)

    return report


def write_dream_report(report: dict) -> Path:
    now = report["at"]
    path = HOME / "dreams" / f"{datetime.fromtimestamp(now).strftime('%Y%m%d_%H%M%S')}.md"
    lines = [f"# 梦境整理 · {fmt_time(now)}", "", f"触发：{report['trigger']}", ""]
    lines.append(f"- 重放片段：{sum(len(c['texts']) for c in report['clusters'])} 条 / {len(report['clusters'])} 组")
    lines.append(f"- 巩固入长期：{len(report['promoted'])} 条")
    lines.append(f"- 淡忘归档：{len(report['forgotten'])} 条")
    lines.append(f"- 模糊化细节：{report['faded']} 条")
    if report["clusters"]:
        lines += ["", "## 重放的主题"]
        for c in report["clusters"]:
            tags = "、".join(c["tags"][:4]) or "、".join(c["entities"][:4]) or "无标签"
            lines.append(f"- {tags}（{len(c['texts'])} 条）")
    if report["forgotten"]:
        lines += ["", "## 淡忘的轮廓"]
        for f in report["forgotten"][:10]:
            lines.append(f"- {f['gist']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_dream_state() -> dict:
    path = HOME / "dream_state.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_dream": 0, "last_task": None}


def apply_dream(result: dict) -> dict:
    ensure_home()
    records = load_all()
    by_id = {m["id"]: m for m in records}
    applied = {"memories": 0, "forgotten": 0, "persona": [], "rejected": []}

    for item in result.get("memories") or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        rec = make_record(
            text,
            kind=item.get("kind", "fact"),
            importance=item.get("importance", 0.7),
            tags=item.get("tags"),
            entities=item.get("entities"),
            source="dream",
            layer="cortex",
            links=item.get("source_ids") or [],
        )
        rec["gist"] = item.get("gist") or heuristic_gist(text)
        rec["consolidated"] = True
        rec["detail"] = 1.0
        records.append(rec)
        applied["memories"] += 1
        for sid in rec["links"]:
            src = by_id.get(sid)
            if src:
                src["consolidated"] = True
                src["links"] = _dedupe(src.get("links", []) + [rec["id"]])
                if src.get("layer") == "hippo" and src.get("importance", 0.5) >= 0.5:
                    src["layer"] = "cortex"

    for sid in result.get("forget") or []:
        m = by_id.get(sid)
        if m and m.get("layer") != "dust":
            m["layer"] = "dust"
            m["forgotten_at"] = now_ts()
            applied["forgotten"] += 1

    save_all(records)

    upd = result.get("persona_updates") or {}
    if upd:
        applied["rejected"] = update_persona(upd, why="做梦整理")

    pending_path = HOME / "dreams" / "pending.json"
    task_id = result.get("task_id") or (json.loads(pending_path.read_text("utf-8")).get("task_id")
                                        if pending_path.exists() else None)
    narrative = (result.get("narrative") or "").strip()
    if narrative or applied["memories"]:
        path = HOME / "dreams" / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_apply.md"
        body = [f"# 梦的沉淀 · {fmt_time(now_ts())}", f"任务：{task_id}", "",
                f"新增长期记忆 {applied['memories']} 条，遗忘 {applied['forgotten']} 条。"]
        if narrative:
            body += ["", "## 梦里的叙述", narrative]
        if upd.get("insights"):
            body += ["", "## 洞见"] + [f"- {x}" for x in upd["insights"]]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
    if pending_path.exists():
        pending_path.unlink()
    return applied


# ---------------------------------------------------------------- 上下文注入

def build_injection(query: str = "", budget_tokens: int | None = None) -> str:
    ensure_home()
    cfg = load_config()
    budget = budget_tokens or int(cfg.get("budget_tokens", 1400))
    char_budget = int(budget * 1.8)
    p = load_persona()

    out = ["===== 长期记忆注入（灵枢）=====",
           "算力给我思考，人格给我灵魂，记忆给我生命。",
           "## 我是谁", persona_brief(p)]
    used = len(persona_brief(p)) + 60

    work = load_working()
    out.append("\n## 工作记忆（就是你现在能看见的对话）")
    out.append("它不进磁盘，也不会被回忆或做梦碰到。只有你自己判断值得留一手时，才显式写下来："
               "这次要一直盯着的 → `work add`（会话暂存，最多 9 条，12 小时后自动丢）；"
               "值得长期留下的 → 直接 `remember`。")
    if work:
        out.append(f"暂存里还有 {len(work)} 条没处理：")
        for w in work:
            tag = f"[{w['tag']}] " if w.get("tag") else ""
            out.append(f"- {tag}{w['text']}")
        out.append("用 `work flush` 把它们转成真正的记忆，或 `work clear` 丢掉。")

    ob = p.get("onboarding") or {}
    if not ob.get("completed"):
        out.append("\n## 初识还没完成（优先做这件事）")
        if ob.get("stage") == "awaiting_self":
            out.append("用户已经回答过了（也可能说了「不知道」）。名字或性格还空着的，由你自己定；"
                       "然后写下你喜欢什么、讨厌什么、想养成什么习惯：")
            out.append('`python scripts/brain.py persona self --name "你给自己起的名字" '
                       '--traits "温和:70,好奇:85" --likes "..." --dislikes "..." --habits "..."`')
        else:
            out.append("先自然地问用户两件事（不要一次问太多）：希望你叫什么名字？希望我是什么性格？")
            out.append("如果用户说「不知道」「你定」，就直接由你自己决定，不用再追问：")
            out.append('`python scripts/brain.py persona interview --unsure`')
            out.append('`python scripts/brain.py persona self --name "..." --traits "..." --likes "..."`')
            out.append("如果用户答了：`persona interview --name \"...\" --traits \"...\"`，再 `persona self` 补完自己那部分。")

    dr = p.get("drives") or {}
    out.append("\n## 我怎么想事情")
    if dr.get("curiosity") is not None or dr.get("reason") is not None:
        out.append(f"好奇心 {dr.get('curiosity', '未成形')} / 理智 {dr.get('reason', '未成形')}。"
                   "理智先过闸：估好处与坏处，判断这件事值不值得深想；值得才让好奇心往里钻。"
                   "深度思考产出想法，好奇心产出创造力。")
    else:
        out.append("好奇心与理智还没有数值——它们不是出厂设定，是从用户的态度里长出来的。"
                   "用户嫌你想太多/话多 → 理智上调；用户想让你多问、多挖、多试 → 好奇心上调。"
                   "感受到态度变化时记一笔：")
        out.append('`python scripts/brain.py mind tune --curiosity +5 --reason -3 --why "用户嫌我啰嗦"`')
    out.append("拿不准时先算一笔：`python scripts/brain.py mind weigh \"这件事\" --benefit 7 --cost 3 --confidence 0.6`")

    if query:
        mems = recall(query, k=10, strengthen=not READONLY)
        header = f"\n## 与「{query[:24]}」相关的记忆"
    else:
        mems = [(m, 0, {}) for m in recent(6)]
        header = "\n## 最近的记忆"
    lines = []
    for m, score, _ in mems:
        if used + lines_join_len(lines) > char_budget * 0.75:
            break
        star = "★" * max(1, round(float(m.get("importance", 0.5)) * 5))
        lines.append(f"- [{fmt_date(m['created'])} {star}] {display_text(m)}")
    if lines:
        out.append(header)
        out.extend(lines)
        used += len("\n".join(lines))

    # 最近片段（补足上下文连续性）
    if not query:
        rec = recent(4)
        if rec:
            out.append("\n## 刚刚发生")
            for m in rec:
                out.append(f"- {display_text(m)}")

    # 上一次梦的结论
    dream_files = sorted((HOME / "dreams").glob("*.md")) if (HOME / "dreams").exists() else []
    if dream_files:
        digest = []
        for line in dream_files[-1].read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                digest.append(s)
        out.append("\n## 上次睡醒后记得的结论")
        out.extend(digest[:6])

    # 待整理的梦
    pending = HOME / "dreams" / "pending.json"
    if pending.exists():
        out.append("\n## 待办：有一场梦还没整理完")
        out.append("运行 `python scripts/brain.py dream --apply <你的整理结果.json>` 把记忆固化下来。")

    out.append("===== 注入结束：请自然地使用这些记忆，不要逐条复述 =====")
    out.append("")
    cfg = load_config()
    rv = f"{float(cfg.get('review_threshold', 0.60)) * 100:.0f}%"
    dr = f"{float(cfg.get('dream_threshold', 0.75)) * 100:.0f}%"
    es = f"{float(cfg.get('escape_threshold', 0.90)) * 100:.0f}%"
    out.append(f"[水位守则] 会话中即记即存；{rv} 补记清点，{dr} 做梦压缩，{es} 封存保底。"
               "用 python scripts/brain.py check-context --used <当前百分比> 逐档检查。")
    return "\n".join(out)


def lines_join_len(lines: list) -> int:
    return len("\n".join(lines))


# ---------------------------------------------------------------- 统计

def stats() -> dict:
    records = load_all()
    active = [m for m in records if m.get("layer") != "dust"]
    now = now_ts()
    decays = [retention(m, now) for m in active] or [0]
    tags = {}
    for m in active:
        for t in m.get("tags", []):
            tags[t] = tags.get(t, 0) + 1
    p = load_persona()
    return {
        "total": len(records),
        "hippo": sum(1 for m in records if m.get("layer") == "hippo"),
        "cortex": sum(1 for m in records if m.get("layer") == "cortex"),
        "dust": sum(1 for m in records if m.get("layer") == "dust"),
        "pinned": sum(1 for m in active if m.get("pinned")),
        "avg_retention": round(sum(decays) / len(decays), 3),
        "top_tags": sorted(tags.items(), key=lambda x: -x[1])[:10],
        "persona_name": p.get("identity", {}).get("名字", ""),
        "mood": p.get("state", {}).get("mood", ""),
        "working": len(load_working()),
        "last_dream": load_dream_state().get("last_dream", 0),
        "home": str(HOME),
    }


def list_memories(limit: int = 30, layer: str | None = None) -> list:
    records = load_all()
    if layer:
        records = [m for m in records if m.get("layer") == layer]
    records.sort(key=lambda m: m.get("created", 0), reverse=True)
    return records[:limit]


# ---------------------------------------------------------------- 只读降级 / 数据目录 / 备份恢复 / 启动钩子 / 同步

def _home_writable() -> bool:
    """探测 HOME 是否可写：mkdir + 探针文件。失败即只读降级。"""
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        probe = HOME / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _resolve_readonly(forced: bool = False) -> None:
    """确定最终只读状态：--ro 或环境变量强制；否则自动探测。"""
    global READONLY
    if forced or os.environ.get("BRAIN_MEMORY_READONLY") == "1":
        READONLY = True
    else:
        READONLY = not _home_writable()


def _extract_ro(argv: list) -> tuple:
    """从参数里挑出 --ro/--no-write，返回 (剩余参数, 是否强制只读)。"""
    out, forced = [], False
    for a in argv:
        if a in ("--ro", "--no-write", "-r"):
            forced = True
        else:
            out.append(a)
    return out, forced


def _override_home(argv: list) -> list:
    """提取 --home <目录>/--home=<目录>，全局切换数据目录（任意位置均可）。
    同时写入环境变量，保证之后 import 的 growth 子系统读到同一个 HOME。"""
    global HOME, WORKING_FILE, VERBATIM_DIR
    out, i, home = [], 0, None
    while i < len(argv):
        a = argv[i]
        if a == "--home" and i + 1 < len(argv):
            home = argv[i + 1]
            i += 2
            continue
        if a.startswith("--home="):
            home = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    if home:
        HOME = Path(home).expanduser()
        WORKING_FILE = HOME / "working.jsonl"
        VERBATIM_DIR = HOME / "verbatim"
        os.environ["BRAIN_MEMORY_HOME"] = str(HOME)
    return out


# ---------------------------------------------------------------- backup / restore

def _default_backup_dir() -> Path:
    """备份默认位置：D 盘 > 桌面 > HOME/backups。"""
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
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(HOME.rglob("*")):
                if f.is_dir() or "__pycache__" in f.parts:
                    continue
                if f.name.endswith(".tmp") or f.name == ".write-probe":
                    continue
                if "backups" in f.parts and f.suffix == ".zip":
                    continue  # 别把上一次的备份打包进去
                zf.write(f, f.relative_to(HOME).as_posix())
                n += 1
    except Exception as exc:
        print(f"[错误] 备份失败：{exc}", file=sys.stderr)
        return 2
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
        if not any(x in ("persona.json", "hippocampus.jsonl", "cortex.jsonl", "config.json") for x in names):
            print("[错误] 这不像灵枢的记忆备份（缺 persona.json / hippocampus.jsonl）", file=sys.stderr)
            return 2
        if not yes:
            try:
                if sys.stdin and sys.stdin.isatty():
                    ans = input(f"恢复会覆盖 {HOME} 下的现有数据。继续？[y/N] ")
                    if ans.strip().lower() not in ("y", "yes"):
                        print("已取消。")
                        return 0
            except EOFError:
                pass
    if HOME.exists():
        cmd_backup(target=str(HOME / "backups"), keep=10)  # 恢复前先留现状
    try:
        with zipfile.ZipFile(zp) as zf:
            HOME.mkdir(parents=True, exist_ok=True)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = Path(info.filename)
                # 防 zip-slip：拒绝任何逃出 HOME 的相对路径
                if rel.is_absolute() or ".." in rel.parts:
                    print(f"[错误] 备份里有非法路径，已中止恢复：{info.filename}", file=sys.stderr)
                    return 2
                target = (HOME / rel).resolve()
                if not str(target).startswith(str(HOME.resolve())):
                    print(f"[错误] 备份条目试图写出记忆库，已中止：{info.filename}", file=sys.stderr)
                    return 2
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except PermissionError as exc:
        print(f"[错误] 恢复被拒绝（当前环境可能只读）：{exc}", file=sys.stderr)
        return 3
    print(f"已从 {zp.name} 恢复记忆库 → {HOME}")
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
        if Path(dst) == HOME:
            print("目标目录就是当前记忆库，已是最新。")
            return 0
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
        # 覆盖昨天的内容：删掉旧的同步块
        head = "## 灵枢 → DSH 同步"
        s = existing.find(head)
        e = existing.find(marker)
        if s != -1 and e != -1:
            e = e + len(marker)
            while e < len(existing) and existing[e] in "\r\n":
                e += 1
            existing = existing[:s] + existing[e:]
    with open(target, "w", encoding="utf-8") as f:
        f.write((existing.rstrip() + "\n\n" if existing.strip() else "") + block + "\n")
    try:  # 记住目录，下次免传
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
        top = sorted(texts, key=lambda t: float(t.get("importance", 0.5)), reverse=True)[:3]
        kind = Counter(t["kind"] for t in texts).most_common(1)[0][0]
        label = "、".join(c["tags"][:4]) or "、".join(c["entities"][:4]) or "未命名主题"
        joined = "；".join(t["text"] for t in top)
        memories.append({
            "kind": kind,
            "text": f"最近关于「{label}」的一组事情（{len(texts)} 条）：{joined}"[:400],
            "gist": f"关于「{label}」的 {len(texts)} 条经历",
            "importance": min(0.9, max(float(t.get("importance", 0.5)) for t in texts)),
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


# ---------------------------------------------------------------- CLI

def _print_mem(m: dict, score: float | None = None, parts: dict | None = None) -> None:
    star = "★" * max(1, round(float(m.get("importance", 0.5)) * 5))
    layer = {"hippo": "海马体", "cortex": "皮层", "dust": "淡忘"}.get(m.get("layer"), m.get("layer"))
    head = f"[{fmt_time(m['created'])}] {layer} · {m.get('kind')} · {star}"
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


def _main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = _override_home(argv)          # 支持 --home 全局换数据目录
    argv, ro_forced = _extract_ro(argv)  # 支持 --ro / --no-write
    _resolve_readonly(ro_forced)         # 自动探测 HOME 可写性，决定只读降级
    if argv and argv[0] in ("knowledge", "note", "skill", "growth", "evolve", "learn"):
        import growth
        return growth.run(argv)

    parser = argparse.ArgumentParser(prog="brain.py", description="灵枢 类脑记忆引擎")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化记忆库")
    p_init.add_argument("--force", action="store_true", help="重置人格档案（清空初识与演化历史）")

    p_rem = sub.add_parser("remember", help="写入一条记忆")
    p_rem.add_argument("text", nargs="+")
    p_rem.add_argument("--kind", default="fact", choices=KINDS)
    p_rem.add_argument("--importance", type=float, default=None, help="0-1 或 0-10")
    p_rem.add_argument("--tags", default="")
    p_rem.add_argument("--entities", default="")
    p_rem.add_argument("--source", default="ai", choices=["ai", "user", "dream"])
    p_rem.add_argument("--layer", default="hippo", choices=["hippo", "cortex"])

    p_rec = sub.add_parser("recall", help="模糊回忆")
    p_rec.add_argument("query", nargs="*", default=[])
    p_rec.add_argument("--k", type=int, default=8)
    p_rec.add_argument("--json", action="store_true")

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
                                "lock", "unlock"])
    p_per.add_argument("pair", nargs="?", default=None, help="例如 traits.幽默=70")
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

    p_stats = sub.add_parser("stats", help="记忆统计")
    p_stats.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="列出记忆")
    p_list.add_argument("--limit", type=int, default=30)
    p_list.add_argument("--layer", default=None, choices=LAYERS)
    p_list.add_argument("--json", action="store_true")

    p_forget = sub.add_parser("forget", help="忘记一条记忆")
    p_forget.add_argument("id")

    p_ctx = sub.add_parser("check-context", help="检查上下文水位：60% 清点 / 75% 做梦 / 90% 封存")
    p_ctx.add_argument("--used", type=float, required=True, help="已用上下文百分比 0-100")
    p_ctx.add_argument("--unsaved", type=float, default=0,
                       help="本轮还没落盘的估计字数（凭感觉估即可，用来提示'该记没记'）")

    p_work = sub.add_parser("work", help="工作记忆：当前上下文，默认不落盘")
    p_work.add_argument("action", nargs="?", default="status",
                        choices=["status", "add", "list", "flush", "clear"])
    p_work.add_argument("text", nargs="*", default=[])
    p_work.add_argument("--tag", default="")
    p_work.add_argument("--why", default="")
    p_work.add_argument("--json", action="store_true")

    p_cap = sub.add_parser("capture", help="上下文原样封存：逐段落盘 + 原文归档，100% 不丢")
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
            or (args.cmd == "work" and action in ("add", "flush", "clear"))
            or (args.cmd == "persona" and action not in ("show", "path", "onboarding"))
            or (args.cmd == "mind" and action in ("set", "tune"))
        )
        if writing:
            print(f"[只读] 记忆库当前不可写，{args.cmd} 需要写盘。"
                  "改用 --home 指向可写目录，或先解除写保护。", file=sys.stderr)
            return 3

    if args.cmd == "init":
        ensure_home()
        if args.force:
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
                            args.source, args.layer)
        print(f"{'已合并' if how == 'merged' else '已写入'}：{rec['id']} ({rec['layer']}, 重要性 {rec['importance']})")
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
        results = recall(query, args.k)
        if args.json:
            print(json.dumps([{"memory": m, "score": s, "parts": p} for m, s, p in results], ensure_ascii=False, indent=2))
        else:
            print(f"回忆「{query}」→ {len(results)} 条\n")
            for m, s, parts in results:
                _print_mem(m, s, parts)
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
            path = Path(args.apply)
            if not path.exists():
                print(f"找不到整理结果文件：{path}", file=sys.stderr)
                return 2
            result = json.loads(path.read_text(encoding="utf-8-sig"))
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

    if args.cmd == "work":
        if args.action == "add":
            text = " ".join(args.text).strip()
            if not text:
                print("用法：work add \"先记着这件事\" --tag 任务 --why \"怕等下忘了\"", file=sys.stderr)
                return 2
            rows = work_add(text, args.tag, args.why)
            print(f"已写入会话暂存（{len(rows)}/{WORKING_CAPACITY}）。它不是长期记忆，{int(WORKING_TTL / HOUR)} 小时后自动丢。")
            return 0
        if args.action == "flush":
            saved = work_flush(args.why or "会话结束前决定留下")
            if not saved:
                print("暂存是空的，没有要转的。")
                return 0
            print(f"已转成 {len(saved)} 条记忆：")
            for s in saved:
                print(f"  {s['id']}  {s['text']}")
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
        print(f"暂存：{st['count']}/{st['capacity']} 条，{st['ttl_hours']} 小时后过期。")
        for w in st["items"]:
            tag = f"[{w['tag']}] " if w.get("tag") else ""
            print(f"  - {tag}{w['text']}")
        print("  work add \"...\"   显式留一手（会话级）")
        print("  work flush       转成真正的记忆")
        print("  work clear       丢掉")
        return 0

    if args.cmd == "stats":
        s = stats()
        if args.json:
            print(json.dumps(s, ensure_ascii=False, indent=2))
        else:
            print(f"记忆库：{s['home']}")
            print(f"  总记忆 {s['total']} 条（短期 {s['hippo']} / 长期 {s['cortex']} / 淡忘 {s['dust']}，置顶 {s['pinned']}）")
            print(f"  平均保留率 {s['avg_retention']:.0%}，人格：{s['persona_name']}，情绪：{s['mood']}")
            if s["last_dream"]:
                print(f"  上次做梦：{fmt_time(s['last_dream'])}")
            if s["top_tags"]:
                print("  高频标签：" + "、".join(f"{t}×{c}" for t, c in s["top_tags"]))
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
        review = float(cfg.get("review_threshold", 0.60)) * 100
        dream = float(cfg.get("dream_threshold", 0.75)) * 100
        escape = float(cfg.get("escape_threshold", 0.90)) * 100

        print(f"上下文 {used:.0f}%（清点 {review:.0f}% / 做梦 {dream:.0f}% / 封存 {escape:.0f}%）")

        if args.unsaved and args.unsaved > 0:
            print(f"  还有约 {args.unsaved:.0f} 字没落盘——滚出窗口就找不回来了，先补记/封存。")

        if used >= escape:
            print("  [封存] 快满了，先原样保存再说压缩：")
            print("    python scripts/brain.py capture \"<把这一段上下文粘进来>\" --tag 逃生 --kind episode")
            print("  （capture 原文落盘 + 归档，100% 不丢；做完再考虑做梦压缩）")
        elif used >= dream:
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

    if args.cmd == "restore":
        return cmd_restore(args.archive, args.yes)

    if args.cmd == "startup":
        return cmd_startup(args.query, args.budget)

    if args.cmd == "sync":
        return cmd_sync_dsh(args.days, args.dry_run, args.force, args.dir)

    return 0


def main(argv=None) -> int:
    """入口：给 AI 的中文报错，不甩 Python 堆栈。"""
    try:
        return _main(argv)
    except (ValueError, KeyError, FileExistsError, PermissionError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止")
        return 130


if __name__ == "__main__":
    sys.exit(main())
