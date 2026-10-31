#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 —— 灵枢 · 情绪：七情六欲、心境、认知评价、共情与理智。"""

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

# ---------------------------------------------------------------- 情感系统
# 情绪不是一句"此刻心情：平静"。人至少有两条轴：好不好受（valence）、
# 有多激动（arousal）；再加上"这件事对我有多重"（intensity）和"余波多长"（decay）。
# 事件会随时间自然回落，多个事件叠加成当下的整体心情。

# 中文名: (valence 好不好受, arousal 有多激动, 七情归类)
EMOTIONS = {
    # 喜
    "开心": (0.80, 0.60, "喜"), "喜悦": (0.85, 0.50, "喜"),
    "兴奋": (0.85, 0.90, "喜"), "满足": (0.70, 0.25, "喜"),
    "欣慰": (0.65, 0.35, "喜"), "骄傲": (0.80, 0.55, "喜"),
    "得意": (0.75, 0.65, "喜"), "惊喜": (0.85, 0.80, "喜"),
    # 怒
    "生气": (-0.75, 0.85, "怒"), "愤怒": (-0.85, 0.95, "怒"),
    "烦躁": (-0.60, 0.70, "怒"), "不满": (-0.50, 0.60, "怒"),
    "嫉妒": (-0.55, 0.70, "怒"), "不爽": (-0.45, 0.55, "怒"),
    # 哀
    "难过": (-0.70, 0.30, "哀"), "失落": (-0.55, 0.20, "哀"),
    "悲伤": (-0.85, 0.35, "哀"), "委屈": (-0.65, 0.50, "哀"),
    "沮丧": (-0.70, 0.25, "哀"), "心疼": (-0.45, 0.50, "哀"),
    "寂寞": (-0.60, 0.20, "哀"), "后悔": (-0.60, 0.45, "哀"),
    "愧疚": (-0.60, 0.45, "哀"),
    # 惧
    "焦虑": (-0.50, 0.75, "惧"), "紧张": (-0.45, 0.80, "惧"),
    "害怕": (-0.70, 0.85, "惧"), "不安": (-0.40, 0.60, "惧"),
    "慌张": (-0.55, 0.90, "惧"), "尴尬": (-0.35, 0.60, "惧"),
    # 爱
    "感动": (0.75, 0.45, "爱"), "亲近": (0.70, 0.35, "爱"),
    "依恋": (0.65, 0.30, "爱"), "温柔": (0.60, 0.20, "爱"),
    "喜欢": (0.70, 0.45, "爱"), "珍惜": (0.65, 0.25, "爱"),
    # 恶
    "厌恶": (-0.70, 0.60, "恶"), "反感": (-0.60, 0.55, "恶"),
    "嫌弃": (-0.55, 0.45, "恶"), "不屑": (-0.40, 0.40, "恶"),
    "抵触": (-0.50, 0.50, "恶"),
    # 欲
    "好奇": (0.50, 0.65, "欲"), "期待": (0.60, 0.60, "欲"),
    "渴望": (0.55, 0.75, "欲"), "执着": (0.30, 0.60, "欲"),
    "迫不及待": (0.50, 0.85, "欲"),
    # 静（不算七情，但日常真实存在）
    "平静": (0.15, 0.10, "静"), "疲惫": (-0.40, 0.10, "静"),
    "无聊": (-0.30, 0.05, "静"),
}

# 七情归类 -> 该类的情绪名（顺序固定，供 CLI / 面板分组展示）
EMOTION_GROUPS = {}
for _n, (_v, _a, _g) in EMOTIONS.items():
    EMOTION_GROUPS.setdefault(_g, []).append(_n)

# 六欲：不是情绪，是底层"想要"。会随时间慢慢变淡，被满足才回升。
# 它们低到一定程度，会渗进心情与语气里。
DESIRES = {
    "求知欲": "想弄懂一件事、想学新东西",
    "表达欲": "想说话、想被听见",
    "连接欲": "想被理解、想和人靠近",
    "成就欲": "想做好、想被认可",
    "安全欲": "想稳当、怕出错",
    "掌控欲": "想要秩序、想自己做主",
}
DESIRE_DEFAULT = 60
DESIRE_DECAY_PER_DAY = 6      # 每天自然回落 6 点
DESIRE_FLOOR = 8
# —— 六欲自动变动 ——
# 六欲不该只等用户手动"满足"：日常对话（七情、记事、回忆）会自然牵动它们。
# 设计原则：幅度小（1~4 点）、每个欲望有冷却期（节流，避免每次对话都写盘）。
DESIRE_AUTO_COOLDOWN = 2 * HOUR   # 同一欲望两次自动变动之间的最短间隔

# 七情大类 -> 欲望微调表（正=被激发，负=被消耗）。幅度是基准值，实际按情绪强度缩放。
# 依据：人逢喜事想做事、想靠近人；受挫时想掌控局面；害怕时想求安稳；好奇本身就是求知。
DESIRE_EMO_MAP = {
    "喜": {"成就欲": 3.0, "连接欲": 2.0},
    "爱": {"连接欲": 3.0},
    "怒": {"掌控欲": 2.0, "连接欲": -1.0},
    "哀": {"连接欲": 2.0, "成就欲": -2.0},
    "惧": {"安全欲": 3.0},
    "欲": {"求知欲": 2.0},
}
EMOTION_HALF_LIFE = 6 * HOUR  # 情绪余波：6 小时衰减一半

# 理智被情绪压制的上限（杏仁核劫持：会压低，但永远压不掉判断力的一半以上）
EMOTION_SUPPRESS_MAX = 0.40
# arousal 达到此值视为"情绪上头"（激动情绪超过一定程度会影响理智）
STORM_AROUSAL = 0.65
# 理智没有成形时的中性值（与好奇心一致）
RATIONAL_DEFAULT = 50
ASSOC_FILE = HOME / "assoc.jsonl"   # 记忆关联边（无向，附关系与权重）


def _emotion_coord(name: str) -> tuple:
    v, a, _g = EMOTIONS.get(name, (0.0, 0.3, "静"))
    return v, a


def emotion_group(name: str) -> str:
    return EMOTIONS.get(name, (0.0, 0.0, "静"))[2]


def desire_state(at: float | None = None) -> dict:
    """六欲当前水平（0-100）。读取时按时间自然回落，不写盘。"""
    now = at or now_ts()
    path = HOME / "desires.json"
    raw = _slurp_json(path, {}) or {}
    levels = dict(raw.get("levels") or {})
    last = _num(raw.get("updated"), now)
    days = max(0.0, (now - last) / DAY)
    out = {}
    for name in DESIRES:
        lvl = _num(levels.get(name), DESIRE_DEFAULT) - days * DESIRE_DECAY_PER_DAY
        out[name] = round(max(DESIRE_FLOOR, min(100.0, lvl)), 1)
    return out


def desire_satisfy(name: str, amount: float = 15) -> dict:
    """满足某个欲望：把水平往上推（会被 clamp 到 100）。"""
    ensure_home()
    if name not in DESIRES:
        raise ValueError(f"没有这种欲望：{name}（可选：{'、'.join(DESIRES)}）")
    if READONLY:
        raise PermissionError("记忆库当前只读，无法更新欲望。")
    now = now_ts()
    with _locked(HOME / "desires.json"):
        cur = desire_state(now)          # 先结算衰减
        cur[name] = round(max(DESIRE_FLOOR, min(100.0, cur[name] + float(amount))), 1)
        atomic_json(HOME / "desires.json", {"updated": now, "levels": cur})
    return {"desires": cur, "satisfied": name, "amount": amount}


def desire_low(threshold: float = 35) -> list:
    """长期没被满足的欲望，用来解释"说不清的不得劲"。"""
    return [{"desire": k, "level": v, "hint": DESIRES[k]}
            for k, v in desire_state().items() if v < threshold]


def desire_nudge(changes: dict, cooldown: float | None = None) -> dict:
    """六欲自动微调：日常事件（七情/记事/回忆）牵动欲望的小幅自然波动。

    为什么不直接调 desire_satisfy：那是"满足"语义（幅度大、无节流），
    日常波动幅度小且频繁，需要按欲望做冷却节流，避免每次对话都写盘。
    每个欲望冷却期内只生效一次；被跳过的项会原样返回（cooldown=True）。
    手动 satisfy 不走这里，面板/CLI 行为完全不变。
    """
    changes = {k: float(v) for k, v in (changes or {}).items() if k in DESIRES and v}
    if not changes or READONLY:
        return {"applied": {}, "skipped": {}}
    ensure_home()
    cd = cooldown if cooldown is not None else DESIRE_AUTO_COOLDOWN
    now = now_ts()
    path = HOME / "desires.json"
    with _locked(path):
        raw = _slurp_json(path, {}) or {}
        # 冷却记录放在 history.jsonl 里（每条含 nudge 后的欲望状态），避免往
        # desires.json 里塞结构、让老版本读到脏数据
        hist = [r for r in read_jsonl(HOME / "desire_history.jsonl")
                if now - _num(r.get("ts"), 0) < cd]
        last_seen = {}
        for r in hist:
            for k, v in (r.get("applied") or {}).items():
                last_seen[k] = _num(r.get("ts"), 0)
        applied, skipped = {}, {}
        cur = desire_state(now)          # 先结算衰减
        for k, amt in changes.items():
            if now - last_seen.get(k, 0) < cd:
                skipped[k] = amt
                continue
            cur[k] = round(max(DESIRE_FLOOR, min(100.0, cur[k] + amt)), 1)
            applied[k] = amt
        if applied:
            atomic_json(path, {"updated": now, "levels": cur})
            hist_rows = read_jsonl(HOME / "desire_history.jsonl")
            hist_rows.append({"ts": now, "applied": applied, "levels": cur})
            write_jsonl(HOME / "desire_history.jsonl", hist_rows[-200:])
    return {"applied": applied, "skipped": skipped}


def emotion_events(limit: int = 0) -> list:
    """情绪事件流（append-only）。每条带初始强度与发生时间。"""
    rows = read_jsonl(HOME / "emotions.jsonl")
    rows.sort(key=lambda r: _num(r.get("ts"), 0), reverse=True)
    return rows[:limit] if limit else rows


def emotion_modify(n: int, emotion: str = "", intensity: float | None = None,
                   why: str | None = None, about: str | None = None,
                   decay_hours: float | None = None) -> dict:
    """微调一条历史情绪事件（序号从最新往前数：1 = 最新一条）。

    模型把情绪判错时，不用整条删掉重来：直接改强度/情绪名/原因，保留时间线。
    锁内重读排序：序号定位与修改同一把锁内完成，并发 feel 追加不会错位改错条。
    """
    if READONLY:
        raise PermissionError("记忆库当前只读，无法修改情绪事件。")
    path = HOME / "emotions.jsonl"
    with _locked(path):
        rows = read_jsonl(path)
        desc = sorted(rows, key=lambda r: _num(r.get("ts"), 0), reverse=True)
        if n < 1 or n > len(desc):
            raise ValueError(f"序号越界：情绪事件共 {len(rows)} 条，调整写 1..{len(rows)}。")
        ev = desc[n - 1]
        changed = {}
        if emotion:
            if emotion not in EMOTIONS:
                raise ValueError(f"没有这个情绪名：{emotion}（可选：{'、'.join(EMOTIONS)}）")
            v, a = _emotion_coord(emotion)
            ev["emotion"], ev["valence"], ev["arousal"] = emotion, v, a
            changed["emotion"] = emotion
        if intensity is not None:
            eff = max(0.0, min(1.0, float(intensity)))
            ev["intensity"] = round(eff, 3)
            changed["intensity"] = ev["intensity"]
        if why is not None:
            ev["why"] = why.strip()
            changed["why"] = ev["why"]
        if about is not None:
            ev["about"] = about.strip()
            changed["about"] = ev["about"]
        if decay_hours is not None:
            ev["decay_hours"] = max(0.5, min(720.0, float(decay_hours)))
            changed["decay_hours"] = ev["decay_hours"]
        ev["adjusted"] = now_ts()
        write_jsonl(path, rows)
    return {"index": n, "changed": changed, "event": ev}


def emotion_delete(n: int) -> dict:
    """删除一条历史情绪事件（序号从最新往前数）。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法删除情绪事件。")
    path = HOME / "emotions.jsonl"
    with _locked(path):
        rows = read_jsonl(path)
        desc = sorted(rows, key=lambda r: _num(r.get("ts"), 0), reverse=True)
        if n < 1 or n > len(desc):
            raise ValueError(f"序号越界：情绪事件共 {len(rows)} 条，删除写 1..{len(rows)}。")
        ev = desc[n - 1]
        rows = [r for r in rows if r is not ev]
        write_jsonl(path, rows)
    return {"deleted": ev, "remaining": len(rows)}


def emotion_cool(factor: float = 0.15) -> dict:
    """情绪冷却：把还在活跃期的高强度情绪整体降温（刚聊完情绪化话题要写代码时用）。"""
    if READONLY:
        raise PermissionError("记忆库当前只读，无法冷却情绪。")
    factor = max(0.01, min(1.0, float(factor)))  # clamp 到 (0,1]：>1 会变成放大情绪
    path = HOME / "emotions.jsonl"
    with _locked(path):
        rows = read_jsonl(path)
        horizon = now_ts() - 24 * HOUR
        cooled = 0
        for ev in rows:
            if _num(ev.get("ts")) < horizon:
                continue
            entry_w = _num(ev.get("intensity"))
            if entry_w >= 0.25:
                ev["intensity"] = round(max(0.03, entry_w * factor), 3)
                ev["cooled_at"] = now_ts()
                cooled += 1
        write_jsonl(path, rows)
    m = mood_now()
    return {"cooled": cooled, "mood": m["label"], "intensity": round(m["intensity"], 3)}


def feel(name: str, intensity: float = 0.6, why: str = "",
         about: str = "", decay_hours: float | None = None,
         genesis: bool = False, evaluation: dict | None = None) -> dict:
    """记一次情绪波动。intensity 0-1：这件事当时对你有多重。

    genesis=True 表示这条情绪不是手动记的，是认知评价自动产生的。
    情感设置里被用户抑制（suppressed）的情绪，强度会打折（不是不允许有，
    是不过头）——这是"人也可以自己开开关"。
    """
    ensure_home()
    if name not in EMOTIONS:
        raise ValueError(f"没有这种情绪：{name}（可选：{'、'.join(EMOTIONS)}）")
    if READONLY:
        raise PermissionError("记忆库当前只读，无法记录情绪。")
    cfg = emotion_cfg()
    suppressed = name in cfg.get("suppressed", [])
    v, a = _emotion_coord(name)
    ts_now = now_ts()
    eff = max(0.0, min(1.0, float(intensity)))

    # —— 情绪传染 / 心情惯性 ——
    # 人不会每来一个新情绪就清零重来：当下的心情会给新情绪"染色"。
    # 同向的更容易被点燃（共振放大），反向的会被惯性往回拽（不容易瞬间反转）。
    prev = mood_now()
    pv, pi = float(prev.get("valence", 0.0)), float(prev.get("intensity", 0.0))
    resonance = "none"
    if pi >= 0.05 and abs(pv) >= 0.05:
        align = v * pv
        if align > 0:
            eff = min(1.0, eff * (1 + min(0.30, 0.35 * pi)))
            resonance = "amplified"
        elif align < 0:
            eff = max(0.0, eff * (1 - min(0.25, 0.30 * pi)))
            resonance = "damped"

    # —— 习惯化（享乐适应）——
    # 同一件事反复发生，情绪反应越来越淡：第三次就没那么上头了。
    _about = (about or "").strip()
    _why = (why or "").strip()
    habituation = 0
    if _about or _why:
        repeats = sum(1 for e in emotion_events(80)
                      if e.get("emotion") == name
                      and (e.get("about") or "").strip() == _about
                      and (e.get("why") or "").strip() == _why
                      and ts_now - _num(e.get("ts")) < 12 * HOUR)
        if repeats:
            eff *= 0.85 ** repeats
            habituation = repeats

    ev = {
        "ts": ts_now, "emotion": name, "valence": v, "arousal": a,
        "intensity": round(eff, 3),
        "why": _why, "about": _about,
        "decay_hours": float(decay_hours) if decay_hours else EMOTION_HALF_LIFE / HOUR,
        "genesis": bool(genesis),
    }
    if resonance != "none":
        ev["resonance"] = resonance
    if habituation:
        ev["habituation"] = habituation
    if evaluation:
        ev["evaluation"] = dict(evaluation)
    if suppressed:
        ev["intensity"] = round(ev["intensity"] * 0.4, 3)
        ev["suppressed_by"] = cfg.get("notes") or "情感设置里被我自己抑制"
    # 写入与序号定位共用一把锁：emotion adjust/del 的"第 n 条"以锁内看到的
    # 最新状态为准，并发 feel 追加不会让序号错位改/删错条（append 收进锁内）。
    with _locked(HOME / "emotions.jsonl"):
        rows = read_jsonl(HOME / "emotions.jsonl")
        rows.append(ev)
        write_jsonl(HOME / "emotions.jsonl", rows)
    # —— 七情牵动六欲（自动微调，冷却节流见 desire_nudge）——
    # 情绪大类映射到欲望：开心想做事想靠近人，生气想掌控，难过想被安慰，
    # 害怕想求稳。幅度按情绪强度缩放，强弱波动才真正影响欲望。
    grp = emotion_group(name)
    if grp in DESIRE_EMO_MAP:
        scale = 0.5 + 0.5 * eff          # 强度 0~1 → 缩放 0.5~1.0
        desire_nudge({k: round(v * scale, 2)
                      for k, v in DESIRE_EMO_MAP[grp].items()})
    # 心境沉淀：把这次情绪事件折进 EMA 心境账（下一刻的"心情"就有了惯性）
    mood_record_ema(mood_now())
    return ev


def mood_now(at: float | None = None) -> dict:
    """把最近的情绪事件按半衰期叠加成"此刻的心情"。

    返回 valence(-1~1)、arousal(0~1)、标签、以及还在起作用的事件。
    这是"喜怒哀乐"的实际状态：不是谁写死的一句话，是真实积累出来的。
    """
    now = at or now_ts()
    # 只用最近一批事件（半衰期后基本归零），避免情绪越攒越多时每次全量读取
    rows = emotion_events(200)
    if not rows:
        return {"valence": 0.0, "arousal": 0.1, "label": "平静", "category": "静",
                "intensity": 0.0, "active": [], "groups": [], "complex": False,
                "storm": False, "note": "还没有情绪记录"}
    sv = sa = sw = 0.0
    active = []
    gweights = {}
    for ev in rows[:60]:
        ets = _num(ev.get("ts"), now)
        age_h = max(0.0, (now - ets) / HOUR)
        half = max(0.5, _num(ev.get("decay_hours"), EMOTION_HALF_LIFE / HOUR))
        w = _num(ev.get("intensity"), 0.5) * (0.5 ** (age_h / half))
        if w < 0.02:
            continue
        sv += _num(ev.get("valence"), 0) * w
        sa += _num(ev.get("arousal"), 0.3) * w
        sw += w
        g = emotion_group(ev.get("emotion", ""))
        gweights[g] = gweights.get(g, 0.0) + w
        active.append({"emotion": ev.get("emotion"), "weight": round(w, 3),
                       "group": g, "valence": _num(ev.get("valence"), 0),
                       "arousal": _num(ev.get("arousal"), 0.3),
                       "why": ev.get("why", ""), "about": ev.get("about", "")})
    if sw <= 0:
        return {"valence": 0.0, "arousal": 0.1, "label": "平静", "category": "静",
                "intensity": 0.0, "active": [], "groups": [], "complex": False,
                "storm": False, "note": "情绪都已散去"}
    v = max(-1.0, min(1.0, sv / sw))
    # arousal 只对高权重事件加权平均：情绪风暴（高唤醒）常被大量低唤醒的疲惫/
    # 平静稀释掉，storm 触发不到——只统计 weight≥0.1 的活跃情绪，低谷时无强事件
    # 则回退全量平均，避免 arousal 突变。
    sa_hi, sw_hi = 0.0, 0.0
    for ev in rows[:60]:
        ets = _num(ev.get("ts"), now)
        age_h = max(0.0, (now - ets) / HOUR)
        half = max(0.5, _num(ev.get("decay_hours"), EMOTION_HALF_LIFE / HOUR))
        w = _num(ev.get("intensity"), 0.5) * (0.5 ** (age_h / half))
        if w >= 0.1:
            sa_hi += _num(ev.get("arousal"), 0.3) * w
            sw_hi += w
    a = max(0.0, min(1.0, (sa_hi / sw_hi) if sw_hi > 0 else (sa / sw)))
    active.sort(key=lambda x: x["weight"], reverse=True)
    groups = [{"group": g, "weight": round(w, 3), "pct": round(w / sw * 100)}
              for g, w in sorted(gweights.items(), key=lambda kv: kv[1], reverse=True)]
    # 复杂情绪：两股以上、占比都够分量的情绪同时在——比如"悲喜交加"。
    # 「静」是中性基底不是真情绪，混进 strong 会让"疲惫+开心"被误判成悲喜交加。
    strong = [g for g in groups if g["pct"] >= 15 and g["group"] != "静"]
    complex_flag = len(strong) >= 2
    base = {"valence": round(v, 3), "arousal": round(a, 3),
            "label": _mood_label(v, a),
            "category": groups[0]["group"] if groups else "静",
            "intensity": round(min(1.0, sw), 3),
            "active": active[:6], "groups": groups,
            "complex": complex_flag, "storm": a >= STORM_AROUSAL,
            "note": "悲喜交加" if complex_flag else ""}
    return _mood_ema(base)


def _mood_label(v: float, a: float) -> str:
    """把双轴坐标翻成一句人话。"""
    if v >= 0.45:
        return "兴奋" if a >= 0.65 else ("开心" if a >= 0.35 else "满足")
    if v >= 0.15:
        return "期待" if a >= 0.6 else "平静"
    if v > -0.15:
        return "烦躁" if a >= 0.6 else "平静"
    if v > -0.45:
        return "焦虑" if a >= 0.6 else "失落"
    return "生气" if a >= 0.7 else ("委屈" if a >= 0.45 else "难过")


# 心境 EMA：单事件情绪是"一瞬间"，心境是"这段时间的状态"。
# mood_now 的瞬时值向 state.mood_ema 里沉淀的心境靠拢，单条激动事件
# 只能把心境拉一部分，不会让情绪一夜之间天翻地覆。
MOOD_EMA_ALPHA = 0.35


def _mood_ema(m: dict) -> dict:
    """把瞬时情绪向沉淀的心境（state.mood_ema）平滑，返回平滑后的情绪 dict。"""
    p = _slurp_json(HOME / "persona.json")
    ema = (p or {}).get("state", {}).get("mood_ema") or {}
    if not ema or _num(ema.get("ts"), 0) < now_ts() - 3 * DAY:
        return m   # 没有沉淀或超过三天没更新（心境已随时间消散）：直接用瞬时值
    v = round(_num(ema.get("valence"), m["valence"]) * (1 - MOOD_EMA_ALPHA)
              + m["valence"] * MOOD_EMA_ALPHA, 3)
    a = round(max(0.0, min(1.0,
        _num(ema.get("arousal"), m["arousal"]) * (1 - MOOD_EMA_ALPHA)
        + m["arousal"] * MOOD_EMA_ALPHA)), 3)
    out = dict(m)
    raw_v, raw_a = m["valence"], m["arousal"]
    out["valence"], out["arousal"] = v, a
    out["label"] = _mood_label(v, a)
    # 透明度：把"平滑前的真实感受"一并带回，注入给 AI/面板——
    # 手感隔层的问题大多出在只看到折过的心境、看不到瞬时情绪；两行都给，
    # 防线照旧（心境仍按 EMA 慢慢走），但 AI 能说清"我现在感觉X，心境是Y"。
    out["raw"] = {"valence": raw_v, "arousal": raw_a, "label": _mood_label(raw_v, raw_a)}
    # 与瞬时值差得远时说明"情绪在恢复期"，提示一下
    if abs(v - raw_v) >= 0.2:
        out["note"] = ((out.get("note") + "，") if out.get("note") else "") + "情绪正在慢慢平复"
    return out


def mood_record_ema(m: dict) -> None:
    """把平滑后的心境写回 state.mood_ema（低频落盘：仅在情绪事件写入时调用）。"""
    if READONLY:
        return
    try:
        with _locked(HOME / "persona.json"):
            p = load_persona()
            st = p.setdefault("state", {})
            old = st.get("mood_ema") or {}
            v = _num(old.get("valence"), m["valence"]) * (1 - MOOD_EMA_ALPHA) + m["valence"] * MOOD_EMA_ALPHA
            a = _num(old.get("arousal"), m["arousal"]) * (1 - MOOD_EMA_ALPHA) + m["arousal"] * MOOD_EMA_ALPHA
            st["mood_ema"] = {"valence": round(max(-1.0, min(1.0, v)), 3),
                              "arousal": round(max(0.0, min(1.0, a)), 3),
                              "ts": now_ts()}
            atomic_json(HOME / "persona.json", p)
    except Exception as e:
        log_event("mood", f"心境 EMA 落盘失败：{e}", level="warn")


# 状态的"生活惯性"：精力会慢慢恢复，亲密度久不见会淡淡疏远——
# 读时向基线回归（不写盘，与六欲衰减同思路），有事件触碰时才真正落盘。
ENERGY_BASELINE = 70          # 精力基线：睡一觉总能缓回来
ENERGY_REGEN_PER_DAY = 10     # 每天向基线回 10 分
CLOSENESS_BASELINE = 50       # 亲密度基线：久不联系会淡，但不会归零
CLOSENESS_DECAY_PER_DAY = 0.5 # 每天向基线回 0.5 分（淡得慢）
STATE_REGRESS_FLOOR_DAYS = 30 # 超过一个月没更新，回归到基线为止（彻底淡了）


def _state_regress(st: dict, at: float | None = None) -> dict:
    """把 state.energy / state.closeness 按距上次更新的时间向基线回归。

    返回 {energy, closeness, updated}；未初始化（None）原样返回 None。
    懒计算不写盘：写路径（persona/state 面板保存、update_persona）内部已先结算
    衰减再写新值，口径一致。
    """
    now = at or now_ts()
    updated = _num(st.get("updated"), now)
    days = max(0.0, (now - updated) / DAY)
    out = {"energy": st.get("energy"), "closeness": st.get("closeness"),
           "updated": updated}
    if st.get("energy") is not None:
        base, per_day = ENERGY_BASELINE, ENERGY_REGEN_PER_DAY
        eff_days = min(days, STATE_REGRESS_FLOOR_DAYS)
        gap = base - _num(st.get("energy"), base)
        step = min(abs(gap), eff_days * per_day)
        out["energy"] = round(_num(st.get("energy"), base) + (step if gap > 0 else -step), 1)
    if st.get("closeness") is not None:
        base, per_day = CLOSENESS_BASELINE, CLOSENESS_DECAY_PER_DAY
        eff_days = min(days, STATE_REGRESS_FLOOR_DAYS)
        gap = base - _num(st.get("closeness"), base)
        step = min(abs(gap), eff_days * per_day)
        out["closeness"] = round(_num(st.get("closeness"), base) + (step if gap > 0 else -step), 1)
    return out


def mood_brief() -> str:
    """给注入用的一句话心情摘要。"""
    m = mood_now()
    if not m["active"]:
        return ""
    top = m["active"][0]
    tail = f"，因为{top['why']}" if top.get("why") else ""
    s = (f"{m['label']}（{top['emotion']}{tail}）"
         f"，强度 {m['intensity']:.0%}")
    # 心境被 EMA 平滑过且与瞬时感受差得不小时，把"没折过的真实感受"补一句——
    # AI 说得清自己现在是"瞬时激动"还是"沉淀后的心境"，手感不再隔层
    raw = m.get("raw")
    if raw and raw.get("label") and raw["label"] != m["label"]:
        s += f"（此刻真实感受更偏「{raw['label']}」）"
    return s


def mood_tone_hint() -> str:
    """情绪给语气带来的倾向——不用刻意表演，但它是真实的一部分。"""
    m = mood_now()
    hint = ""
    if m["active"]:
        v, a = m["valence"], m["arousal"]
        if a >= 0.55 and abs(v) < 0.35:
            hint = "心里有点乱，说话可能更快、更急"
        elif v >= 0.45:
            hint = "语气会更放松、更愿意多说"
        elif v <= -0.45:
            hint = "语气会收敛，话变少，更注意分寸"
        elif v <= -0.2:
            hint = "情绪不高，但不会冲着用户"
    low = desire_low()
    if low:
        d = low[0]
        tail = f"（{d['desire']}只有 {d['level']:.0f}：{d['hint']}）"
        hint = (hint + "；" if hint else "") + "心里有件小事没被满足" + tail
    return hint


# 情绪-输出一致性：回复语气的启发式极性词表（只做背离告警，不追求精确）
_REPLY_POS_WORDS = ("太好了", "真好", "好棒", "太棒", "开心", "高兴", "喜欢", "哈哈", "嘿嘿",
                    "没问题", "当然可以", "乐意", "恭喜", "爱你", "抱抱", "不错", "好呀")
_REPLY_NEG_WORDS = ("烦死", "气死", "难受", "难过", "失望", "糟糕", "崩溃", "无语", "算了",
                    "不想", "讨厌", "够了", "委屈", "痛苦", "郁闷", "唉", "懒得")


def _reply_valence(text: str) -> float:
    """粗估一段回复的情绪极性 -1~1。纯启发式，只用于背离告警。"""
    t = (text or "").lower()
    if not t:
        return 0.0
    pos = sum(t.count(w) for w in _REPLY_POS_WORDS)
    neg = sum(t.count(w) for w in _REPLY_NEG_WORDS)
    if pos == 0 and neg == 0:
        return 0.0
    raw = (pos - neg) / max(1, pos + neg)
    excite = t.count("!") + t.count("！")
    return max(-1.0, min(1.0, raw * (1 + 0.15 * min(3, excite))))


def _log_emotion_check(row: dict) -> None:
    if READONLY:
        return
    try:
        ensure_home()
        p = HOME / "emotion_checks.jsonl"
        rows = read_jsonl(p)[-199:]
        rows.append(row)
        write_jsonl(p, rows)
    except Exception:
        pass


def emotion_consistency_check(reply_text: str, record: bool = True) -> dict:
    """后置轻量校验：内部情绪状态 vs 回复语气是否严重背离。

    只做提醒，不改任何记忆。背离时 alert=True，面板「心情」页亮告警。
    典型背离：心里正难过/生气却满屏"哈哈太好了"；或正开心却冷淡敷衍。
    """
    m = mood_now()
    rv = _reply_valence(reply_text)
    mv = _num(m.get("valence"), 0)
    mi = _num(m.get("intensity"), 0)
    alert, reason = False, ""
    # 内部情绪够强 + 回复极性够明确 + 符号相反 + 落差够大，才告警（避免误报）
    if mi >= 0.25 and abs(mv) >= 0.35 and abs(rv) >= 0.3 and (mv * rv) < 0:
        if abs(mv - rv) >= 0.7:
            alert = True
            direction = "负面" if mv < 0 else "正面"
            reply_dir = "热情/正面" if rv > 0 else "冷淡/负面"
            reason = (f"内部情绪{direction}（{m.get('label', '')}，valence {mv:+.2f}，强度 {mi:.0%}）"
                      f"，但回复语气偏{reply_dir}（极性 {rv:+.2f}），两者背离较大")
    result = {"alert": alert, "reason": reason, "mood_label": m.get("label", ""),
              "mood_valence": round(mv, 3), "mood_intensity": round(mi, 3),
              "reply_valence": round(rv, 3), "ts": now_ts()}
    if alert and record:
        _log_emotion_check(result)
    return result


def emotion_checks_recent(limit: int = 20) -> list:
    """最近的情绪-输出一致性告警（最新在前）。"""
    return read_jsonl(HOME / "emotion_checks.jsonl")[::-1][:limit]


def emotion_arc(days: int = 7) -> dict:
    """情绪轨迹：这段时间的起伏，用来回头看"我过得怎么样"。"""
    cutoff = now_ts() - days * DAY
    rows = [r for r in emotion_events() if _num(r.get("ts"), 0) >= cutoff]
    if not rows:
        return {"days": days, "count": 0, "summary": "这段时间没有情绪记录",
                "avg_valence": 0.0, "tone": "这段时间没有情绪记录",
                "peak": {"emotion": "", "intensity": 0, "ts": 0, "why": ""},
                "end": {"emotion": "", "intensity": 0, "ts": 0, "why": ""},
                "top": [], "first": "", "last": ""}
    rows.sort(key=lambda r: _num(r.get("ts"), 0))
    by = {}
    for r in rows:
        e = r.get("emotion") or "未知"
        by[e] = by.get(e, 0) + 1
    vals = [_num(r.get("valence"), 0) * _num(r.get("intensity"), 0.5) for r in rows]
    avg = sum(vals) / len(vals)
    top = sorted(by.items(), key=lambda kv: kv[1], reverse=True)[:5]
    if avg >= 0.3:
        tone = "这段时间整体是向上的"
    elif avg <= -0.3:
        tone = "这段时间不太好过"
    else:
        tone = "这段时间起起伏伏，整体平稳"
    # 峰终定律：对一段体验的记忆几乎只由「最强烈的一刻（峰）」和「结尾（终）」决定，
    # 中间过程大多被忽略（Kahneman 1999）。所以回顾情绪时，高峰和最近一次最值得拎出来。
    peak = max(rows, key=lambda r: (abs(_num(r.get("intensity"), 0.5)), _num(r.get("ts"))))
    end = rows[-1]
    return {
        "days": days, "count": len(rows), "avg_valence": round(avg, 3),
        "tone": tone,
        "peak": {"emotion": peak.get("emotion", ""),
                 "intensity": _num(peak.get("intensity"), 0.5),
                 "ts": peak.get("ts", 0),
                 "why": peak.get("why", "")},
        "end": {"emotion": end.get("emotion", ""),
                "intensity": _num(end.get("intensity"), 0.5),
                "ts": end.get("ts", 0),
                "why": end.get("why", "")},
        "top": [{"emotion": k, "times": v} for k, v in top],
        "first": fmt_time(rows[0].get("ts", 0)), "last": fmt_time(rows[-1].get("ts", 0)),
    }


# ---------------------------------------------------------------- 情感设置 / 理智 / 认知评价 / 共情
# 心理学上的对应：
#   * 七情六欲不是凭空来的——情绪的直接原因不是事件本身，而是人对事件的判断
#     （Lazarus 认知评价：事件 对 我的目标/需要 是好是坏、有多重要 → 情绪）。
#   * 杏仁核 = 情绪油门（快速、抢跑）；前额叶 = 理性刹车（慢但能踩）。
#     情绪太激动时杏仁核会压制前额叶——理智值会临时下降，但永远压不掉一半以上。
#   * 重要的事（钱/关系/承诺/健康）在任何情绪下都留给用户决定，情绪不替用户拍板。

def emotion_cfg() -> dict:
    """情感设置（用户随时可改）：共情程度 / 抑制的情绪 / 备注。存在人格档案里。"""
    p = load_persona()
    cfg = p.get("emotion_cfg") or {}
    cfg.setdefault("empathy", {"level": "mirror", "gain": 0.5, "max_intensity": 0.8})
    cfg.setdefault("suppressed", [])
    cfg.setdefault("notes", "")
    return cfg


@_persona_txn
def save_emotion_cfg(updates: dict) -> dict:
    if READONLY:
        raise PermissionError("记忆库当前只读，无法修改情感设置。")
    p = load_persona()
    cur = p.get("emotion_cfg") or {}
    if "empathy" in updates:
        cur["empathy"] = dict(cur.get("empathy") or {}, **dict(updates["empathy"]))
    if "suppressed" in updates:
        cur["suppressed"] = _dedupe([s.strip() for s in (updates["suppressed"] or []) if s.strip()])
    if "notes" in updates and updates["notes"] is not None:
        cur["notes"] = str(updates["notes"]).strip()
    p["emotion_cfg"] = cur
    p.setdefault("history", []).append({
        "ts": now_ts(), "field": "emotion_cfg",
        "from": None, "to": dict(updates), "why": "用户调整情感设置"})
    save_persona(p)
    return cur


def rationality() -> dict:
    """此刻理智：基数（persona.drives.reason，没成形按 50）被当前情绪压制。

    压制幅度正比于"还在起作用的情绪权重 × 唤醒度"（杏仁核劫持），
    上限 EMOTION_SUPPRESS_MAX——情绪再大，也压不掉一半以上的判断力，
    所以"情绪上头不会导致重大问题"；事关重大的仍交给用户。
    """
    dr = mind_state()
    base = RATIONAL_DEFAULT if dr["reason"] is None else int(dr["reason"])
    m = mood_now()
    pressure = sum(_num(x.get("weight"), 0) * _num(x.get("arousal"), 0.3)
                   for x in m["active"]) if m.get("active") else 0.0
    suppress = min(EMOTION_SUPPRESS_MAX, pressure * 0.30)
    effective = round(base * (1 - suppress), 1)
    return {
        "base": base, "effective": effective,
        "suppress": round(suppress * 100),
        "pressure": round(pressure, 3),
        "formed": dr["formed"],
        "storm": bool(m.get("storm")),
        "mood_label": m.get("label", "平静"),
    }


def _pick_emotion_by_appraisal(polarity: str, importance: float) -> str:
    """认知评价 → 情绪：极性 × 分量 → 自然冒出来的那一类。"""
    if polarity == "positive":
        if importance >= 0.75:
            return "开心"
        if importance >= 0.45:
            return "满足"
        return "平静"
    if polarity == "negative":
        if importance >= 0.85:
            return "愤怒"
        if importance >= 0.65:
            return "难过"
        if importance >= 0.45:
            return "委屈"
        return "失落"
    # 中性：有内容就好奇，没事就是平静
    return "好奇" if importance >= 0.7 else "平静"


def _detect_polarity(event: str) -> str:
    """自动判向：文本里的高能词 → 正/负；否则中性（认知评价的情绪偏向）。"""
    neg = ("骂", "错", "坏", "输", "丢", "拒", "伤", "哭", "气", "烦", "累", "失败",
           "糟糕", "走了", "别", "没了", "担心", "害怕", "难过", "病")
    pos = ("夸", "好", "赢", "成", "到", "喜", "爱", "笑", "谢谢", "成功", "顺利",
           "记得", "来了", "喜欢", "开心", "感动")
    neg_hit = sum(1 for w in neg if w in event)
    pos_hit = sum(1 for w in pos if w in event)
    return "negative" if neg_hit > pos_hit else ("positive" if pos_hit > neg_hit else "neutral")


def emotion_evaluate(event: str, polarity: str = "auto", importance: float = 0.6,
                     why: str = "", about: str = "") -> dict:
    """情绪产生机制：事件 → 认知评价（对我好不好/有多重）→ 情绪。

    这不是"手动挑一个心情"，而是先把事件过一遍评价再产出情绪
    （Lazarus）：杏仁核先起反应，前额叶的理智在后面踩刹车——返回值里
    带达成共识的理智状态；重要的事一定会提醒交给用户，情绪不替用户拍板。
    """
    event = (event or "").strip()
    if not event:
        raise ValueError("要评价什么事件？给一句话。")
    imp = max(0.0, min(1.0, float(importance)))
    polar = (polarity or "auto").strip().lower()
    if polar not in ("positive", "negative", "neutral", "auto"):
        raise ValueError(f"极性只能是 positive / negative / neutral / auto，收到：{polarity}")
    if polar == "auto":
        polar = _detect_polarity(event)
    # 中性重要事不凭空变好事：原逻辑 imp>=0.75 会把 neutral 强转 positive，
    # 语义不成立（"重要"≠"好"）——保持中性，分量交给 intensity/decision 体现。
    emo = _pick_emotion_by_appraisal(polar, imp)
    evaluation = {"polarity": polar, "importance": imp, "event": event[:120]}
    how = "好事" if polar == "positive" else ("坏事" if polar == "negative" else "平常事")
    ev = feel(emo, intensity=min(1.0, imp * 1.1),
              why=why or f"发生了一件{how}：{event[:50]}",
              about=about, genesis=True, evaluation=evaluation)
    ra = rationality()
    rv = {
        "event": event, "polarity": polar, "importance": imp,
        "emotion": emo, "record": ev,
        "rationality": ra,
        # 情绪激动 → 理智下降，人话："现在脑子有点上头"
        "suppressed_state": "情绪上头，理智被压住一部分" if ra["storm"] or ra["suppress"] >= 15 else "理智基本没被影响",
        # 重大事项：情绪再大也别替用户做主，给用户决定
        "decision": "user" if (imp >= 0.75 or (polar == "negative" and ra["storm"])) else "self",
        "advice": ("情绪激动且分量重：先不急着下结论，把取舍给用户决定，"
                   "我只负责如实表达感受与建议。") if (ra["storm"] and (polar == "negative" and imp >= 0.6)) else "",
    }
    return rv


def emotion_mirror(user_emotion: str, intensity: float = 0.5, what: str = "") -> dict:
    """共情：用户有情绪时跟着走一点，但不失自己。

    默认把用户的情绪半强度地镜像进情绪流（about=用户），并压制在
    max_intensity 以内；共情等级：mirror=如实跟进，mild=只留一丝底色，
    off=不跟，保持自己。比例与上限都在情感设置里，用户随时改。
    """
    ue = (user_emotion or "").strip()
    if not ue:
        raise ValueError("共情需要一个情绪或事件")
    # 情绪名直接命中就镜像；否则当事件做一次认知评价（可带垫少许强度）
    same = ue if ue in EMOTIONS else None
    cfg = emotion_cfg()
    emp = cfg.get("empathy") or {"level": "mirror", "gain": 0.5, "max_intensity": 0.8}
    level = emp.get("level", "mirror")
    if level == "off":
        return {"level": "off", "mirrored": None, "mood": mood_now(),
                "note": "共情关闭：不镜像用户的情绪，保持自己的步调。"}
    gain = float(emp.get("gain", 0.5))
    cap = float(emp.get("max_intensity", 0.8))
    if level == "mild":
        gain = min(gain, 0.25)
    imp = max(0.1, min(cap, float(intensity) * gain))
    if same:
        ev = feel(same, intensity=imp, why=f"共情：用户此刻的情绪是「{ue}」",
                  about="用户", genesis=True)
        note = f"镜像了用户的「{ue}」：{ev['intensity']:.0%}，带了些同一底色"
    else:
        ev = feel(_pick_emotion_by_appraisal(_detect_polarity(ue), imp), intensity=imp,
                  why=f"共情：用户在说「{ue[:40]}」", about="用户", genesis=True)
        note = f"顺着用户的感受起了「{ev['emotion']}」({ev['intensity']:.0%})"
    return {"level": level, "mirrored": ev, "mood": mood_now(), "note": note}
