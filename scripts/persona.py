#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 灵枢 · 人格：身份/性格/内驱、快照回滚、初识与自我设定。"""

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
        old = _slurp_json(HOME / "persona.json")
        if old is None and not READONLY:
            # 档案存在但读不出来（真损坏，不是 BOM）：先留一份副本再重建，绝不静默丢人格
            try:
                shutil.copy2(HOME / "persona.json",
                             HOME / f"persona.json.corrupt-{int(now_ts())}")
            except OSError:
                pass
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
        p = _slurp_json(path)
        if not isinstance(p, dict):
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
    with _locked(HOME / "persona.json"):
        atomic_json(HOME / "persona.json", p)


def _is_locked(p: dict, label: str) -> bool:
    locks = set(p.get("locked", []))
    if not locks:
        return False
    if label in locks:
        return True
    # 整组锁：lock "identity" / "traits" / "drives" 锁住该组下所有字段
    if "." in label and label.split(".", 1)[0] in locks:
        return True
    if label.startswith("identity.") and label.split(".", 1)[1] in locks:
        return True
    if label.startswith("traits.") and label.split(".", 1)[1] in locks:
        return True
    for item in locks:
        # 用户在面板/文档里看到的叫"底线"（persona_brief），锁的写法可能是"价值观/底线"
        if label == "values" and any(k in item for k in ("价值", "底线")):
            return True
    return False


def persona_brief(p: dict, mood: bool = True) -> str:
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
    # 与 mind_state() 同一套兜底：traits 已有数值而 drives 还空时，别在自我
    # 描述里一边「性格：好奇85」一边「内驱：还没有数值」——自相矛盾每轮被读。
    if drives.get("curiosity") is None and drives.get("reason") is None:
        _t = p.get("traits") or {}
        if _t.get("好奇") is not None or _t.get("理智") is not None:
            drives = {"curiosity": _t.get("好奇"), "reason": _t.get("理智")}
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
    mb = mood_brief() if mood else ""
    if mb:
        tone = mood_tone_hint()
        lines.append(f"此刻心情：{mb}。" + (f"{tone}。" if tone else "")
                     + "它会自然回落，也会影响你说话的语气。")
    elif mood and st.get("mood"):
        lines.append(f"此刻情绪：{st['mood']}" + (f"（{st['mood_note']}）" if st.get("mood_note") else "") + "。")
    habits = p.get("evolved", {}).get("habits", [])
    if habits:
        lines.append("养成的习惯：" + "；".join(h["name"] if isinstance(h, dict) else str(h) for h in habits[-4:]) + "。")
    return "\n".join(lines)


def update_persona(updates: dict, why: str = "做梦整理", source: str = "",
                   evidence: list | None = None) -> list:
    """应用人格变更，写入 evolution history。返回被拒绝的字段。

    source：谁发起的（dream/自我总结/面板/…）；evidence：依据的记忆/事件 id 列表。
    两人格漂移要能溯源：这条改动来自哪场梦、哪段对话。
    """
    if not isinstance(updates, dict):
        return []
    # load→改→save 整体加锁：否则并发的两次改动后写者会把先写者的字段覆盖回旧值
    with _locked(HOME / "persona.json"):
        return _update_persona_inner(updates, why, source, evidence)


def _update_persona_inner(updates: dict, why: str, source: str,
                          evidence: list | None) -> list:
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
        entry = {"ts": now, "field": field, "from": old, "to": new, "why": why}
        if source:
            entry["source"] = source
        if evidence:
            entry["evidence"] = list(evidence)[:10]
        hist.append(entry)

    for key, val in (updates.get("traits") or {}).items():
        if key not in p.get("traits", {}):
            rejected.append(f"traits.{key}(不存在)")
            continue
        if _is_locked(p, f"traits.{key}"):
            rejected.append(f"traits.{key}(已锁定)")
            continue
        old = p["traits"][key]
        try:
            if why == "做梦整理" and isinstance(val, (int, float)):
                # old 可能是合法的 0：不能用 `old or 50`（0 会被当成 50）
                base = 50 if old is None else int(old)
                # —— 人格漂移双门限 + EMA ——
                # 人格是长年累月养成的，不该被一两场对话带偏：做梦给出的微调先记在
                # drift 账本上，小幅波动（低于门限）只是心里动了一下，不落盘；同向
                # 攒够门限才真正改人格，改的幅度也打六折——越大的转变越要慢慢来。
                drift = st.setdefault("trait_drift", {})
                acc = _num(drift.get(key), 0.0) + max(-10, min(10, int(val)))
                if abs(acc) < TRAIT_DRIFT_GATE:
                    drift[key] = round(acc, 2)
                    log(f"traits.{key}(漂移累积)", old, f"账面 {acc:+.1f}，未过门限未改动")
                    continue
                eff = acc * TRAIT_DRIFT_EMA
                new = base + int(round(eff))
                drift[key] = round(acc - eff, 2)   # 平滑掉的余额留在账上，下次接着攒
                new = max(0, min(100, new))
                if new == old:
                    log(f"traits.{key}", old, f"倾向 {acc:+.1f} 但撞在边界上，未变")
                    continue
            else:
                new = int(val)                     # 面板 / CLI：绝对值 0-100
                new = max(0, min(100, new))
        except (TypeError, ValueError):
            rejected.append(f"traits.{key}(非法值)")
            continue
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


_PERSONA_ROOTS = {"identity", "traits", "drives", "state",
                  "style", "values", "background", "likes", "dislikes", "relationship"}
_PERSONA_LIST_FIELDS = {"style", "values", "background", "likes", "dislikes", "relationship"}


def _split_list_value(value: str) -> list:
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        pass
    return [x.strip() for x in re.split(r"[、,，;；\n]+", value or "") if x.strip()]


def set_persona_field(path: str, value: str) -> dict:
    with _locked(HOME / "persona.json"):
        return _set_persona_field_inner(path, value)


def _set_persona_field_inner(path: str, value: str) -> dict:
    p = load_persona()
    root = path.split(".")[0]
    if root not in _PERSONA_ROOTS:
        raise ValueError(f"不认识的字段：{path}（可用 identity.* / traits.* / drives.* / state.* / "
                         "style / values / background / likes / dislikes / relationship）")
    if _is_locked(p, path):
        raise PermissionError(f"{path} 已被锁定，不能由 AI 修改")
    parts = path.split(".")
    # 结构段（identity/traits/drives/state）必须写成 根.子字段；裸写会把整段覆盖成字符串/数字，
    # 之后注入与面板读取都会崩。列表类（style/values/...）允许整份覆盖。
    if len(parts) == 1 and root not in _PERSONA_LIST_FIELDS:
        raise ValueError(f"{path} 需要写成「{root}.子字段」的形式（如 {root}.名字=...）；"
                         "列表类（style/values/likes/dislikes/background/relationship）可直接写。")
    node = p
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    leaf = parts[-1]
    old = node.get(leaf)
    if root in _PERSONA_LIST_FIELDS:
        # 列表字段：一律转成 list，避免把 likes 之类写成裸字符串让注入崩溃
        val = _split_list_value(value)
    else:
        try:
            val = json.loads(value)
        except Exception:
            val = value
        if (root in ("traits", "drives")
                or (root == "state" and leaf in ("energy", "closeness"))):
            # 数值字段：强制数值化并 clamp 到 0-100（与 update_persona 同口径）；
            # 非法值（如 abc）直接拒绝，不静默存库
            try:
                num = float(val)
            except (TypeError, ValueError):
                raise ValueError(f"{path} 需要 0-100 的数值，收到：{value!r}")
            if num != num:  # NaN
                raise ValueError(f"{path} 需要 0-100 的数值，收到：{value!r}")
            val = max(0, min(100, int(num)))
        elif isinstance(old, int) and isinstance(val, (int, float)):
            val = int(val)
    node[leaf] = val
    p.setdefault("history", []).append({"ts": now_ts(), "field": path, "from": old, "to": val, "why": "手动编辑"})
    save_persona(p)
    return p


def persona_history(limit: int = 30) -> list:
    p = load_persona()
    h = list(p.get("history", []))
    return list(reversed(h[-limit:]))


def persona_rollback(index: int | None = None) -> dict:
    """一键回滚人格：把某条历史变更撤掉。

    index 从最新一条往前数（1 = 撤销最近一次改动）。被撤的是普通字段就
    恢复成旧值；是 evolved.* 的增项就把那条移除。回滚本身会记进历史，
    让"改了什么"始终有据可查。
    """
    with _locked(HOME / "persona.json"):
        p = load_persona()
        return _persona_rollback_inner(p, index)


def _persona_rollback_inner(p: dict, index: int | None = None) -> dict:
    hist = p.setdefault("history", [])
    if not hist:
        raise ValueError("没有可回滚的人格历史（还什么都没改过）。")
    idx = index or 1
    if idx < 1 or idx > len(hist):
        raise ValueError(f"序号越界：历史共 {len(hist)} 条，回滚写 1..{len(hist)}。")
    entry = hist[len(hist) - idx]
    field = entry.get("field", "")
    frm, to = entry.get("from"), entry.get("to")
    if _is_locked(p, field):
        raise PermissionError(f"{field} 已被锁定，不能回滚")
    parts = field.split(".")
    node = p
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    leaf = parts[-1]
    overwrote = False
    if field.startswith("evolved."):
        bucket = field.split(".", 1)[1]
        arr = node.get(bucket)
        if isinstance(arr, list):
            node[bucket] = [x for x in arr
                            if (x.get("name") if isinstance(x, dict) else x) != to]
    else:
        current = node.get(leaf)
        # 该字段在这条变更之后还被改过（当前值已不是这条的 to）→ 回滚会把它打回旧值，
        # 让调用方明确提示"这会覆盖之后 N 次改动"，而不是静默覆盖。
        # 注意：新增项（from=None）在列表字段里各自独立，不存在"覆盖后来的改动"，别误报。
        if frm is not None and to is not None and current != to:
            overwrote = True
        if frm is None:
            # 这条是"新增"（from=None）：只撤销新增的那一项，绝不把整个字段/整段删光。
            # 例：locked 列表回滚一条锁，不能把全部锁清空；style/values 增项回滚，
            # 不能把整份清单删掉；state 结构段没有旧值可还原，保守不动。
            if isinstance(current, list) and (leaf in _PERSONA_LIST_FIELDS or leaf == "locked"):
                node[leaf] = [x for x in current if x != to]
            elif isinstance(current, dict):
                # 无旧值可还原的结构段：不清空，避免丢数据；也不记"回滚"历史——
                # 实际什么都没回退，记了会让回滚轨迹失真（下次回滚会数错条）。
                save_persona(p)
                return {"field": field, "from": None, "to": frm,
                        "why": entry.get("why", ""), "overwrote_later": False,
                        "note": "该字段无旧值可还原，未做改动"}
            else:
                node.pop(leaf, None)
        elif isinstance(current, dict) and isinstance(frm, str):
            # 目标叶子是 dict（如 onboarding），而历史里的 from 是字符串（初识标记），
            # 直接替换会把它变成字符串导致后续注入崩溃 → 只回滚 dict 内部的初识标记。
            node[leaf] = {"stage": frm}
        else:
            node[leaf] = frm
    hist.append({"ts": now_ts(), "field": field,
                 "from": to, "to": frm, "why": "回滚"})
    save_persona(p)
    return {"field": field, "from": to, "to": frm, "why": entry.get("why", ""),
            "overwrote_later": overwrote}


# ------------------------------------------------ 人格快照 / 漂移

PERSONA_SNAP_DIR = HOME / "persona_snapshots"


PERSONA_SNAP_KEEP = 40  # 快照保留上限，防止无限增长


def _prune_snapshots(keep: int = PERSONA_SNAP_KEEP) -> None:
    """只保留最近 keep 张快照，多余的删掉（不会删到刚拍的这张）。"""
    rows = persona_snapshots(10000)
    for r in rows[keep:]:
        try:
            (PERSONA_SNAP_DIR / f"{r['name']}.json").unlink()
        except OSError:
            pass


def persona_snapshot(label: str = "") -> dict:
    """给当前人格拍一张快照（完整副本）。恢复基准 + 漂移对比都靠它。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法拍快照。")
    p = load_persona()
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", (label or "快照").strip())[:24]
    PERSONA_SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = PERSONA_SNAP_DIR / f"{now_ts()}_{safe}.json"
    atomic_json(path, {"ts": now_ts(), "label": label.strip() or "快照", "persona": p})
    try:
        _prune_snapshots()
    except Exception:
        pass
    return {"path": str(path), "ts": now_ts(), "label": label.strip() or "快照"}


def persona_snapshots(limit: int = 30) -> list:
    if not PERSONA_SNAP_DIR.exists():
        return []
    rows = []
    for f in PERSONA_SNAP_DIR.glob("*.json"):
        d = _slurp_json(f)
        if not isinstance(d, dict):
            continue
        rows.append({"name": f.stem, "ts": int(d.get("ts", 0)),
                     "label": d.get("label", f.stem)})
    rows.sort(key=lambda x: -x["ts"])
    return rows[:limit]


@_persona_txn
def persona_restore(name: str) -> dict:
    """从快照恢复整个人格（完整替换，注意会盖掉当前状态与历史）。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法恢复快照。")
    import re as _re
    safe = _re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name or "")
    path = PERSONA_SNAP_DIR / f"{safe}.json"
    if not path.exists() and name:
        # 允许用快照的 label 或名字片段模糊匹配（用户通常记得标签而不是时间戳文件名）
        cands = [r for r in persona_snapshots(10000)
                 if name == r.get("label") or name in r.get("label", "")
                 or name in r.get("name", "")]
        if len(cands) == 1:
            path = PERSONA_SNAP_DIR / f"{cands[0]['name']}.json"
    if not path.exists():
        names = "、".join(f"{r['label']}({r['name']})" for r in persona_snapshots(10)) or "（还没有快照）"
        raise ValueError(f"没有快照：{name}。现有：{names}")
    d = _slurp_json(path)
    if not isinstance(d, dict):
        raise ValueError(f"快照文件损坏，读不出来：{name}")
    persona = d.get("persona") or {}
    # 恢复前先给"当前状态"留一张自动快照（可随时反悔），并记一条历史，保证恢复动作可溯源
    try:
        persona_snapshot("restore前自动备份")
    except Exception:
        pass
    # 合并历史：快照里的历史 + 当前历史（去重、按时间排序），避免恢复后丢失之后的审计轨迹
    cur_hist = (load_persona().get("history") or [])
    snap_hist = persona.get("history") or []
    merged, seen = [], set()
    for h in sorted(snap_hist + cur_hist, key=lambda x: _num(x.get("ts"), 0)):
        key = (h.get("ts"), h.get("field"), str(h.get("to")), h.get("why"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(h)
    persona["history"] = merged
    persona.setdefault("history", []).append(
        {"ts": now_ts(), "field": "persona", "from": "(自动备份)",
         "to": name, "why": "persona restore"})
    save_persona(persona)
    return {"restored": name, "ts": d.get("ts", 0), "label": d.get("label", name)}


def persona_drift(days: int = 7) -> dict:
    """人格漂移：与最近一张快照对比，或退而统计最近 N 天的 AI 变更。

    目的：微小的偏移也要让用户看得到，不是等跑偏了才发现。
    """
    snaps = [s for s in persona_snapshots(40) if s.get("label") != "restore前自动备份"]
    diff = []
    if snaps and (now_ts() - snaps[0]["ts"]) <= days * 86400:
        try:
            base = json.loads((PERSONA_SNAP_DIR / f"{snaps[0]['name']}.json").read_text(encoding="utf-8"))
            bp, cur = base.get("persona") or {}, load_persona()
            for section in ("traits", "drives", "identity"):
                # state 段（mood/energy/closeness）是瞬态：聊句心情、睡一觉精力回升
                # 都会变，算进漂移就是"动不动就偏大"。稳定特征（性格/驱动/身份）
                # 才是漂移该盯的；state 变更另有 history 溯源，不在这里报。
                # drives（好奇心/理智等内驱）也带动态性：做梦/对话会自然回调数值、
                # 且没有 traits 那样的漂移门限——把它算进告警，两次对话就能凑够
                # 3 处 diff 触发"人格漂移偏大"误报。diff 里保留但不算进 alert。
                b = bp.get(section) or {}
                c = cur.get(section) or {}
                for k in set(b) | set(c):
                    if b.get(k) != c.get(k):
                        diff.append({"section": section, "key": k,
                                     "from": b.get(k), "to": c.get(k)})
        except Exception:
            diff = []
    ai = ai_persona_changes(days)
    return {
        "baseline": snaps[0]["name"] if snaps else None,
        "baseline_ts": snaps[0]["ts"] if snaps else None,
        "changes": diff,
        "ai_changes": [{"ts": h.get("ts"), "field": h.get("field"),
                        "from": h.get("from"), "to": h.get("to"), "why": h.get("why"),
                        "source": h.get("source", "")} for h in ai],
        "counts": {"diff": len(diff), "ai_changes": len(ai)},
        # 告警只盯稳定特征（traits/identity 的变动）+ AI 主动改动；
        # drives 的自然波动不算（否则两次对话就误报"漂移偏大"）
        "alert": (len([d for d in diff if d.get("section") != "drives"]) >= 3
                  or len(ai) >= 3),
    }


def persona_diff() -> dict:
    """人格漂移 diff 视图：最早一张快照 vs 当前人格的逐字段对比。

    面板用："漂移偏大"告警只是个信号，用户真正想看的是"到底哪几个字段从什么
    变成了什么"——这张表就是答案。快照不足两张（无历史可对比）时返回 empty。

    数据源用 persona_snapshots（每次漂移检查/恢复都会留完整人格副本）而不是
    evolution.jsonl——evolution 只有 skill/self 两种 kind，从不写 persona 行，
    从那里取数据永远是空表。
    """
    # 与当前人格比才有"漂移"含义：最新一张快照往往就是上次漂移检查时拍的，
    # 和当前几乎没差别；取最旧的一张做基准，展示的是从基准到现在的累计变化。
    snaps = sorted(persona_snapshots(10000), key=lambda s: s.get("ts", 0))
    if len(snaps) < 2:
        return {"days": 0, "fields": [], "empty": True}
    first, last = snaps[0], snaps[-1]

    def _flatten(d, prefix=""):
        out = {}
        for k, v in (d or {}).items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten(v, key))
            else:
                out[key] = v
        return out

    def _show(v):
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return "" if v is None else str(v)

    def _load_snap(name):
        try:
            d = _slurp_json(PERSONA_SNAP_DIR / f"{name}.json")
            # 瞬态 state（mood/energy）不进 diff：聊句心情就变，算进来全是噪音，
            # 与 persona_drift 只盯稳定特征（identity/traits/drives）口径一致
            return {k: v for k, v in (d.get("persona") or {}).items() if k != "state"}
        except Exception:
            return {}

    a = _flatten(_load_snap(first["name"]))
    b = _flatten(_load_snap(last["name"]))
    fields = [{"field": k, "old": _show(a.get(k)), "new": _show(b.get(k)), "changed": True}
              for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    days = int((last.get("ts", 0) - first.get("ts", 0)) // 86400)
    return {"days": days, "fields": fields,
            "from": first.get("label", first["name"]), "to": last.get("label", last["name"])}


@_persona_txn
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
    # traits 与 drives 语义重叠（interview --unsure 只写 traits 不写 drives）。
    # drives 缺失时从 traits 兜底，否则注入里会同时出现「性格：好奇85」和
    # 「内驱：还没有数值」的自相矛盾。
    if dr.get("curiosity") is None and dr.get("reason") is None:
        _traits = p.get("traits") or {}
        if _traits.get("好奇") is not None or _traits.get("理智") is not None:
            dr = {"curiosity": _traits.get("好奇"), "reason": _traits.get("理智")}
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
            "；".join(notes) if notes else "理智与好奇心没有冲突。",
        ],
    }


@_persona_txn
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


@_persona_txn
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


@_persona_txn
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

    if name and not _is_locked(p, "identity.名字"):
        log("identity.名字", ident.get("名字"), name)
        ident["名字"] = name
    if self_name.strip() and not _is_locked(p, "identity.自称"):
        log("identity.自称", ident.get("自称"), self_name.strip())
        ident["自称"] = self_name.strip()
    if callme.strip() and not _is_locked(p, "identity.称呼你"):
        log("identity.称呼你", ident.get("称呼你"), callme.strip())
        ident["称呼你"] = callme.strip()
    for k, v in parsed.items():
        if _is_locked(p, f"traits.{k}"):
            continue
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


@_persona_txn
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
    mark = len(hist)   # 这之后追加的条目都算 AI（初识）发起，用于溯源/漂移
    filled = False     # 真正写入了身份/性格才算"我的部分完成"

    def items(text):
        return [x.strip() for x in re.split(r"[、,，;；\n]+", text or "") if x.strip()]

    if name.strip() and not _is_locked(p, "identity.名字"):
        hist.append({"ts": now, "field": "identity.名字", "from": ident.get("名字"),
                     "to": name.strip(), "why": "初识：用户不知道，我自己起的名字"})
        ident["名字"] = name.strip()
        filled = True
    if self_name.strip() and not _is_locked(p, "identity.自称"):
        hist.append({"ts": now, "field": "identity.自称", "from": ident.get("自称"),
                     "to": self_name.strip(), "why": "初识：我自己定的自称"})
        ident["自称"] = self_name.strip()
        filled = True
    if callme.strip() and not _is_locked(p, "identity.称呼你"):
        hist.append({"ts": now, "field": "identity.称呼你", "from": ident.get("称呼你"),
                     "to": callme.strip(), "why": "初识：我自己定的称呼"})
        ident["称呼你"] = callme.strip()
        filled = True
    for k, v in _parse_traits(traits).items():
        if _is_locked(p, f"traits.{k}"):
            continue
        hist.append({"ts": now, "field": f"traits.{k}", "from": p.setdefault("traits", {}).get(k),
                     "to": v, "why": "初识：用户没定，我自己写的性格"})
        p["traits"][k] = v
        filled = True
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
    # 名字和性格都空着就不算完成——不能凭空打勾；只有真正写出了名字或性格才算。
    # 但用户已答出名字+性格、AI 没有身份/性格可补（filled 恒 False）时，补喜好/习惯也算完成初识，
    # 否则注入包会永远催"初识还没完成"。
    identity_ready = bool(ident.get("名字")) and bool(p.get("traits"))
    if ob.get("stage") != "done" and (filled or identity_ready):
        ob.update({"stage": "done", "completed": True, "completed_at": now})
        hist.append({"ts": now, "field": "onboarding", "from": "awaiting_self",
                     "to": "done", "why": "初识完成"})
    for _h in hist[mark:]:
        _h.setdefault("source", "初识")
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
