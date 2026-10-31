#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 灵枢 · 记忆：分层存储、编码/回忆、归档、工作记忆、capture、关联、注入与统计。"""

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

def detect_conflicts(limit: int = 50) -> list:
    """启发式扫描"可能矛盾的记忆"（如「我用 Windows」vs「我改用 Linux」）。

    按实体分组，组内两两比较：词面有交集、且一条带改口/否定信号、另一条不带，
    就列为候选。只做提醒、不下结论——到底矛不矛盾由人来判。
    """
    records = load_all()
    by_entity: dict[str, list] = {}
    for m in records:
        for ent in (m.get("entities") or []):
            by_entity.setdefault(str(ent), []).append(m)

    change_cues = ("改用", "换成", "改成", "改为", "不再", "已经不用", "换了", "现在用",
                   "现在不用", "不喝", "不吃", "不用", "戒了", "停了", "放弃", "移除", "删了")
    neg_cues = ("不", "没", "别", "反")
    found, seen = [], set()

    def flag(a, b, reason):
        key = tuple(sorted((a["id"], b["id"])))
        if key in seen:
            return
        seen.add(key)
        found.append({
            "a": {"id": a["id"], "text": a["text"], "ts": a.get("created")},
            "b": {"id": b["id"], "text": b["text"], "ts": b.get("created")},
            "reason": reason,
        })

    for ent, members in by_entity.items():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a["id"] == b["id"]:
                    continue
                inter = set(terms(a.get("text", ""))) & set(terms(b.get("text", "")))
                if len(inter) < 1:
                    continue
                ca = any(c in a.get("text", "") for c in change_cues)
                cb = any(c in b.get("text", "") for c in change_cues)
                if ca != cb:
                    flag(a, b, f"关于「{ent}」的两条记忆词面相近，但其中一条带改口信号"
                               "（改用/不再/换成…），可能已经过时或更新了")
                    continue
                na = any(c in a.get("text", "") for c in neg_cues)
                nb = any(c in b.get("text", "") for c in neg_cues)
                if na != nb:
                    flag(a, b, f"关于「{ent}」的两条记忆一条肯定一条否定，可能互相矛盾")

    found.sort(key=lambda x: min(x["a"]["ts"] or 0, x["b"]["ts"] or 0), reverse=True)
    return found[:limit]


# ---------------------------------------------------------------- 审核闸

def pending_memory_reviews() -> list:
    """AI（做梦等）整理出来、还没人工确认的记忆。"""
    return [m for m in load_all()
            if m.get("review") == "pending" and m.get("layer") != "dust"]


def ai_persona_changes(days: int = 7) -> list:
    """最近 N 天里由 AI（非手动）发起的人格变更，用于溯源与漂移告警。

    注意：做梦 traits 微调没过漂移门限也会写 history（field 带 "(漂移累积)"
    后缀、to 写"未过门限/未变"）——那些行什么都没改，不算 AI 改动，
    否则审核清单和漂移告警会被一堆"没生效的记账"撑得动不动就偏大。
    """
    p = load_persona()
    cutoff = now_ts() - days * 86400
    out = []
    for h in p.get("history", []):
        if float(h.get("ts", 0)) < cutoff:
            continue
        src = str(h.get("source", ""))
        why = str(h.get("why", ""))
        field = str(h.get("field", ""))
        to = h.get("to")
        # 没真正改的记账行：字段带括号注记（如 "traits.幽默(漂移累积)"），
        # 或 to 是"未过门限/未变/撞在边界上"这类说明文字而非实际新值
        if "(" in field:
            continue
        if isinstance(to, str) and ("未过门限" in to or "未变" in to or "边界" in to):
            continue
        # 优先看 source 字段（比 why 可靠）；用户自己说的（初识回答、手动编辑、
        # 回滚、面板保存）不算 AI 私改
        if src:
            if src in ("面板", "用户", "手动编辑", "回滚", "restore", "persona restore"):
                continue
            if "初识" in src and "我自己" not in src:
                continue
            out.append(h)
            continue
        if ("用户" in why) or why in ("手动编辑", "回滚", "persona restore") or "初识" in why:
            continue
        out.append(h)
    return out


def set_memory_review(mid: str, status: str) -> dict:
    """确认 / 废弃一条 AI 整理的记忆（confirm → 转正；discard → 移入淡忘区）。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法审核。")
    # 读-改-写整体放在写锁里：否则并发的 remember/dream 可能在我们读完之后插入新记忆，
    # 被这里的 save_all 用旧快照整库覆盖而丢失。
    with _locked(HOME / "hippocampus.jsonl"):
        records = load_all()
        target = next((m for m in records if m.get("id") == mid), None)
        if not target:
            raise ValueError(f"没有这条记忆：{mid}")
        if status == "confirm":
            target["review"] = "confirmed"
            target["review_at"] = now_ts()
        elif status == "discard":
            # pinned 不许 discard：与 apply_dream 的 forget 同一道防线
            if target.get("pinned"):
                raise ValueError("这条记忆是用户置顶的，不能 discard，请直接 confirm 或手动处理")
            target["layer"] = "dust"
            target["forgotten_at"] = now_ts()
            target["review"] = "discarded"
        else:
            raise ValueError("状态只能是 confirm / discard")
        save_all(records)
    return target


def review_report() -> dict:
    """待审核清单：AI 记忆 / AI 知识 / AI 人格变更。"""
    mems = []
    for m in pending_memory_reviews():
        mid = m.get("id")
        if not mid:
            continue   # 畸形记录（缺 id）没法在报告里给定位，直接跳过
        mems.append({"id": mid, "text": m.get("text", ""), "created": m.get("created"),
                     "source": m.get("review_source", "dream")})
    try:
        import growth
        kn = [{"id": k.get("id"), "title": k.get("title", ""), "topic": k.get("topic", "")}
              for k in growth.knowledge_pending()]
    except Exception:
        kn = []
    return {"memories": mems, "knowledge": kn,
            "persona": [{"ts": h.get("ts"), "field": h.get("field"),
                         "from": h.get("from"), "to": h.get("to"), "why": h.get("why")}
                        for h in ai_persona_changes(7)]}


def review_counts() -> dict:
    rep = review_report()
    return {"memories": len(rep["memories"]), "knowledge": len(rep["knowledge"]),
            "persona": len(rep["persona"])}


def bulk_memory_action(ids: list, action: str, value=None) -> dict:
    """面板批量操作：一次写锁内完成，避免 N 条逐条读-改-写整库。

    action:
      confirm / discard      —— 审核转正 / 废弃（移入淡忘区）
      importance             —— 批量设置重要性（value: 0-1 或 0-10）
      add_tags / remove_tags —— 批量加 / 删标签（value: list[str]）
      pin / unpin            —— 批量置顶 / 取消置顶
      delete                 —— 批量移入淡忘区（同 forget，不真删，可回捞）
    """
    if READONLY:
        raise PermissionError("记忆库当前只读，无法批量修改。")
    ids = [str(x) for x in (ids or []) if str(x).strip()]
    if not ids:
        raise ValueError("没有选中任何记忆")
    if action not in ("confirm", "discard", "importance", "add_tags", "remove_tags",
                      "pin", "unpin", "delete"):
        raise ValueError("不支持的批量操作")
    # 预校验参数：坏值在动库之前就报清楚，别让 float(None) 之类变成 500
    imp_val = None
    tag_list = None
    if action == "importance":
        try:
            imp_val = float(value)
        except (TypeError, ValueError):
            raise ValueError("重要性必须是数字（1-10）")
        if imp_val < 0 or imp_val > 10:
            raise ValueError("重要性要在 0-10 之间")
    elif action in ("add_tags", "remove_tags"):
        if isinstance(value, str):
            tag_list = [t.strip() for t in re.split(r"[,，;；、\n]+", value) if t.strip()]
        elif isinstance(value, (list, tuple)):
            tag_list = [str(t).strip() for t in value if str(t).strip()]
        else:
            raise ValueError("标签必须是数组，或逗号分隔的文本")
        if not tag_list:
            raise ValueError("没有可用的标签")
    want = set(ids)
    updated = 0
    with _locked(HOME / "hippocampus.jsonl"):
        records = load_all()
        for m in records:
            if m.get("id") not in want:
                continue
            if action == "confirm":
                m["review"] = "confirmed"
                m["review_at"] = now_ts()
            elif action == "discard":
                m["layer"] = "dust"
                m["forgotten_at"] = now_ts()
                m["review"] = "discarded"
            elif action == "importance":
                # 刻度约定：>1 视为 10 分制（如 8 → 0.8），≤1 视为 0-1 小数。
                # 0.1~1 之间是合法小数；正好 1 当作满分 1.0（10 分制不会写 1）
                m["importance"] = round(max(0.0, min(1.0, imp_val / 10 if imp_val > 1 else imp_val)), 3)
            elif action == "pin":
                m["pinned"] = True
            elif action == "unpin":
                m["pinned"] = False
            elif action == "delete":
                if m.get("layer") != "dust":
                    m["layer"] = "dust"
                    m["forgotten_at"] = now_ts()
                    m["detail"] = round(max(0.15, _num(m.get("detail"), 1.0) * 0.5), 2)
            elif action == "add_tags":
                cur = [str(t) for t in (m.get("tags") or [])]
                for t in tag_list:
                    if t not in cur:
                        cur.append(t)
                m["tags"] = cur
            elif action == "remove_tags":
                drop = set(tag_list)
                m["tags"] = [str(t) for t in (m.get("tags") or []) if str(t) not in drop]
            updated += 1
        if updated:
            save_all(records)
    return {"updated": updated, "requested": len(ids), "action": action,
            "review_counts": review_counts()}


def audit() -> dict:
    """一次自查：待审核项 + 冲突记忆 + 该做什么。"""
    rep = review_report()
    conf = detect_conflicts(20)
    tips = []
    if rep["memories"]:
        tips.append(f"有 {len(rep['memories'])} 条 AI 整理的记忆待审核："
                    "`review` 看全文 → `review confirm <id>` 或 `review discard <id>`。")
    if rep["knowledge"]:
        tips.append(f"有 {len(rep['knowledge'])} 条知识待审核：`knowledge review` → confirm/discard。")
    if rep["persona"]:
        tips.append(f"最近 7 天 AI 改了 {len(rep['persona'])} 次人格："
                    "`persona history` 看来源，跑偏就 `persona rollback <序号>`。")
    if conf:
        tips.append(f"有 {len(conf)} 组疑似冲突记忆：`detect-conflicts` 查看，别让旧记忆继续误导。")
    n_arch = len(load_archive())
    if n_arch:
        tips.append(f"归档层有 {n_arch} 条冷记忆（默认不参与回忆，原文保留）："
                    "`archive search <词>` 找、`archive restore <id>` 回捞。")
    if not tips:
        tips.append("干净：没有待审核项、没有明显冲突。")
    return {"counts": {"memories": len(rep["memories"]),
                       "knowledge": len(rep["knowledge"]),
                       "persona": len(rep["persona"]),
                       "conflicts": len(conf),
                       "archive": n_arch},
            "review": rep, "conflicts": conf, "tips": tips}


def load_all() -> list:
    """三层热层合并；缺 id 或不是 dict 的畸形记录一律跳过——
    项目里没有任何调用方想处理畸形记录，在这里统一过滤最省事。"""
    raw = read_jsonl(HOME / "hippocampus.jsonl") + read_jsonl(HOME / "cortex.jsonl") + read_jsonl(HOME / "dust.jsonl")
    good, bad = [], 0
    for r in raw:
        if isinstance(r, dict) and r.get("id"):
            good.append(r)
        else:
            bad += 1
    if bad:
        log_event("load", f"load_all 跳过 {bad} 条畸形记录（缺 id 或类型错）", level="warning")
    return good


def load_archive() -> list:
    """归档层独立读取：load_all 不含 archive，热查询集保持精简。

    所有现有路径（recall/forget/dream/remember…）行为不变；需要全量计数或展示时
    （stats / 面板 / audit / archive 命令）再显式合并 load_archive()。
    """
    raw = read_jsonl(HOME / "archive.jsonl")
    good, bad = [], 0
    for r in raw:
        if isinstance(r, dict) and r.get("id"):
            good.append(r)
        else:
            bad += 1
    if bad:
        log_event("load", f"load_archive 跳过 {bad} 条畸形记录", level="warning")
    return good


def _hay(m: dict) -> str:
    """廉价的"可检索全文"拼接（小写）：text + tags + entities。

    用于 recall/remember/archive search 的第一道预筛（子串包含测试）。数学上不丢召回：
    relevance/jaccard 非零当且仅当查询 term 与文档 terms 共享，而共享 term 必然是
    本文档某段文本的连续子串 → `t in hay` 必为真。统一小写，英文词忽略大小写。
    """
    parts = [str(m.get("text") or "")]
    for k in ("tags", "entities"):
        parts.append(" ".join(str(x) for x in (m.get(k) or [])))
    return "\n".join(parts).lower()


def _is_archivable(m: dict, now: int) -> bool:
    """单条判断：是否该被雪藏进归档层。archive_candidates / dream 共用同一套条件，避免漂移。"""
    if not m.get("id"):
        return False               # 畸形记录（缺 id）不能进候选集——后续的 dict 推导都会 KeyError
    if m.get("layer") not in ("hippo", "cortex"):
        return False
    if m.get("pinned") or m.get("verbatim") or m.get("kind") == "identity":
        return False
    if m.get("review") == "pending":
        return False
    imp = _num(m.get("importance"), 0.5)
    age = now - _num(m.get("created"), now)
    R = retention(m, now)
    idle = now - _num(m.get("last_access"), _num(m.get("created"), now))
    if imp > ARCHIVE_MAX_IMPORTANCE or R >= ARCHIVE_RETENTION:
        return False
    if age < ARCHIVE_MIN_AGE_DAYS * DAY:
        return False
    if int(m.get("access_count", 0) or 0) > 0 and idle < ARCHIVE_IDLE_DAYS * DAY:
        return False
    return True


def archive_candidates(records: list, now: int | None = None) -> list:
    """选出符合"沉眠"条件的记忆：保留度极低 + 低重要 + 满月 + 长期没被想起。

    不碰 pinned / 封存原文 / 身份记忆 / 待审核项。返回记录本身（调用方决定是否落盘）。
    """
    now = now or now_ts()
    return [m for m in records if _is_archivable(m, now)]


def _append_archive(new_rows: list) -> None:
    """把记录并入 archive.jsonl，按 id 去重。
    先写 archive、后从热层删除（顺序上防丢）；若上一次在两步之间被打断，
    记录会同时留在热层和 archive，重跑时这里按 id 去重，不会越堆越多。"""
    if not new_rows:
        return
    # archive.jsonl 的读-改-写自身加锁：调用方（archive_run/dream）只持热层锁，
    # 没有互斥保护 archive；restore 那边是两把锁都持，不加这把会互相踩丢行。
    with _locked(HOME / "archive.jsonl"):
        existing = load_archive()
        have = {m.get("id") for m in existing if m.get("id")}
        add = [m for m in new_rows if m.get("id") not in have]
        if add:
            write_jsonl(HOME / "archive.jsonl", existing + add)


def archive_run(dry_run: bool = False) -> dict:
    """手动触发归档：把沉眠记忆雪藏进 archive 层（原文保真，永不删）。

    读-改-写整体放在写锁里（统一锚点 hippocampus.jsonl），与 remember/dream 互斥。
    """
    if READONLY and not dry_run:
        raise PermissionError("记忆库当前只读，无法归档。")
    now = now_ts()
    if dry_run:
        # 预览不写盘，也不抢写锁（只读环境下锁文件也可能建不了）
        cand = archive_candidates(load_all(), now)
        return {"count": len(cand),
                "archived": [{"id": m["id"], "gist": m.get("gist") or heuristic_gist(m.get("text", ""))}
                             for m in cand],
                "dry_run": True}
    with _locked(HOME / "hippocampus.jsonl"):
        records = load_all()
        cand = archive_candidates(records, now)
        arch = [{"id": m["id"], "gist": m.get("gist") or heuristic_gist(m.get("text", ""))}
                for m in cand]
        if not dry_run and cand:
            for m in cand:
                m["layer"] = ARCHIVE_LAYER
                m["archived_at"] = now
            keep_ids = {m["id"] for m in cand}
            # 归档层文件单独维护：只在本次真的新增了归档时才重写，绝不整库覆写清空
            _append_archive(cand)
            save_all([m for m in records if m.get("id") not in keep_ids])
    return {"count": len(arch), "archived": arch, "dry_run": dry_run}


def save_all(records: list) -> None:
    groups = {"hippo": [], "cortex": [], "dust": []}
    for r in records:
        layer = r.get("layer", "hippo")
        # 未知层名（损坏的手改数据）归入海马回，绝不静默丢弃
        # archive 记录走专用通道（load_archive 维护），不带进整库覆写，避免把归档文件清空
        if layer != ARCHIVE_LAYER:
            groups[layer if layer in groups else "hippo"].append(r)
    # 三文件尽量同步落盘：先把三个临时文件都写好，再依次 replace。
    # 把"存了一半"的窗口压到最小——只有进程恰好在两次 replace 之间挂掉才可能不一致，
    # 而不是像原来那样每写一个文件就有一个完整窗口。
    targets = [("hippocampus.jsonl", groups["hippo"]),
               ("cortex.jsonl", groups["cortex"]),
               ("dust.jsonl", groups["dust"])]
    staged = []
    try:
        for fname, rows in targets:
            p = HOME / fname
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = _tmp_path(p)
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            staged.append((tmp, p))
    except Exception:
        for tmp, _ in staged:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
    for tmp, p in staged:
        _replace_retry(tmp, p)


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

def _num(v, default: float = 0.0) -> float:
    """把可能是 None / 字符串 / 数字的值安全转成 float；转不了用默认值。
    记忆库里手改、旧版本、坏字段都可能让数值变成 null 或字符串，硬 float() 会整条崩。"""
    if v is None:
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def retention(m: dict, now: int | None = None) -> float:
    """艾宾浩斯遗忘曲线：R = exp(-Δt / S)"""
    now = now or now_ts()
    last = m.get("last_access")
    if last is None:
        last = m.get("created", now)
    dt = max(0.0, now - _num(last, now))
    stability = max(HOUR, _num(m.get("stability"), DEFAULT_STABILITY))
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
             tags=None, entities=None, source: str = "ai", layer: str = "hippo",
             emotion: str | None = None) -> tuple:
    ensure_home()
    text = (text or "").strip()
    if not text:
        raise ValueError("记忆内容为空")
    if emotion and emotion not in EMOTIONS:
        raise ValueError(f"没有这种情绪：{emotion}"
                         f"（可用：开心/生气/难过/焦虑/感动…共 {len(EMOTIONS)} 种，"
                         "`brain.py emotion list` 查看全部）")
    records = None  # 锁内读取：见下方 _locked 作用域
    new_terms = terms(text)
    imp = importance if importance is not None else heuristic_importance(text, kind)

    # 情绪增强编码（杏仁核标记）：情绪激动时经历的事记得更牢。
    # 显式给了 emotion 就用它；没给时，若此刻正好情绪激动（唤醒≥0.65），
    # 自动把当时最重的情绪标在这条记忆上——"发生在情绪上头那一刻的事"。
    emo_name, emo_arousal, boost = "", 0.0, 0.0
    if emotion:
        if emotion in EMOTIONS:
            emo_name, emo_arousal = emotion, _emotion_coord(emotion)[1]
    else:
        m = mood_now()
        if m.get("storm") and m["active"]:
            emo_name = m["active"][0]["emotion"]
            emo_arousal = float(m["active"][0].get("arousal", 0))
    if emo_name and emo_arousal >= 0.45:
        boost = 0.08 * emo_arousal
        imp = min(1.0, (imp if imp <= 1 else imp / 10) + boost)
        stability_boost = 1.0 + 0.9 * emo_arousal
    else:
        emo_name, stability_boost = "", 1.0

    # 读-改-写整体加锁：remember 走 save_all 整库快照覆写，若锁外先 load_all，
    # 并发写的另一个进程 os.replace 覆盖我们正读着的文件会在 Windows 上报 PermissionError。
    # 放进锁里后，读和写在锁内互斥，顺带杜绝"旧快照覆盖新写入"。
    with _locked(HOME / "hippocampus.jsonl"):
        records = load_all()
        # 近重复合并：同一类记忆、文本高度相似 -> 强化旧记忆而不是堆新条
        # 先做廉价子串预筛（jaccard≥0.86 必共享 term，共享 term 必是旧文本子串，数学上不丢召回），
        # 上万条记忆时避免对每条都跑正则分词；archive 记录不参与合并（防归档记忆被悄悄复活）
        for m in records:
            if m.get("layer") in ("dust", ARCHIVE_LAYER) or m.get("kind") != kind:
                continue
            if new_terms and not any(t in _hay(m) for t in new_terms):
                continue
            old_text = m.get("text", "") or ""
            if jaccard(new_terms, terms(old_text)) >= 0.86:
                # 保留更长/更完整的文本，别让一句简短的重复把原细节盖掉
                old, new = m.get("text", ""), text
                if len(new) > len(old):
                    m["text"] = new
                    m["gist"] = heuristic_gist(new)
                else:
                    m["gist"] = heuristic_gist(old)
                m["importance"] = round(max(0.0, min(1.0, max(_num(m.get("importance"), 0.5), float(imp) if imp <= 1 else imp / 10))), 3)
                m["last_access"] = now_ts()
                m["access_count"] = int(_num(m.get("access_count"), 0)) + 1
                m["stability"] = min(MAX_STABILITY, _num(m.get("stability"), DEFAULT_STABILITY) * 1.4 * stability_boost)
                # 习惯化（享乐适应）：同一件事反复出现，新鲜感递减、重要性不再步步抬升。
                # 就像警报器一直响就没人再在意——但保留一个下限，别把真重要的磨掉。
                m["repeat"] = int(_num(m.get("repeat"), 0)) + 1
                if m["repeat"] >= 3 and m["importance"] > 0.25:
                    m["importance"] = round(m["importance"] - 0.02, 3)
                if emo_name and not m.get("emotion"):
                    m["emotion"] = emo_name
                m["tags"] = _dedupe(m.get("tags", []) + [t.strip().lstrip("#") for t in (tags or [])])
                m["entities"] = _dedupe(m.get("entities", []) + (entities or []))
                save_all(records)
                _remember_desire_nudge(m, imp)
                return m, "merged"

        rec = make_record(text, kind, importance, tags, entities, source, layer)
        rec["importance"] = round(float(rec.get("importance", 0.5)) + (boost if emo_name else 0), 3)
        rec["importance"] = max(0.0, min(1.0, rec["importance"]))
        # 高重要性待审闸：重要性 ≥0.8（10 分制 ≥8）的记忆多半是约定/承诺/重要决定，
        # 一旦记错就是长期错误，与做梦产出一样先进待审池，用户确认后转正。
        if rec["importance"] >= 0.8 and source != "user":
            rec["review"] = "pending"
            rec["review_source"] = f"high-importance:{source}"
        rec["stability"] = min(MAX_STABILITY, rec.get("stability", DEFAULT_STABILITY) * stability_boost)
        if emo_name:
            rec["emotion"] = emo_name
            rec["emotional"] = round(emo_arousal, 2)
        # —— 记忆冲突裁决（Mem0 风格）——
        # 与某条旧记忆中度相似（jaccard 0.55~0.86）但文本不同 = 同一件事说法变了/事实更新。
        # 旧的不删：打上 superseded_by 指向新记忆（回忆默认跳过被取代者，仍可沿链回溯旧观念）。
        # AI 来源的新说法先标"待审核"：防止 AI 改写结论未经用户确认就生效。
        # 必须在锁内做：remember 走整库快照覆写，锁内读锁内写才不会互相覆盖。
        superseded = []
        for old in records:
            if (old is rec or old.get("kind") != kind
                    or old.get("layer") in ("dust", ARCHIVE_LAYER)
                    or old.get("superseded_by")):
                continue
            old_text = str(old.get("text", "")).strip()
            old_terms = terms(old_text)
            if not old_terms or old_text == text:
                continue
            sim = jaccard(new_terms, old_terms)
            if 0.55 <= sim < 0.86:
                old["superseded_by"] = rec["id"]
                old["updated"] = now_ts()
                rec.setdefault("links", [])
                if old["id"] not in rec["links"]:
                    rec["links"].append(old["id"])
                superseded.append(old["id"])
        if superseded:
            if source != "user":
                rec["review"] = "pending"
                rec["review_source"] = "conflict"
            rec["superseded"] = superseded   # 给调用方展示"这次取代了哪些旧说法"
        records.append(rec)
        save_all(records)
        _remember_desire_nudge(rec, rec.get("importance"))
        return rec, ("superseded" if superseded else "created")


def _remember_desire_nudge(rec: dict, imp) -> None:
    """记事牵动六欲（自动微调）：记东西本身就是表达，重要的事还带着求知/掌控。

    重要度高 → 表达欲/成就欲；探查类（question/insight）→ 求知欲。
    幅度 1~2 点，冷却节流，避免频繁对话反复写盘。
    """
    changes = {"表达欲": 1.0}
    kind = (rec.get("kind") or "")
    if kind in ("question", "insight", "curiosity"):
        changes["求知欲"] = 2.0
    if _num(imp, 0) >= 0.7:
        changes["成就欲"] = 1.5
    try:
        desire_nudge(changes)
    except Exception:
        pass          # 六欲牵动是锦上添花，绝不能让记事本身失败


def entity_profile(name: str, limit: int = 20) -> dict:
    """实体档案卡（Zep 风格）：围绕一个人物/事物把散落的记忆聚成一张画像。

    面板用：点开一个实体就能看到"什么时候第一次出现、最近一次出现、
    一起出现过的情绪、时间线、以及最重要几条记忆拼出的一句话画像"。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("实体名不能为空")
    low = name.lower()
    hits = []
    for m in load_all() + load_archive():
        ents = [str(e).lower() for e in (m.get("entities") or [])]
        hay = _hay(m)
        # 优先按 entities 精确匹配；退化到 text/tags 里包含该词也算（实体抽取没跑全的兜底）
        if low in ents or low in hay:
            hits.append(m)
    if not hits:
        return {"entity": name, "count": 0, "timeline": [], "emotions": {},
                "summary": "（还没有关于这个实体的记忆）"}
    createds = [int(_num(m.get("created"), 0)) for m in hits if _num(m.get("created"), 0)]
    emotions = {}
    for m in hits:
        e = str(m.get("emotion") or "").strip()
        if e:
            emotions[e] = emotions.get(e, 0) + 1
    ordered = sorted(hits, key=lambda m: _num(m.get("created"), 0), reverse=True)
    timeline = [(fmt_time(int(_num(m.get("created"), 0))), str(display_text(m))[:80])
                for m in ordered[:max(1, limit)]]
    # 一句话画像：重要性最高的 3 条记忆拼接——实体"最被强调"的侧面
    top = sorted(hits, key=_imp_key_safe, reverse=True)[:3]
    summary = "；".join(str(m.get("text") or "") for m in top)[:120]
    first = min(createds) if createds else 0
    last = max(createds) if createds else 0
    return {
        "entity": name,
        "first_seen": first, "first_seen_text": fmt_time(first),
        "last_seen": last, "last_seen_text": fmt_time(last),
        "count": len(hits),
        "importance_avg": round(sum(_imp_key_safe(m) for m in hits) / len(hits), 3),
        "emotions": emotions,
        "timeline": timeline,
        "summary": summary or "（这些记忆没有可用正文）",
    }


def _imp_key_safe(m: dict) -> float:
    """importance 安全取值：坏类型（None/list 等）沉底为 0，不让一条坏记忆炸掉画像。"""
    return _num(m.get("importance"), 0.0)


def recall(query: str, k: int = 8, layers: tuple = ("cortex", "hippo"),
           strengthen: bool = True, include_dust: bool = False,
           include_archive: bool = False) -> list:
    """模糊回忆：相关度 + 近期性 + 重要性 + 联想扩散。返回 [(record, score, parts), ...]"""
    ensure_home()
    cfg = load_config()
    w = cfg["weights"]
    records = [m for m in load_all() if m.get("id") and (m.get("layer") in layers or (include_dust and m.get("layer") == "dust"))]
    # 被取代的记忆默认跳过：观念已更新，回忆里只出现新说法；
    # 需要"翻旧账"时走 show 单条查，沿 superseded_by 链还能回溯到旧观念。
    records = [m for m in records if not m.get("superseded_by")]
    if include_archive:
        records += [m for m in load_archive() if m.get("id")]
    if not records:
        return []
    now = now_ts()
    q_terms = terms(query)
    # 第一道预筛：带查询词时先做廉价的子串包含测试，只对可能相关的记忆做正则分词与打分。
    # 上万条记忆时这一刀把"全量 terms + 全量 idf"砍成"候选集 terms"；数学上不丢召回
    # （relevance>0 ⇔ 共享 term，而共享 term 必是本文档文本的连续子串）。
    if q_terms:
        cand = []
        for m in records:
            h = _hay(m)
            if any(t in h for t in q_terms):
                cand.append(m)
        records = cand
        if not records:
            return []
    # 无查询词（startup 注入等场景）不构建 terms/idf：relevance 恒为 0，省掉上万条的整库分词
    all_terms = {m["id"]: terms(_hay(m)) for m in records} if q_terms else {}
    idf = build_idf(list(all_terms.values())) if q_terms else {}

    scored = []
    # 首因效应：同一片记忆里最早留下的印象占权更高（第一次的印象比后来补的更深）
    oldest = max((now - _num(m.get("created"), now) for m in records), default=0)
    for m in records:
        rel = relevance(q_terms, all_terms[m["id"]], idf) if q_terms else 0.0
        rec = retention(m, now)
        imp = _num(m.get("importance"), 0.5)
        score = w["relevance"] * rel + w["recency"] * rec + w["importance"] * imp
        # 首因：最早创建的那批（年龄接近最大）给一点额外权重
        age = now - _num(m.get("created"), now)
        if oldest > 0 and age >= oldest * 0.85:
            score += w.get("primacy", 0.03)
        # 标签/实体精确命中，显著加分
        hits = 0
        if query:
            q_low = query.lower()
            hits += sum(1 for t in m.get("tags", []) if str(t).lower() in q_low)
            hits += sum(1 for e in m.get("entities", []) if str(e).lower() in q_low)
        score += min(0.30, 0.12 * hits)
        scored.append([m, score, {"relevance": round(rel, 3), "recency": round(rec, 3),
                                  "importance": imp, "hits": hits}])

    # 情绪一致性提取（mood-congruent recall）：心情会偏向勾起同调性的记忆
    # ——难过时更容易想起难过的事，开心时更容易想起开心的事（Bower 1981）。
    mv = mood_now().get("valence", 0.0)
    if abs(mv) >= 0.2:
        for item in scored:
            e = item[0].get("emotion")
            if e and e in EMOTIONS:
                if _emotion_coord(e)[0] * mv > 0:
                    item[1] += 0.06

    # 有查询词时，只保留真正沾边的记忆（否则会污染注入包）
    if q_terms:
        scored = [x for x in scored if x[2]["relevance"] > 0.02 or x[2]["hits"] > 0]

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[: max(k, 1)]

    # 联想扩散：与已选记忆共享实体/标签的获得小幅加成（像海马回放带动关联）
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
    # 显式联想：记忆之间已有的关联边把邻居也带起来（"提到 A 就想起 B"）
    if not READONLY or ASSOC_FILE.exists():
        for r in load_assoc():
            if r["src"] in top_ids and r["dst"] not in top_ids:
                w = _num(r.get("weight"), 1)
                for item in scored[len(top):]:
                    if item[0]["id"] == r["dst"]:
                        item[1] += min(0.12, 0.04 * w)
            elif r["dst"] in top_ids and r["src"] not in top_ids:
                w = _num(r.get("weight"), 1)
                for item in scored[len(top):]:
                    if item[0]["id"] == r["src"]:
                        item[1] += min(0.12, 0.04 * w)

    scored.sort(key=lambda x: x[1], reverse=True)
    result = scored[: max(k, 1)]

    # 侧抑制去冗余（lateral inhibition）：近似重复的痕迹只留最强一条。
    # 大脑不会同时点亮两份"同一件事"，弱的那份被压下去。
    # 判重两种：字面高度重合，或一句是另一句的子串（同一件事的详略两版）。
    if len(result) > 1:
        kept, kept_terms, kept_texts = [], [], []
        for item in result:
            m = item[0]
            t = all_terms.get(m["id"]) or terms(m.get("text", ""))
            txt = (m.get("text") or "").strip()
            dup = any(jaccard(t, kt) >= 0.85 for kt in kept_terms)
            if not dup and txt:
                for ot in kept_texts:
                    a, b = (txt, ot) if len(txt) <= len(ot) else (ot, txt)
                    if a and a in b and len(a) >= 0.6 * len(b):
                        dup = True
                        break
            if dup:
                continue
            kept.append(item)
            kept_terms.append(t)
            kept_texts.append(txt)
        result = kept

    if strengthen and not READONLY:
        touched = [m for m, _, _ in result]
        for m in touched:
            ac = int(_num(m.get("access_count"), 0))
            first = ac == 0
            m["access_count"] = ac + 1
            # 首次提取巩固更强：头一回想起来的事，印记打得比反复想起更深
            # （首因效应 + 首次测试效应：第一次回忆本身就是一次强巩固）
            mult = 2.6 if first else 1.8
            m["stability"] = min(MAX_STABILITY, _num(m.get("stability"), DEFAULT_STABILITY) * mult + 4 * HOUR)
            m["last_access"] = now
            m["detail"] = min(1.0, _num(m.get("detail"), 1.0) * 0.9 + 0.1)
        # 只写回被"想起"的这条，别把没加载进来的层（如 dust）覆盖清空；没想起就不动盘
        # 定向写回：只重写命中记忆所在层，避免整库覆写（上万条时省掉大头 I/O）
        if touched:
            _strengthen_write(touched)

    # —— 回忆牵动六欲：真想起点什么了，求知欲得到一点满足（自动微调，冷却节流）——
    # 只在有查询词且确实命中时触发，startup 注入（无 query）不算"回忆"。
    if q_terms and result and not READONLY:
        try:
            desire_nudge({"求知欲": 1.5})
        except Exception:
            pass          # 锦上添花，失败不影响回忆本身
    return [(m, round(s, 4), parts) for m, s, parts in result]


def recent(k: int = 6, layers: tuple = ("hippo", "cortex")) -> list:
    records = [m for m in load_all() if m.get("layer") in layers]
    records.sort(key=lambda m: _num(m.get("created"), 0), reverse=True)
    return records[:k]


def forget(memory_id: str) -> bool:
    # 读-改-写整体加锁：forget 用的是 save_all 整库快照覆写，
    # 不加锁会把锁外并发写入的新记忆一起丢回旧状态。
    with _locked(HOME / "hippocampus.jsonl"):
        records = load_all()
        for m in records:
            if m.get("id") == memory_id and m.get("layer") != "dust":
                m["layer"] = "dust"
                m["forgotten_at"] = now_ts()
                m["detail"] = round(max(0.15, _num(m.get("detail"), 1.0) * 0.5), 2)
                save_all(records)
                return True
    return False


def display_text(m: dict) -> str:
    """细节度低时用模糊化的要点代替原文。"""
    detail = _num(m.get("detail"), 1.0)
    if detail < 0.5 and m.get("gist"):
        return f"{m['gist']}（细节已模糊）"
    return m.get("text", "")

def _usage_terms(text: str) -> set:
    return {t for t in terms(text) if t not in _USAGE_STOP}


def record_retrieval(query: str, mems, channel: str = "recall") -> None:
    """记下本轮找回/注入的记忆，供 usage 核对是否真的出现在回复里。

    只存 id + 文本摘要，不存整库；失败静默，绝不影响回忆本身。
    """
    if READONLY:
        return
    items = []
    for x in mems or []:
        m = x[0] if isinstance(x, (tuple, list)) and x else x
        if not isinstance(m, dict) or not m.get("id"):
            continue
        items.append({"id": m["id"], "layer": m.get("layer"),
                      "text": display_text(m)[:240]})
    if not items:
        return
    try:
        atomic_json(RETRIEVAL_FILE, {"ts": now_ts(), "query": query or "",
                                     "channel": channel, "items": items})
    except Exception as e:
        log_event("usage", f"回忆快照写入失败：{e}", level="warn")


def evaluate_retrieval_usage(reply: str) -> dict:
    """粗判上一轮找回的记忆有没有出现在回复里（关键词重叠）。

    不是判定"模型有没有抄记忆"，只是回答"这条记忆对这次输出有没有贡献"。
    核对过一次就作废，避免同一批记忆被重复计数。
    """
    snap = _slurp_json(RETRIEVAL_FILE, {}) or {}
    if not isinstance(snap, dict) or not snap.get("items"):
        return {"error": "没有待核对的回忆记录：先 recall 或 startup，再 usage --reply"}
    if snap.get("evaluated_at"):
        return {"error": "上一批回忆已经核对过了；先重新 recall，再 usage"}
    reply_terms = _usage_terms(reply)
    if not reply_terms:
        return {"error": "回复文本没有可比较的关键词（太短或全是虚词）"}
    by_id = {m.get("id"): m for m in load_all() if m.get("id")}
    now = now_ts()
    touched, rows, used_ids = [], [], []
    for it in snap["items"]:
        mid = it.get("id")
        mt = _usage_terms(it.get("text", ""))
        shared = mt & reply_terms
        ratio = round(len(shared) / max(1, len(mt)), 3)
        used = len(shared) >= USAGE_MIN_SHARED and ratio >= USAGE_MIN_RATIO
        # 短记忆只共享一个词也可能是真用上了（如"只让叫阿哲"）
        if not used and len(shared) == 1 and len(mt) <= 12:
            used = True
        rows.append({"id": mid, "used": used, "shared": len(shared), "ratio": ratio,
                     "hits": sorted(shared)[:8]})
        m = by_id.get(mid)
        if used and m:
            m["used_count"] = int(_num(m.get("used_count"), 0)) + 1
            m["used_at"] = now
            m["used_hits"] = sorted(shared)[:8]
            touched.append(m)
            used_ids.append(mid)
    if touched:
        _strengthen_write(touched)
    if not READONLY:
        try:
            with _locked(USAGE_FILE):
                hist = read_jsonl(USAGE_FILE)
                hist.append({"ts": now, "query": snap.get("query", ""),
                             "channel": snap.get("channel", ""),
                             "recalled": [r["id"] for r in rows],
                             "used": used_ids, "items": rows})
                write_jsonl(USAGE_FILE, hist[-1000:])
            atomic_json(RETRIEVAL_FILE, dict(snap, evaluated_at=now))
        except Exception as e:
            log_event("usage", f"使用反馈写入失败：{e}", level="warn")
    recalled = [it.get("id") for it in snap["items"]]
    return {"recalled": len(recalled), "used": len(used_ids),
            "rate": round(len(used_ids) / len(recalled), 3) if recalled else 0.0,
            "query": snap.get("query", ""), "channel": snap.get("channel", ""),
            "items": rows}


def usage_metrics() -> dict:
    """历史回忆使用率（只算已经核对过的批次）。"""
    rows = read_jsonl(USAGE_FILE)
    recalled = sum(len(r.get("recalled") or []) for r in rows)
    used = sum(len(r.get("used") or []) for r in rows)
    return {"events": len(rows), "recalled": recalled, "used": used,
            "rate": round(used / recalled, 3) if recalled else 0.0}


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
            "open_count": sum(1 for r in rows if r.get("open", True)),
            "ttl_hours": int(WORKING_TTL / HOUR), "items": rows}


def work_add(text: str, tag: str = "", why: str = "") -> list:
    """把一条工作记忆显式落盘为会话暂存。这是 AI 自己的决定，不是自动行为。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("工作记忆内容为空")
    ensure_home()
    with _locked(WORKING_FILE):
        rows = load_working()
        rows.append({"ts": now_ts(), "text": text,
                     "tag": (tag or "").strip(), "why": (why or "").strip(),
                     "open": True})
        rows = rows[-WORKING_CAPACITY:]
        write_jsonl(WORKING_FILE, rows)
    return rows


def work_done(idx: int) -> list:
    """把暂存里第 idx 条（从 1 起）标记为已完成。

    蔡格尼克效应：没做完的事会在脑子里反复冒出来；一旦做完，
    动机满足了，它就迅速退场——"完成"本身就是一种释放。
    """
    with _locked(WORKING_FILE):
        rows = load_working()
        if 1 <= idx <= len(rows):
            rows[idx - 1]["open"] = False
            write_jsonl(WORKING_FILE, rows)
    return rows


def work_clear() -> None:
    with _locked(WORKING_FILE):
        if WORKING_FILE.exists():
            write_jsonl(WORKING_FILE, [])


def work_flush(why: str = "会话结束前决定留下", skip_done: bool = False) -> dict:
    """把暂存里还值得留的条目一次性转成真正的记忆（海马体），然后清空暂存。

    skip_done=True 时，已经 `work done` 标记完成的条目直接丢弃、不落盘——
    做完的事就该放下，不必再占长期记忆。返回 {"saved": [...], "dropped": n}。
    """
    # 读-转-清 一次加锁完成：不加锁的话，两个终端同时 flush / add，
    # 后 flush 的会拿旧快照把并发新增的暂存条目一起清掉（静默丢失）。
    with _locked(WORKING_FILE):
        rows = load_working()
        saved, dropped = [], 0
        for r in rows:
            if skip_done and not r.get("open", True):
                dropped += 1
                continue
            tags = ["工作记忆"] + ([r["tag"]] if r.get("tag") else [])
            rec, how = remember(r["text"], kind="episode", tags=tags, source="ai")
            saved.append({"id": rec["id"], "text": rec["text"], "how": how})
        # 已持锁：直接清空，不能再调 work_clear()（它会重复抢同一把锁→自锁超时）
        write_jsonl(WORKING_FILE, [])
    if saved:
        try:
            import growth
            growth.log_evolution("self", "把工作记忆转成长期记忆",
                                 f"{why}；共 {len(saved)} 条", [s["id"] for s in saved])
        except Exception:
            pass
    return {"saved": saved, "dropped": dropped}


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

    # 2) 逐段进海马体（--raw-only 时跳过：只留原文快照，不污染记忆库）
    if raw_only:
        return {"saved": [], "skipped": 0, "archive": archive, "chars": len(payload)}
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


# ---------------------------------------------------------------- 记忆关联（联想能力）
# 人有联想：一件事会牵出另一件。记忆之间除了"文本像不像"，还有显式的关联边——
# "A 导致了 B"、"A 和 B 是同一件事的两个侧面"、"提到 A 就会想起 B"。
# recall 时，命中的记忆会沿关联边把邻居也带起来（联想扩散的显式版）。

def load_assoc() -> list:
    raw = read_jsonl(ASSOC_FILE)
    good, bad = [], 0
    for r in raw:
        if isinstance(r, dict) and r.get("src") and r.get("dst"):
            good.append(r)
        else:
            bad += 1
    if bad:
        log_event("load", f"load_assoc 跳过 {bad} 条畸形边（缺 src/dst 或类型错）", level="warning")
    return good


def save_assoc(rows: list) -> None:
    write_jsonl(ASSOC_FILE, rows)


def _norm_edge(a: str, b: str) -> tuple:
    return (a, b) if (a or "") < (b or "") else (b, a)


def link_memory(src: str, dst: str, relation: str = "相关", weight: float = 1.0,
                why: str = "") -> dict:
    """把两条记忆连起来（无向边）。同一对记忆反复连会加重。"""
    ensure_home()
    if READONLY:
        raise PermissionError("记忆库当前只读，无法修改关联。")
    if src == dst:
        raise ValueError("不能自己关联自己。")
    if not src or not dst:
        raise ValueError("关联的双方 id 不能为空。")
    a, b = _norm_edge(str(src), str(dst))
    with _locked(ASSOC_FILE):
        ids = {m["id"] for m in load_all() if m.get("id")}
        for mid in (src, dst):
            if mid not in ids:
                raise KeyError(f"没有这条记忆：{mid}（用 list 或 recall 拿 id）")
        rows = load_assoc()
        for r in rows:
            if r.get("src") == a and r.get("dst") == b:
                r["weight"] = round(min(3.0, _num(r.get("weight"), 1) + _num(weight, 1)), 2)
                r["relation"] = relation or r.get("relation", "相关")
                r["ts"] = now_ts()
                if why:
                    r["why"] = why
                save_assoc(rows)
                return {"src": src, "dst": dst, "relation": relation, "weight": r["weight"],
                        "created": False, "updated": True}
        rows.append({"src": a, "dst": b, "relation": (relation or "相关").strip(),
                     "weight": round(max(0.1, _num(weight, 1)), 2),
                     "why": (why or "").strip(), "ts": now_ts()})
        save_assoc(rows)
    return {"src": src, "dst": dst, "relation": relation, "weight": _num(weight, 1),
            "created": True, "updated": False}


def unlink_memory(src: str, dst: str) -> bool:
    a, b = _norm_edge(str(src or ""), str(dst or ""))
    with _locked(ASSOC_FILE):
        rows = load_assoc()
        keep = [r for r in rows if not (r.get("src") == a and r.get("dst") == b)]
        if len(keep) == len(rows):
            return False
        save_assoc(keep)
        return True


def assoc_of(mid: str) -> list:
    """一条记忆的邻居（带边信息）。"""
    if not mid:
        return []
    out = []
    for r in load_assoc():
        # assoc.jsonl 任一行缺 src/dst 就跳过——否则崩
        src, dst = r.get("src"), r.get("dst")
        if src == mid:
            out.append({"id": dst, "relation": r.get("relation", "相关"),
                        "weight": _num(r.get("weight"), 1),
                        "direction": "dst", "why": r.get("why", "")})
        elif dst == mid:
            out.append({"id": src, "relation": r.get("relation", "相关"),
                        "weight": _num(r.get("weight"), 1),
                        "direction": "src", "why": r.get("why", "")})
    by_id = {m["id"]: m for m in load_all() if m.get("id")}
    for n in out:
        n["gist"] = by_id.get(n["id"], {}).get("gist", "") if n["id"] else ""
        n["kind"] = by_id.get(n["id"], {}).get("kind", "") if n["id"] else ""
    out.sort(key=lambda x: -_num(x.get("weight"), 0))
    return out


def assoc_auto(weight: float = 0.8, dry_run: bool = False) -> dict:
    """自动联想：共享实体或标签的记忆之间补边（这是"触类旁通"的机器版）。"""
    records = [m for m in load_all() if m.get("id") and m.get("layer") != "dust"]
    by_ent = {}
    by_tag = {}
    for m in records:
        for e in m.get("entities", []):
            by_ent.setdefault(e, []).append(m["id"])
        for t in m.get("tags", []):
            by_tag.setdefault(t, []).append(m["id"])
    planned = []
    seen = set()
    for bucket in list(by_ent.values()) + list(by_tag.values()):
        for i, a in enumerate(bucket):
            for b in bucket[i + 1:]:
                key, x = _norm_edge(a, b)
                if key in seen:
                    continue
                seen.add(key)
                planned.append((a, b))
    existing = {(r["src"], r["dst"]) for r in load_assoc()}
    added = []
    for a, b in planned:
        if (a, b) in existing:
            continue
        if dry_run:
            added.append({"src": a, "dst": b})
            continue
        link_memory(a, b, relation="相关", weight=weight, why="共享实体/标签的自动联想")
        added.append({"src": a, "dst": b})
    return {"planned": len(planned), "added": len(added),
            "dry_run": dry_run,
            "pairs": added[:20]}


def assoc_graph(limit: int = 300) -> dict:
    """联想图谱数据：面板 3D 空间用的节点与关联边。

    节点上限 limit（默认 300，太多画不动）；边只保留两端都在图里的。
    """
    records = [m for m in load_all() if m.get("layer") != "dust"]
    records.sort(key=lambda m: (_num(m.get("importance"), 0.5),
                                float(retention(m))), reverse=True)
    nodes = records[:limit]
    ids = {n["id"] for n in nodes}
    edges = []
    for r in load_assoc():
        if r["src"] in ids and r["dst"] in ids:
            edges.append({"src": r["src"], "dst": r["dst"],
                          "relation": r.get("relation", "相关"),
                          "weight": float(r.get("weight", 1))})
    return {
        "nodes": [{"id": m["id"], "gist": m.get("gist") or heuristic_gist(m.get("text", "")),
                   "kind": m.get("kind", ""), "layer": m.get("layer", "hippo"),
                   "importance": _num(m.get("importance"), 0.5),
                   "emotion": m.get("emotion", ""),
                   "recency": round(retention(m), 2)} for m in nodes],
        "edges": edges,
        "connected": len({e["src"] for e in edges} | {e["dst"] for e in edges}),
    }

# ---------------------------------------------------------------- 上下文注入

def read_user_rules() -> list:
    """读取用户附加规则：数据目录 lingshu.user.md。
    写在数据目录里，更新技能永远不碰它；每会话注入自动带上。"""
    p = HOME / "lingshu.user.md"
    if not p.exists():
        return []
    try:
        lines = [l.rstrip() for l in p.read_text(encoding="utf-8-sig").splitlines()]
    except OSError:
        return []
    return [l for l in lines if l.strip()]


def build_injection(query: str = "", budget_tokens: int | None = None) -> str:
    ensure_home()
    cfg = load_config()
    # budget 0/负数按 1 兜底：`budget_tokens or ...` 会把 0 静默吞掉，负数则算出负 char_budget
    budget = budget_tokens if isinstance(budget_tokens, int) and budget_tokens > 0 else _cfg_num(cfg, "budget_tokens", 1400, int)
    char_budget = int(budget * 1.8)
    p = load_persona()
    session_mode = resolve_mode(query)

    out = ["===== 长期记忆注入（灵枢）=====",
           "算力给我思考，人格给我灵魂，记忆给我生命。",
           "## 我是谁", persona_brief(p, mood=(session_mode != "work"))]
    used = len(persona_brief(p, mood=(session_mode != "work"))) + 60

    if model_tier() == "small":
        out.append("\n## 小模型模式（当前模型档位 = small）")
        out.append("你的上下文和推理都有限，下面这些高风险自动能力**不要主动做**：")
        out.append("- 不要自动 `dream --apply` 合并记忆、不要自动 `knowledge tidy` 提炼知识库——"
                   "这两件事容易把幻觉固化落盘。要做也行，但产出必须 `knowledge review` 人工过目。")
        out.append("- `emotion evaluate` 自动判读情绪在小模型上很不可靠，改用最朴素的 `emotion feel` 手动记。")
        out.append("- 只保留硬约束：会话开始 `startup`、值得记的 `remember`、被问起 `recall`、")
        out.append("  快满了 `check-context` / `capture` 保命。其余的能省则省。")

    if session_mode == "work":
        # 工程模式：情绪隔离。刚聊完情绪化话题立刻写代码时，旧情绪不许进工作会话。
        out.append("\n## 工程模式（情绪隔离，mode=work）")
        out.append("本次是写代码 / 排错 / 项目 / 数据分析这类工程场景，而且可能刚聊过情绪化话题。"
                   "规则：")
        out.append("- 情绪状态与拟人语气**不注入本会话**，别看/别背情绪；先给可执行方案，重视正确性与工程契约。")
        out.append("- 旧情绪的余波（不管刚才多上头）与当前任务无关，当作没有，理性为主。")
        out.append("- 若已手工打开过情绪闸需要先冷却，用 `emotion cool`。")
    else:
        # 拟人模式：内部状态 → 可执行的行为指引（而不是甩裸数值给模型）
        out.append("\n## 我现在的状态（行为指引）")
        for line in _behavior_map():
            out.append(f"- {line}")
        tone = mood_tone_hint()
        if tone:
            out.append(f"- 语气底色（自然流露即可，不表演）：{tone}")

    work = load_working()
    out.append("\n## 工作记忆（就是你现在能看见的对话）")
    out.append("它不进磁盘，也不会被回忆或做梦碰到。只有你自己判断值得留一手时，才显式写下来："
               "这次要一直盯着的 → `work add`（会话暂存，最多 9 条，12 小时后自动丢）；"
               "值得长期留下的 → 直接 `remember`。")
    if work:
        opens = [w for w in work if w.get("open", True)]
        dones = [w for w in work if not w.get("open", True)]
        if opens:
            out.append(f"心里还惦记着 {len(opens)} 件没做完的事"
                       "（未完成的更占心思，别让它一直悬着）：")
            # 序号与 `work` / `work done <序号>` 保持一致（含已完成项的全局序号）
            for i, w in enumerate(work, 1):
                if not w.get("open", True):
                    continue
                tag = f"[{w['tag']}] " if w.get("tag") else ""
                out.append(f"- {i}. {tag}{w['text']}")
        if dones:
            out.append(f"另有 {len(dones)} 件已完成（做完了就该放下，`work flush` 转成记忆或 `work clear` 清掉）。")
        out.append("做完一件就 `work done <序号>` 标记——完成会释放它占的心思。"
                   "`work flush` 转成真正的记忆（`--skip-done` 只转没完成的，已做完的直接丢），"
                   "或 `work clear` 丢掉。")

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
    # 与 mind_state() 同一套兜底：traits 已有数值而 drives 还空时，别在注入里
    # 一边说「性格：好奇85」一边说「内驱：还没有数值」——自相矛盾每轮都被读。
    if dr.get("curiosity") is None and dr.get("reason") is None:
        _traits = p.get("traits") or {}
        if _traits.get("好奇") is not None or _traits.get("理智") is not None:
            dr = {"curiosity": _traits.get("好奇"), "reason": _traits.get("理智")}
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
        new_mem: list = []
    else:
        # 先切出「上次 startup 之后」新写入的记忆 → 专属「刚刚发生」段，
        # 并从「最近的记忆」里剔除，两段互斥不重复。
        # >= 而非 >：同秒写入（startup 落盘和 remember 同一秒很常见）也要算进来。
        _last = (load_config().get("last_startup") or {}).get("ts") or 0
        new_mem = [m for m in recent(20) if _num(m.get("created"), 0) >= _last][:4]
        _new_ids = {m.get("id") for m in new_mem}
        mems = [(m, 0, {}) for m in recent(6 + len(new_mem)) if m.get("id") not in _new_ids][:6]
        header = "\n## 最近的记忆"
    lines, kept_mems = [], []
    for m, score, _ in mems:
        line = (f"- [{fmt_date(m.get('created', 0))} "
                f"{'★' * max(1, round(_num(m.get('importance'), 0.5) * 5))}] {display_text(m)}")
        # append 之后累计再判：append 前判会把"当前这行自身"的长度漏算，实际可超预算一行
        lines.append(line)
        if used + lines_join_len(lines) > char_budget * 0.75:
            lines.pop()   # 超预算：这行不放，留一条整记忆不切半
            break
        kept_mems.append(m)
    if lines:
        out.append(header)
        out.extend(lines)
        used += len("\n".join(lines))
        record_retrieval(query, kept_mems,
                         channel="inject" if query else "startup")

    # 「刚刚发生」：上面已按 id 切分（new_mem），这里直接渲染即可。
    if not query and new_mem:
        out.append("\n## 刚刚发生（本会话新增）")
        for m in new_mem:
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
    # 每日例行 + 自我总结提醒（保证"每天更新、定期自省"）
    st = _daily_state()
    if st.get("last_date") != fmt_date(now_ts()):
        out.append("[每日例行] 今天还没跑 daily：`python scripts/brain.py daily`"
                   "（写今日日记、沉淀知识、过期的梦自动做掉）。")
    else:
        out.append(f"[每日例行] 今天的日记已写（{st.get('last_date')}）。")
    cfg2 = load_config()
    last_review = float(cfg2.get("last_self_review", 0) or 0)
    if now_ts() - last_review > 2 * DAY:
        out.append("[自我总结] 超过 2 天没总结自己的灵枢技能了："
                   "`python scripts/brain.py skill self-review`。")
    ra = rationality()
    if session_mode != "work" and (ra["storm"] or ra["suppress"] >= 10):
        out.append(f"[理智状态] 情绪在起作用：{ra['mood_label']}，理智 {ra['base']} → "
                   f"{ra['effective']}（被压住 {ra['suppress']}%）。语气自然流露可以，"
                   "涉及钱/关系/承诺等大事，把决定权交给用户。")
    cfg = load_config()
    rv = f"{_cfg_num(cfg, 'review_threshold', 0.60) * 100:.0f}%"
    dr = f"{_cfg_num(cfg, 'dream_threshold', 0.75) * 100:.0f}%"
    es = f"{_cfg_num(cfg, 'escape_threshold', 0.90) * 100:.0f}%"
    out.append(f"[水位守则] 会话中即记即存；{rv} 补记清点，{dr} 做梦压缩，{es} 封存保底。"
               "用 python scripts/brain.py check-context --used <当前百分比> 逐档检查。")
    # 待审核提醒：AI 整理出来的东西不能当事实直接沉淀
    try:
        rc = review_counts()
        if rc["memories"] or rc["knowledge"] or rc["persona"]:
            parts = []
            if rc["memories"]:
                parts.append(f"{rc['memories']} 条 AI 整理的记忆")
            if rc["knowledge"]:
                parts.append(f"{rc['knowledge']} 条待审核知识")
            if rc["persona"]:
                parts.append(f"AI 改了人格 {rc['persona']} 次")
            out.append(f"[待审核] 有{'、'.join(parts)}。"
                       "先 `review` 过一遍：确认的 `review confirm <id>`，编造的 `review discard <id>`，"
                       "人格来源用 `persona history` 查。别让幻觉沉淀成事实。")
    except Exception:
        pass
    # 人格漂移告警：微小偏移也要让用户看得见，不能等跑偏了才发现
    try:
        dr = persona_drift(7)
        if dr["alert"]:
            out.append("[人格漂移] 与最近基准快照比有明显变化"
                       f"（结构 {dr['counts']['diff']} 处、AI 改动 {dr['counts']['ai_changes']} 次）。"
                       "用 `persona drift` 看细节；不是你要的，`persona restore <快照>` 或 `persona rollback <序号>`。")
    except Exception:
        pass
    # 用户附加规则：写在数据目录 lingshu.user.md（更新技能不碰数据目录，永不覆盖），每会话自动带上
    user_lines = read_user_rules()
    if user_lines:
        out.append("\n## 用户附加规则（来自 ~/.brain-memory/lingshu.user.md，更新技能永不覆盖）")
        out.extend(user_lines)
    # 扩展模块：AI 自己写的可执行钩子，inject(ctx) 返回的文本并入注入包（数据目录，更新永不覆盖）
    try:
        import growth
        exts = growth.ext_injections({"query": query, "mode": session_mode})
        if exts:
            out.append("\n## 扩展模块（~/.brain-memory/extensions/，更新技能永不覆盖）")
            for ext_name, ext_text in exts:
                out.append(f"[{ext_name}] {ext_text}")
    except Exception:
        pass
    # 技能库：告诉 AI 哪里有现成技能、怎么直接用（跨宿主共享，不用先安装）
    try:
        import growth
        avail = [r for r in growth.skill_roots() if r.get("exists") and r.get("count")]
        if avail:
            out.append("\n## 技能库（跨宿主，可直接用）")
            out.append("这些目录里有现成技能，都是本地文件，读出来照做即可，不必先安装：")
            for r in avail[:8]:
                out.append(f"- [{r['label']}] {r['path']}（{r['count']} 个）")
            out.append("列全部：`python scripts/brain.py skill scan`；照做某个：`skill use <名字>`"
                       "（`--root <宿主名或目录>` 限定来源）；变成自己的版本：`skill borrow <名字>`。")
    except Exception:
        pass
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
    # "存得好不好"两个便宜指标：历史回忆的使用率，以及从没被命中过的记忆占比
    um = usage_metrics()
    n_active = len(active) or 1
    never_recalled = sum(1 for m in active if int(_num(m.get("access_count"), 0)) == 0)
    never_used = sum(1 for m in active if int(_num(m.get("used_count"), 0)) == 0)
    old_never = sum(1 for m in active
                    if int(_num(m.get("access_count"), 0)) == 0
                    and not m.get("pinned")
                    and now - _num(m.get("created"), now) >= ARCHIVE_MIN_AGE_DAYS * DAY)
    return {
        "total": len(records),
        "hippo": sum(1 for m in records if m.get("layer") == "hippo"),
        "cortex": sum(1 for m in records if m.get("layer") == "cortex"),
        "dust": sum(1 for m in records if m.get("layer") == "dust"),
        "archive": len(load_archive()),
        "pinned": sum(1 for m in active if m.get("pinned")),
        "avg_retention": round(sum(decays) / len(decays), 3),
        "top_tags": sorted(tags.items(), key=lambda x: -x[1])[:10],
        "persona_name": p.get("identity", {}).get("名字", ""),
        # 旧实现读 persona.state.mood（通常没写过，永远是空）；心情是算出来的，不是存出来的
        "mood": mood_brief() or mood_now()["label"],
        "working": len(load_working()),
        "assoc": len(load_assoc()),
        "last_dream": load_dream_state().get("last_dream", 0),
        "daily": _daily_state().get("last_date", ""),
        "rationality": rationality(),
        "recall_usage": um,
        "never_recalled": never_recalled,
        "never_recalled_pct": round(never_recalled / n_active, 3),
        "never_used_pct": round(never_used / n_active, 3),
        "archive_hint": old_never,
        "home": str(HOME),
    }


def list_memories(limit: int = 30, layer: str | None = None) -> list:
    records = load_all()
    if layer:
        records = [m for m in records if m.get("layer") == layer]
    records.sort(key=lambda m: _num(m.get("created"), 0), reverse=True)
    return records[:limit]
