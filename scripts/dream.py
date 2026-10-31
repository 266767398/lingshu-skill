#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 灵枢 · 做梦：聚类巩固、梦境应用与报告。"""

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

# ---------------------------------------------------------------- 做梦 / 巩固

# 聚类池上限：记忆多了之后 O(n²) 两两比对会拖垮每晚的巩固，
# 超过就按重要度取前 N 条，剩下的留到下轮（反正 consolidate 是按批推进的）。
_MAX_CLUSTER_POOL = 400


def cluster_memories(records: list, max_clusters: int = 6, include_ids: set | None = None) -> list:
    include_ids = include_ids or set()
    # 无 id 的畸形记录没法做并查集/parent 映射，直接跳过。
    # 这些记录会原样保留在库，不会被 save_all 丢掉。
    pool = [m for m in records if m.get("id") and m.get("layer") != "dust"
            and (not m.get("consolidated") or m["id"] in include_ids)]
    if not pool:
        return []
    if len(pool) > _MAX_CLUSTER_POOL:
        pool = sorted(pool, key=lambda m: _num(m.get("importance"), 0), reverse=True)[:_MAX_CLUSTER_POOL]
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
        if len(members) < 2 and max(_num(m.get("importance")) for m in members) < 0.7:
            continue
        clusters.append(members)
    clusters.sort(key=lambda g: (len(g) * max(_num(m.get("importance")) for m in g)), reverse=True)

    out = []
    now = now_ts()
    for g in clusters[:max_clusters]:
        out.append({
            "ids": [m["id"] for m in g],
            "texts": [{"id": m["id"], "kind": m.get("kind", "fact"),
                       "text": m.get("text", ""), "importance": m.get("importance", 0.5),
                       "age_days": round((now - _num(m.get("created"), now)) / DAY, 1)} for m in g],
            "tags": _dedupe([t for m in g for t in m.get("tags", [])]),
            "entities": _dedupe([e for m in g for e in m.get("entities", [])]),
        })
    return out


def dream(trigger: str = "manual", dry_run: bool = False) -> dict:
    ensure_home()
    now = now_ts()
    report = {"trigger": trigger, "at": now, "forgotten": [], "promoted": [], "faded": 0,
              "archived": [], "clusters": []}

    # 读-改-写整体加锁：dream 用 save_all 整库快照覆写，
    # 不加锁会把锁外并发写入的新记忆用旧快照整库覆盖丢。
    # dry_run 纯预览不写盘，就别去抢锁——只读记忆库（HOME 不可写）下抢锁会直接报错。
    lock_ctx = (nullcontext() if dry_run else _locked(HOME / "hippocampus.jsonl"))
    with lock_ctx:
        records = load_all()
        archived_recs = []

        for m in records:
            if not m.get("id"):          # 缺 id 的畸形记录不参与巩固/遗忘（但仍原样保留在库）
                continue
            if m.get("layer") == "dust" or m.get("pinned"):
                continue
            text = m.get("text", "") or ""
            # 封存的原文不做细节淡化、不进淡忘区：它的价值就是"一个字都没丢"
            verbatim = bool(m.get("verbatim"))
            R = retention(m, now)
            # 冷热归档：久到想不起的沉眠记忆优先雪藏（原文保真、永不删、可 archive restore 回热），
            # 而不是直接模糊淡忘。放在淡忘判断之前，避免低重要沉眠记忆先被 dust 吃掉。
            if not verbatim and _is_archivable(m, now):
                m["layer"] = ARCHIVE_LAYER
                m["archived_at"] = now
                archived_recs.append(m)
                report["archived"].append({"id": m["id"], "gist": m.get("gist") or heuristic_gist(text)})
                continue
            if (not verbatim and m.get("layer") == "hippo" and R < RETENTION_FLOOR
                    and _num(m.get("importance"), 0.5) < 0.6 and now - _num(m.get("created"), now) > 5 * DAY):
                # 淡忘判据曾经是"2 天 + 重要度<0.75"：做梦触发得勤了之后（daily 自动
                # 做梦、面板一键做梦），同一批记忆第一次被巩固处理就会大片进淡忘区，
                # 看起来像"新版算得严了"。放宽到"满 5 天 + 重要度<0.6"——普通闲聊
                # 碎片多留几天被想起的机会，真沉眠的才淡忘；想抓回来的仍有轮廓在 dust。
                m["layer"] = "dust"
                m["forgotten_at"] = now
                m["detail"] = round(max(0.12, _num(m.get("detail"), 1) * 0.4), 2)
                report["forgotten"].append({"id": m["id"], "gist": m.get("gist") or heuristic_gist(text)})
                continue
            if (m.get("layer") == "hippo" and R >= 0.45
                    and (_num(m.get("access_count"), 0) >= 2 or _num(m.get("importance"), 0) >= 0.78)):
                m["layer"] = "cortex"
                m["consolidated"] = True
                report["promoted"].append({"id": m["id"], "gist": m.get("gist") or heuristic_gist(text)})
            if verbatim:
                m["detail"] = 1.0  # 封存件永不模糊
            else:
                target = round(0.35 + 0.65 * R, 2)
                if _num(m.get("detail"), 1) > target:
                    m["detail"] = target
                    report["faded"] += 1
            if not m.get("gist"):
                m["gist"] = heuristic_gist(text)

        # 冷热归档：长期没被想起、保留度极低的沉眠记忆雪藏进 archive 层
        # （原文保真、永不删，只是移出 recall 的热遍历集合）。顺带归档几乎零额外成本。
        if archived_recs:
            records = [m for m in records if m.get("layer") != ARCHIVE_LAYER]
            if not dry_run:
                _append_archive(archived_recs)

        clusters = cluster_memories(records, include_ids={x["id"] for x in report["promoted"]})
        if not dry_run:
            save_all(records)

    report["clusters"] = clusters
    task_id = f"dream_{now}"
    report["task_id"] = task_id

    if not dry_run:
        if clusters:
            pending = {
                "task_id": task_id,
                "created": now,
                "trigger": trigger,
                "instructions": "把 clusters 里的碎片压缩成更抽象、更持久的长期记忆；提取人格变化；输出 apply.json 后执行 --apply。",
                "clusters": clusters,
                "report": {k: report[k] for k in ("forgotten", "promoted", "faded", "archived")},
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
        # 没有可整理的碎片就不写 pending.json，免得让 AI 对着一份空任务空转
        write_dream_report(report)
        st = load_dream_state()
        st["last_dream"] = now
        st["last_task"] = task_id if clusters else None
        atomic_json(HOME / "dream_state.json", st)

    return report


def write_dream_report(report: dict) -> Path:
    now = report["at"]
    base = f"{datetime.fromtimestamp(now).strftime('%Y%m%d_%H%M%S')}"
    path = HOME / "dreams" / f"{base}.md"
    n = 1
    while path.exists():  # 同秒多次做梦时不要覆盖上一份
        n += 1
        path = HOME / "dreams" / f"{base}_{n}.md"
    lines = [f"# 梦境整理 · {fmt_time(now)}", "", f"触发：{report['trigger']}", ""]
    lines.append(f"- 重放片段：{sum(len(c['texts']) for c in report['clusters'])} 条 / {len(report['clusters'])} 组")
    lines.append(f"- 巩固入长期：{len(report['promoted'])} 条")
    lines.append(f"- 淡忘归档：{len(report['forgotten'])} 条")
    lines.append(f"- 雪藏冷档：{len(report.get('archived', []))} 条（原文保留，可 archive search / restore）")
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


def load_dream_pending() -> dict | None:
    """当前待整理的梦任务（pending.json）；不存在/损坏返回 None。"""
    return _slurp_json(HOME / "dreams" / "pending.json", None)


def dream_draft_path() -> Path:
    return HOME / "dreams" / "apply_draft.json"


def load_dream_draft() -> dict | None:
    """上次在面板里编辑、还没应用的 apply.json 草稿。"""
    return _slurp_json(dream_draft_path(), None)


def save_dream_draft(obj) -> dict:
    """把 apply.json 草稿存盘（面板网页编辑→保存，避免模型合并出错时只能命令行改文件）。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法保存草稿。")
    ensure_home()
    if not isinstance(obj, dict):
        raise ValueError("草稿必须是 JSON 对象")
    atomic_json(dream_draft_path(), obj)
    return {"ok": True, "path": str(dream_draft_path())}


def clear_dream_draft() -> None:
    try:
        dream_draft_path().unlink()
    except (FileNotFoundError, OSError):
        pass


def load_dream_state() -> dict:
    path = HOME / "dream_state.json"
    if path.exists():
        d = _slurp_json(path)
        if isinstance(d, dict):
            return d
    return {"last_dream": 0, "last_task": None}


def apply_dream(result: dict) -> dict:
    ensure_home()
    pending_path = HOME / "dreams" / "pending.json"
    task_id = result.get("task_id")
    applied = {"memories": 0, "forgotten": 0, "persona": [], "rejected": []}

    # 读-校验-改-写-消费 pending 整体在一把锁里：
    # 1) 两个 apply 并发时，第二个进锁后 pending 已被删，直接拒绝，不会把同一份结果套两遍；
    # 2) 整库 save_all 不会把锁外并发写入的新记忆覆盖丢。
    with _locked(HOME / "hippocampus.jsonl"):
        # 先对任务号：apply 必须对着当前这场的 pending.json，防止把上一场的结果
        # 或乱写的 task_id 应用到现在的记忆上。不对就直接拒绝，不碰任何数据。
        pending_id = None
        if pending_path.exists():
            try:
                pending_id = (json.loads(pending_path.read_text("utf-8")) or {}).get("task_id")
            except Exception:
                pending_id = None
        if pending_id is None:
            raise ValueError("没有待应用的任务：dreams/pending.json 不存在")
        if not task_id:
            # 以前这里 fallback 到 pending_id，等于"缺 task_id 但也放行"——
            # 上一场漏写 task_id 的 apply.json 会静默套用到这一场。改为必须显式带上。
            raise ValueError(
                f"apply 里没有 task_id：为防止把上一场的整理结果套用到这一场，拒绝应用。"
                f"请在 apply.json 里带上 \"task_id\": \"{pending_id}\"。"
            )
        if task_id != pending_id:
            raise ValueError(
                f"任务对不上：apply 里的 task_id={task_id!r}，与 pending.json 的 {pending_id!r} 不一致，拒绝应用。"
            )
        records = load_all()
        by_id = {m["id"]: m for m in records if m.get("id")}

        for item in result.get("memories") or []:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            rec = make_record(
                text,
                kind=item.get("kind", "fact"),
                importance=_num(item.get("importance"), 0.7),
                tags=item.get("tags"),
                entities=item.get("entities"),
                source="dream",
                layer="cortex",
                links=[x for x in (item.get("source_ids") or []) if isinstance(x, str)],
            )
            rec["gist"] = item.get("gist") or heuristic_gist(text)
            rec["consolidated"] = True
            rec["detail"] = 1.0
            # AI 整理出来的记忆先标"待审核"：防止做梦合并/编造的内容直接当事实沉淀。
            rec["review"] = "pending"
            rec["review_source"] = "dream"
            # —— 矛盾记忆不覆盖，留链路 ——
            # 新记忆与旧记忆高度相似但内容不同（说法变了/事实更新）时，旧的不删，
            # 打上 superseded_by 指向新的：回忆默认跳过被取代者，需要时仍可回溯
            # "我以前是怎么想的"——像人一样，观念变了但旧观念还在记忆深处。
            for old in records:
                if old is rec or old.get("layer") == "dust" or old.get("superseded_by"):
                    continue
                old_terms = terms(str(old.get("text", "")))
                if not old_terms:
                    continue
                sim = jaccard(terms(text), old_terms)
                if sim >= 0.55 and str(old.get("text", "")).strip() != text:
                    old["superseded_by"] = rec["id"]
                    rec.setdefault("links", [])
                    if old["id"] not in rec["links"]:
                        rec["links"].append(old["id"])
                    applied.setdefault("superseded", []).append(old["id"])
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
                # pinned 与 dream 主循环同一待遇：用户置顶的不许 AI 梦里淡忘——
                # 否则 apply.json 里给个 pinned id 就能绕过防线
                if m.get("pinned"):
                    applied["rejected"].append(f"拒绝淡忘 {sid}：用户置顶的记忆不参与做梦淡忘")
                    continue
                m["layer"] = "dust"
                m["forgotten_at"] = now_ts()
                applied["forgotten"] += 1

        save_all(records)
        # 在锁内消费 pending：写入与"这场梦已应用"必须同时成立，
        # 否则并发 apply / 崩溃重试会把这批记忆再套一遍（重复沉淀）。
        try:
            pending_path.unlink()
        except FileNotFoundError:
            pass

    upd = result.get("persona_updates")
    upd = upd if isinstance(upd, dict) else {}
    if upd:
        evid = [sid for it in (result.get("memories") or [])
                if isinstance(it, dict) for sid in (it.get("source_ids") or [])
                if isinstance(sid, str)][:10]
        applied["rejected"] = update_persona(upd, why="做梦整理", source="dream", evidence=evid)

    narrative = (result.get("narrative") or "").strip()
    if narrative or applied["memories"]:
        path = HOME / "dreams" / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_apply.md"
        body = [f"# 梦的沉淀 · {fmt_time(now_ts())}", f"任务：{task_id}", "",
                f"新增长期记忆 {applied['memories']} 条，遗忘 {applied['forgotten']} 条。"]
        if narrative:
            body += ["", "## 梦里的叙述", narrative]
        if upd.get("insights"):
            body += ["", "## 洞见"] + [f"- {x}" for x in upd["insights"]]
        rej = applied.get("rejected") or []
        if rej:
            # 被双门限/锁定/不存在拦下的改动留痕在梦报告里——AI 与人都能看到
            # "梦想改什么、防线拦了什么"，而不是只看到悄悄没生效
            body += ["", "## 被防线拦下的改动"] + [f"- {x}" for x in rej]
        try:
            path.write_text("\n".join(body) + "\n", encoding="utf-8")
        except OSError:
            pass   # 梦境笔记写不进去不影响记忆已落盘，别把成功报成失败
    clear_dream_draft()  # 应用成功后，网页里编辑过的那份草稿也一并清掉
    return applied
