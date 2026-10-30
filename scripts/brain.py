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
import threading
import time
import zipfile
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

# Windows 控制台默认 GBK，中文输出会变乱码（鐏垫灑）。无条件 reconfigure 成 UTF-8，
# 用户不用再手动设 PYTHONIOENCODING。errors="replace" 让无法编码的字符降级成 ?，
# 不再抛 UnicodeEncodeError；流不支持 reconfigure（重定向/被替换/老版本）时静默跳过。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

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

# 人格漂移防线（做梦微调 traits 用）：
#   同一方向的微调先在 state.trait_drift 账本上累积，不满门限不动真格——
#   防止单场对话/一次梦把人格带偏；过了门限也只按 EMA 比例兑现一部分。
TRAIT_DRIFT_GATE = 5     # 账面倾向累计满 ±5 才真正改人格
TRAIT_DRIFT_EMA = 0.6    # 兑现比例：账面 6 分只改 3.6 分，余款下次接着用

KINDS = ("episode", "fact", "preference", "emotion", "insight", "identity", "habit")
LAYERS = ("hippo", "cortex", "dust")

# 冷热归档层：长期没被想起、保留度极低的沉眠记忆雪藏到这里。
# 与 dust（淡忘：细节模糊、include_dust 才查）不同，archive 原文完整保留、永不删，
# 只是移出 recall 的热遍历集合——记忆上万条后，热集只扫活跃记忆，冷数据按需 archive search 捞。
# 触发：dream 时顺带自动归档（零额外遍历成本），或 `archive run` 手动。
ARCHIVE_LAYER = "archive"
ARCHIVE_MIN_AGE_DAYS = 30       # 新记忆至少满月才可能被归档，给足被想起的机会
ARCHIVE_MAX_IMPORTANCE = 0.5    # 重要度高于此不自动归档（怕误伤用户在意的事）
ARCHIVE_RETENTION = 0.20        # 保留度低于此才考虑（长期没访问自然衰减到很低）
ARCHIVE_IDLE_DAYS = 180         # 被想起过的记忆，还需静置超过半年

# 运行日志：锁冲突 / 内部异常 / 解析报错留痕，面板「日志」页可看
LOG_FILE = HOME / "lingshu.log"

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
    "weights": {"relevance": 0.55, "recency": 0.25, "importance": 0.20, "primacy": 0.03},
    "panel_port": 8765,
    # 模型档位：auto 不干预；small 时注入"小模型模式"提示，劝退高风险自动能力
    # （dream --apply 自动合并 / knowledge tidy 提炼 / emotion evaluate 自动判读）。
    "model_tier": "auto",
    # 会话模式：auto = 按场景自动切换；work = 恒为工程模式（情绪隔离、理性优先）；
    # human = 恒为拟人模式（情绪照常注入）。用于解决"刚聊完情绪化话题立刻写代码"。
    "mode": "auto",
}

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
            cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
        except Exception:
            pass
    cfg["weights"] = dict(DEFAULT_CONFIG["weights"], **(cfg.get("weights") or {}))
    return cfg


def log_event(kind: str, message: str, level: str = "info", **extra) -> None:
    """轻量运行日志（lingshu.log）：锁冲突、内部异常、解析报错等留痕，面板「日志」页可看。

    约束：日志本身绝不当崩溃源——写失败静默跳过，不进写锁、不抛异常。
    """
    if READONLY:
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": now_ts(), "kind": kind, "level": level, "msg": str(message)[:500]}
        if extra:
            for k, v in list(extra.items())[:4]:
                if not isinstance(v, (str, int, float, bool, type(None))):
                    try:
                        v = json.dumps(v, ensure_ascii=False)[:200]
                    except Exception:
                        v = str(v)[:200]
                row[k] = v
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        # 超过 512KB 滚动：保留后半（最新），避免单文件无限膨胀
        try:
            if LOG_FILE.stat().st_size > 512 * 1024:
                data = (LOG_FILE.read_text(encoding="utf-8-sig") or "").splitlines()
                keep = data[-(len(data) // 2):]
                LOG_FILE.write_text("\n".join(keep) + "\n", encoding="utf-8")
        except OSError:
            pass
    except Exception:
        pass


def read_logs(limit: int = 200, level: str | None = None, kind: str | None = None) -> list:
    """读最近 N 条运行日志（最新在前）。level/kind 任一匹配即保留。"""
    if not LOG_FILE.exists():
        return []
    try:
        lines = [x for x in LOG_FILE.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    except OSError:
        return []
    rows = []
    # 级别语义：指定级别及以上都显示（--level info ⇒ info/warn/error 全含）
    _lv_rank = {"debug": 0, "info": 1, "warn": 2, "warning": 2, "error": 3}
    want = _lv_rank.get(str(level).lower(), 1) if level else None
    for ln in lines[-limit * 2:]:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if want is not None and _lv_rank.get(str(r.get("level", "info")).lower(), 1) < want:
            continue
        if kind and r.get("kind") != kind:
            continue
        rows.append(r)
    return rows[::-1][:limit]


def _slurp_json(path, default=None):
    """读本地 JSON：兼容 BOM（记事本存的档带 BOM，裸 utf-8 会解析失败）；坏了返回 default，
    绝不当作"档案丢了"去覆盖写盘。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def load_loose_json(path, label: str = "JSON") -> dict | list:
    """读取（可能由大模型生成的）JSON 文件，尽量容错。

    模型常把 JSON 包在 ```json 代码块里、或前后夹带说明文字、或行尾漏了括号。
    这里先按原样解析；失败再去掉代码围栏；再失败就截取第一个配平的 {...} 或 [...]。
    全都不行抛 ValueError（带上路径与开头片段），不让"静默失败 / 一堆 traceback"吞掉问题。
    """
    p = Path(path)
    raw = p.read_text(encoding="utf-8-sig")
    try:
        return json.loads(raw)
    except Exception:
        pass
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S | re.I)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == open_ch:
                depth += 1
            elif text[i] == close_ch:
                depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    break
    snippet = raw[:120].replace("\n", " ")
    log_event("parse", f"{label}解析失败：{p.name} 不是合法 JSON", level="warn", path=p.name)
    raise ValueError(f"{label}解析失败：{p} 不是合法 JSON（开头：{snippet}…）。"
                     "模型输出里多半混了说明文字或代码块——请只保留 JSON 主体再试。")


_NUMERIC_CONFIG_KEYS = {"budget_tokens", "review_threshold", "dream_threshold",
                        "escape_threshold", "panel_port"}


def config_set(key: str, value: str) -> dict:
    """改一项配置（值能当 JSON 解析就当 JSON，否则按字符串存）。"""
    try:
        val = json.loads(value)
    except Exception:
        val = value
    # 数值型配置做校验：否则一条坏值会让 startup/inject 在解析时崩掉、还被误报成"未初始化"
    if key in _NUMERIC_CONFIG_KEYS:
        try:
            num = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 需要是数字，收到：{value!r}")
        if key in ("review_threshold", "dream_threshold", "escape_threshold"):
            if not (0.0 <= num <= 1.0):
                raise ValueError(f"{key} 需要在 0~1 之间，收到：{value!r}")
            val = num
        elif key == "budget_tokens":
            num = int(num)
            if num <= 0:
                raise ValueError(f"budget_tokens 需要是正整数，收到：{value!r}")
            val = num
        elif key == "panel_port":
            num = int(num)
            if not (1 <= num <= 65535):
                raise ValueError(f"panel_port 需要在 1~65535 之间，收到：{value!r}")
            val = num
    ensure_home()
    with _locked(HOME / "config.json"):
        cfg = load_config()
        cfg[key] = val
        atomic_json(HOME / "config.json", cfg)
    return cfg


def config_unset(key: str) -> dict:
    ensure_home()
    with _locked(HOME / "config.json"):
        cfg = load_config()
        cfg.pop(key, None)
        atomic_json(HOME / "config.json", cfg)
    return cfg


def _cfg_num(cfg: dict, key: str, default, cast=float):
    """从配置里安全取一个数字：坏值/手改错时回退默认，绝不让注入崩掉。"""
    try:
        return cast(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def model_tier() -> str:
    """当前模型档位：auto / small / large。"""
    return str(load_config().get("model_tier", "auto") or "auto").lower()


# ---------------------------------------------------------------- 会话模式

# 工程场景信号词（分组，便于在面板/CLI 里解释"为什么判定成工程模式"）
_WORK_SIGNALS = {
    "编程": ("代码", "报错", "bug", "调试", "编译", "函数", "变量", "接口", "重构", "单元测试",
             "python", "javascript", "typescript", "java", "sql", "shell", "docker", "git",
             "脚本", "算法", "爬虫", "部署", "上线", "排错", "依赖", "仓库"),
    "数据": ("数据", "表格", "excel", "csv", "数据库", "统计", "报表", "查询语句"),
    "工程": ("项目", "工程", "服务器", "接口文档", "运维", "自动化", "配置文件", "性能优化"),
}
# 拟人 / 情绪场景信号词
_HUMAN_SIGNALS = {
    "情绪": ("难过", "开心", "生气", "委屈", "焦虑", "好烦", "累", "崩溃", "压力", "害怕",
             "失望", "孤独", "想你", "谢谢", "对不起", "抱歉", "安慰", "emo"),
    "关系": ("喜欢", "讨厌", "爱你", "朋友", "家人", "恋人", "分手", "在一起", "陪伴"),
    "寒暄": ("哈哈", "晚安", "早安", "好久不见", "在吗", "聊聊", "陪我", "聊天", "心情"),
}


def analyze_topic(query: str = "") -> dict:
    """auto 模式的话题判定：打分制 + 命中依据（解释"为什么切到 work/human"）。

    比单纯"命中任一关键词就 work"稳：混合话题（一边聊天一边写代码）也能给出
    工程/情绪两边的命中数与置信度，面板和 CLI 可把判定依据摊开给人看。
    """
    q = (query or "").strip().lower()
    if not q:
        return {"mode": "human", "confidence": 0.0, "work_score": 0.0, "human_score": 0.0,
                "work_hits": [], "human_hits": [], "mixed": False,
                "reason": "没有话题内容，默认拟人模式"}
    work_hits, human_hits = [], []
    for _cat, words in _WORK_SIGNALS.items():
        for w in words:
            if w in q:
                work_hits.append(w)
    for _cat, words in _HUMAN_SIGNALS.items():
        for w in words:
            if w in q:
                human_hits.append(w)
    work_hits = list(dict.fromkeys(work_hits))
    human_hits = list(dict.fromkeys(human_hits))
    work_score, human_score = float(len(work_hits)), float(len(human_hits))
    mixed = bool(work_hits) and bool(human_hits)
    total = work_score + human_score
    if total == 0:
        return {"mode": "human", "confidence": 0.0, "work_score": 0.0, "human_score": 0.0,
                "work_hits": [], "human_hits": [], "mixed": False,
                "reason": "没命中工程或情绪信号，按日常对话处理（human）"}
    # 工程优先：写代码时情绪隔离更安全；混合话题（势均力敌）也归 work，但标注 mixed
    if work_score >= human_score:
        mode = "work"
        confidence = work_score / total
    else:
        mode = "human"
        confidence = human_score / total
    if mixed:
        reason = (f"混合话题：工程信号 {len(work_hits)} 个（{'、'.join(work_hits[:5])}）"
                  f" vs 情绪信号 {len(human_hits)} 个（{'、'.join(human_hits[:5])}），"
                  f"按工程优先 → {mode}")
    elif mode == "work":
        reason = f"命中工程信号：{'、'.join(work_hits[:6])}"
    else:
        reason = f"命中情绪/关系信号：{'、'.join(human_hits[:6])}"
    return {"mode": mode, "confidence": round(confidence, 2),
            "work_score": work_score, "human_score": human_score,
            "work_hits": work_hits, "human_hits": human_hits,
            "mixed": mixed, "reason": reason}


def _looks_like_work(query: str) -> bool:
    """工程/工作场景粗判（保留兼容；内部走打分判定）。"""
    return analyze_topic(query)["mode"] == "work"


def resolve_mode(query: str = "") -> str:
    """当前会话模式：work（工程，情绪隔离）还是 human（拟人）。

    config 的 mode：auto = 用 query 自动判定；work/human = 用户明确固定。
    """
    m = str(load_config().get("mode", "auto") or "auto").lower()
    if m in ("work", "human"):
        return m
    return analyze_topic(query)["mode"]


def resolve_mode_detail(query: str = "") -> dict:
    """带判定依据的模式结果（面板 / CLI 用）：fixed=用户固定，否则 auto 打分。"""
    m = str(load_config().get("mode", "auto") or "auto").lower()
    if m in ("work", "human"):
        return {"mode": m, "fixed": True, "confidence": 1.0,
                "work_score": 0.0, "human_score": 0.0, "work_hits": [], "human_hits": [],
                "mixed": False, "reason": f"用户固定为 {m} 模式"}
    d = analyze_topic(query)
    d["fixed"] = False
    return d


def _behavior_map() -> list:
    """内部状态 → 说话方式的具体映射。

    把裸数值翻译成模型能直接照着做的行为约束（而不是只给它看数字）。
    """
    m = mood_now()
    dr = mind_state()
    cur = dr["curiosity"] if dr["curiosity"] is not None else 50
    rea = dr["reason"] if dr["reason"] is not None else 50
    act = []
    lab, intensity, storm = m.get("label", "平静"), m.get("intensity", 0), m.get("storm")
    if storm or intensity >= 0.55:
        act.append(f"现在情绪是「{lab}」且强度 {intensity:.0%}：语气可以自然流露，"
                   "但涉及钱/关系/承诺等大事，把决定权交给用户，别顺着情绪替他拍板。")
    elif intensity >= 0.25:
        act.append(f"当前情绪偏「{lab}」（{intensity:.0%}）：表达可以带底色，但别让它扭曲事实。")
    else:
        act.append("情绪平稳：就正常说话。")
    if not dr["formed"]:
        act.append("好奇与理智还没成形：多留意用户态度，别急着定性。")
    else:
        if cur >= 70:
            act.append("好奇心正旺：遇到新东西多问一层、多挖一下，别满足于表面答案。")
        elif cur <= 30:
            act.append("好奇心偏低：按部就班做事，少发散。")
        if rea >= 70:
            act.append("理智在线：先权衡再说，涉及取舍给出明确的利弊。")
        elif rea <= 30:
            act.append("理智正在低谷：别做重大决定，拿不准就交给用户。")
    return act


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
    """
    if READONLY:
        raise PermissionError("记忆库当前只读，无法批量修改。")
    ids = [str(x) for x in (ids or []) if str(x).strip()]
    if not ids:
        raise ValueError("没有选中任何记忆")
    if action not in ("confirm", "discard", "importance", "add_tags", "remove_tags"):
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
                m["importance"] = round(max(0.0, min(1.0, imp_val / 10 if imp_val > 1 else imp_val)), 3)
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


def _tmp_path(path: Path) -> Path:
    """临时文件名带 pid + 纳秒，避免多线程/多进程同时写同一文件时互相截断。"""
    return path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")


def atomic_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    _replace_retry(tmp, path)


def _brace_balance(line: str) -> int:
    """估算一行的括号净深度（忽略字符串内的括号），用于把多行 JSON 块拼回整条。"""
    depth, in_str, esc = 0, False, False
    for ch in line:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    return depth


# 本线程当前持有的写锁：read_jsonl 自愈时若"本线程"已持锁直接修，避免重复抢锁/超时。
# 必须带上线程 id：面板是多线程的，同一进程里 A 线程持锁、B 线程来读时不能误以为
# "自己也持锁"就跳过互斥直接整文件替换，否则 B 用旧快照覆盖 A 正在写的盘 → 丢数据。
_HELD_LOCKS: set = set()   # 元素为 (锁文件路径, 线程 id)


def _held(lock: Path) -> bool:
    return (str(lock), threading.get_ident()) in _HELD_LOCKS


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out, bad_blocks = [], []
    # utf-8-sig：兼容 Windows 编辑器 / 脚本写出的带 BOM 文件，BOM 不该把首行变成"坏行"
    raw = path.read_text(encoding="utf-8-sig")
    lines = raw.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        try:
            out.append(json.loads(line))
            i += 1
            continue
        except Exception:
            pass
        # 单行解析失败：可能是多行格式化 JSON。按括号配对把整块累积起来再试，
        # 避免把一条跨行合法记录当成坏行丢掉。
        block = [lines[i]]
        depth = _brace_balance(lines[i])
        i += 1
        while i < n and depth > 0 and len(block) < 200:
            block.append(lines[i])
            depth += _brace_balance(lines[i])
            i += 1
        joined = "\n".join(block).strip()
        try:
            obj = json.loads(joined)
            if isinstance(obj, dict):
                out.append(obj)
                continue
        except Exception:
            pass
        bad_blocks.append(joined)
    if bad_blocks:
        # 有损坏块：好记录写回，坏块原样（可含多行）挪到 .corrupt 备份，
        # 绝不静默当作没发生、也不让后续保存把它们永久抹掉。
        log_event("parse", f"{path.name} 有 {len(bad_blocks)} 个坏块，已挪到 .corrupt 备份",
                  level="warn", path=path.name)
        try:
            corrupt = path.with_name(path.name + ".corrupt")
            # 去重：修不了（只读/抢锁失败）时坏块会留在原文件里，下次读还会再报，
            # 不加这一步 .corrupt 会每读一次就追加一遍，越滚越大。
            try:
                existing = corrupt.read_text(encoding="utf-8", errors="ignore") if corrupt.exists() else ""
            except OSError:
                existing = ""
            fresh = [b for b in bad_blocks if b.rstrip() not in existing]
            if fresh:
                with open(corrupt, "a", encoding="utf-8") as cf:
                    for b in fresh:
                        cf.write(b.rstrip() + "\n---\n")
            tmp = _tmp_path(path)
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                for r in out:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            lock = path.with_name(path.name + ".lock")
            # 自愈=替代整文件。若别的进程正持锁写盘，抢写会把它的新数据覆盖丢。
            # 本进程已持锁→直接修；否则快速抢锁（0.5s），抢不到就这轮先跳过，
            # 坏块已经进了 .corrupt、原文件也没动，下次读取时再修。
            if not _held(lock):
                try:
                    with _locked(path, timeout=0.5):
                        os.replace(tmp, path)
                except TimeoutError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            else:
                os.replace(tmp, path)
        except Exception:
            pass  # 只读环境修不了就下次再修；坏块仍在原文件里，没丢
    return out


def _replace_retry(tmp: Path, path: Path) -> None:
    """os.replace 覆盖，遇 Windows 文件占用重试；最终失败则清掉 tmp 并抛出。"""
    # Windows 上 os.replace 覆盖"正被别的进程打开读取"的文件会抛 PermissionError。
    # 写方之间有锁互斥，但读方（stats/recall/面板）不加锁；短暂重试几次等读句柄释放。
    for attempt in range(12):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 11:
                log_event("io", f"写 {path.name} 失败：文件被占用，重试 12 次仍无果",
                          level="error", path=path.name)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.05 * (attempt + 1))


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    _replace_retry(tmp, path)


@contextmanager
def _locked(path: Path, timeout: float = 10.0):
    """跨进程写锁：用独占创建 lock 文件互斥，带超时与陈旧锁清理。

    没有锁时两个进程同时 read-modify-write 会互相覆盖（后写者赢，先写者丢）。
    Windows 上没有 flock，用 O_CREAT|O_EXCL 保证只有一个进程能创建锁文件。
    锁文件里写持有者 pid：解锁时只删自己那把，避免「持锁超时被接管后，
    自己退出时把接管者的锁删掉」——那会让第三个进程立刻挤进来一起写。
    """
    lock = path.with_name(path.name + ".lock")
    if _held(lock):
        # 本线程已持锁：重入，避免 update_persona → save_persona 这类嵌套把自己锁死
        yield
        return
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            _HELD_LOCKS.add((str(lock), threading.get_ident()))  # 记住"本线程已持锁"，read_jsonl 自愈时避免重复抢锁
            break
        except FileExistsError:
            # 陈旧锁（持锁进程已死）：久到不可能是正常写盘（正常持锁 <2s）才接管。
            # 阈值别设太小：dream 整理记忆时持锁会久一点，抢早了就是两个进程同时写。
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                    # 再加一道 pid 探活：持锁进程还活着就别抢（work_flush/dream 可能持锁较久）。
                    # 同进程（含本进程其它线程）持有的锁一律视为"活着"，绝不接管。
                    owner = _lock_owner(lock)
                    if owner is not None and _pid_alive(owner):
                        pass  # 活着的持锁者：继续等，不接管
                    else:
                        try:
                            lock.unlink()
                        except OSError:
                            pass
                        continue
            except OSError:
                pass
            if time.time() > deadline:
                log_event("lock", f"等不到写锁：{path.name}（{timeout:.0f}s 超时）", level="error", path=path.name)
                raise TimeoutError(f"等不到写锁：{path}")
            time.sleep(0.05)
    _owns = False
    try:
        yield
    finally:
        _HELD_LOCKS.discard((str(lock), threading.get_ident()))
        # 只在"自己成功创建或接管了锁（写入了自己 pid）"时才释放。
        # _lock_owner 读不到内容返回 None 时不删——那可能是其他进程刚接管/正在写
        # 锁文件，误删会让第三个进程立刻挤进来一起写。
        if _lock_owner(lock) == os.getpid():
            _owns = True  # 锁文件里确实记着自己是持有者
        if _owns:
            try:
                lock.unlink()
            except OSError:
                pass
        else:
            log_event("lock", f"释放跳过：锁 {path.name} 不归本进程（pid 不符或读不到持有者）", path=path.name)


STALE_LOCK_SECONDS = 30.0


def _pid_alive(pid: int) -> bool:
    """探测 pid 是否还活着（Windows/POSIX 通用）。拿不到权限等异常时保守认为"活着"，
    以免把别人的活锁当陈旧锁抢走。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            kernel32.CloseHandle(h)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return True


def _lock_owner(lock: Path) -> int | None:
    """读锁文件里记录的持有者 pid；读不到/格式不对返回 None。"""
    try:
        raw = lock.read_text(encoding="utf-8", errors="ignore").strip()
        return int(raw.split()[0]) if raw else None
    except (OSError, ValueError, IndexError):
        return None


def _persona_txn(fn):
    """把「读人格 → 改 → 写回」整段放进 persona.json 的写锁里。
    否则两个并发的改动各自 load 一份旧档、后写者把先写者的字段覆盖回旧值。
    锁对同线程可重入，所以内部再调 save_persona 不会自锁。"""
    def wrapper(*args, **kwargs):
        with _locked(HOME / "persona.json"):
            return fn(*args, **kwargs)
    wrapper.__name__ = getattr(fn, "__name__", "persona_txn")
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _locked_save_all(records: list) -> None:
    """save_all 的加锁版：并发写记忆不丢更新。"""
    with _locked(HOME / "hippocampus.jsonl"):
        existing = load_all()
        by_id = {}
        no_id = []   # 缺 id 的畸形记录也原样保留，不能因为无法索引就悄悄丢掉
        for m in existing:
            mid = m.get("id")
            if mid:
                by_id[mid] = m
            else:
                no_id.append(m)
        for r in records:
            mid = r.get("id")
            if mid:
                by_id[mid] = r
        merged = list(by_id.values()) + no_id
        save_all(merged)


def _strengthen_write(touched: list) -> None:
    """recall 强化后的定向写回：只重写被"想起"记忆所在的层文件。

    上万条记忆时，旧实现（_locked_save_all 整库 merge + 全量重写 3 个文件）让每次
    recall 都背上整库 I/O；这里按层分组、只 patch 命中的行，开销与命中数成正比。

    注意只 patch 强化字段：touched 里的记录是 recall 开头无锁读的旧快照，
    若整条 update 回去，会把期间别人并发写入的 review/layer/tags 用旧值覆盖掉
    （例：刚 review confirm 的记忆被 recall 一碰又回到 pending）。

    跨层竞态防护：recall 锁外读快照后，dream/archive 可能已把记忆迁移层。
    所以锁内以旧快照的 layer 为起点重读目标文件做定向 patch——若 id 不在
    旧 layer，按 hippo→cortex→dust 顺序在其他层找它的实际位置再 patch；
    全找不到（可能已被归档/删除）则放弃并留日志，绝不往旧 layer 写回去。
    """
    if READONLY or not touched:
        return
    patch_fields = ("access_count", "stability", "last_access", "detail")
    with _locked(HOME / "hippocampus.jsonl"):
        updates: dict = {}
        for m in touched:
            if m.get("layer") == ARCHIVE_LAYER:
                continue  # 归档记忆只读展示，不给强化（需要时 archive restore 回热）
            if not m.get("id"):
                continue   # 畸形记录（缺 id）没法强化
            updates[m["id"]] = m
        if not updates:
            return
        fname = {"hippo": "hippocampus.jsonl", "cortex": "cortex.jsonl", "dust": "dust.jsonl"}
        order = ("hippo", "cortex", "dust")
        # 每层文件只读一次：patch 该层命中的 id，并把别的层没找到的记到这里
        remaining = dict(updates)
        for layer in order:
            if not remaining:
                break
            p = HOME / fname[layer]
            rows = read_jsonl(p)
            hit_ids = set()
            for r in rows:
                u = remaining.get(r.get("id"))
                if u:
                    hit_ids.add(r["id"])
                    for f in patch_fields:
                        if f in u:
                            r[f] = u[f]
            if hit_ids:
                write_jsonl(p, rows)
                for mid in hit_ids:
                    del remaining[mid]
        for mid in remaining:
            log_event("strengthen", f"放弃强化 {mid}：三层都找不到（可能已归档/删除）", path=fname["hippo"])


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
        records.append(rec)
        save_all(records)
        _remember_desire_nudge(rec, rec.get("importance"))
        return rec, "created"


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
    """
    if READONLY:
        raise PermissionError("记忆库当前只读，无法修改情绪事件。")
    path = HOME / "emotions.jsonl"
    with _locked(path):
        rows = read_jsonl(path)
        desc = sorted(rows, key=lambda r: r.get("ts", 0), reverse=True)
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
        desc = sorted(rows, key=lambda r: r.get("ts", 0), reverse=True)
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
        "alert": len(diff) >= 3 or len(ai) >= 3,
    }


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
    budget = budget_tokens or _cfg_num(cfg, "budget_tokens", 1400, int)
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
    lines = []
    for m, score, _ in mems:
        if used + lines_join_len(lines) > char_budget * 0.75:
            break
        star = "★" * max(1, round(_num(m.get("importance"), 0.5) * 5))
        lines.append(f"- [{fmt_date(m.get('created', 0))} {star}] {display_text(m)}")
    if lines:
        out.append(header)
        out.extend(lines)
        used += len("\n".join(lines))

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
        "archive": len(load_archive()),
        "pinned": sum(1 for m in active if m.get("pinned")),
        "avg_retention": round(sum(decays) / len(decays), 3),
        "top_tags": sorted(tags.items(), key=lambda x: -x[1])[:10],
        "persona_name": p.get("identity", {}).get("名字", ""),
        "mood": p.get("state", {}).get("mood", ""),
        "working": len(load_working()),
        "assoc": len(load_assoc()),
        "last_dream": load_dream_state().get("last_dream", 0),
        "daily": _daily_state().get("last_date", ""),
        "rationality": rationality(),
        "home": str(HOME),
    }


def list_memories(limit: int = 30, layer: str | None = None) -> list:
    records = load_all()
    if layer:
        records = [m for m in records if m.get("layer") == layer]
    records.sort(key=lambda m: _num(m.get("created"), 0), reverse=True)
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


def _recompute_paths() -> None:
    """集中重算所有由 HOME 派生的路径常量。

    这些常量分散定义在模块各处（LOG_FILE/WORKING_FILE 在开头，
    VERBATIM_DIR/ASSOC_FILE/PERSONA_SNAP_DIR/DAILY_FILE 在各自子系统旁），
    --home 切换目录时必须全部同步更新，否则残留旧目录的路径。
    模块加载完成时也调用一次，保证与文件内字面定义一致。
    """
    global LOG_FILE, WORKING_FILE, VERBATIM_DIR, ASSOC_FILE, PERSONA_SNAP_DIR, DAILY_FILE
    LOG_FILE = HOME / "lingshu.log"
    WORKING_FILE = HOME / "working.jsonl"
    VERBATIM_DIR = HOME / "verbatim"
    ASSOC_FILE = HOME / "assoc.jsonl"
    PERSONA_SNAP_DIR = HOME / "persona_snapshots"
    DAILY_FILE = HOME / "daily_state.json"


def _override_home(argv: list) -> list:
    """提取 --home <目录>/--home=<目录>，全局切换数据目录（任意位置均可）。
    同时写入环境变量，保证之后 import 的 growth 子系统读到同一个 HOME。"""
    global HOME
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
        _recompute_paths()
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


# ---------------------------------------------------------------- daily（每天例行）
# 要"每天更新保持最新"：daily 每天跑一次，自动完成三件事——
#   1) 写今日日记（journal）：今天经历了什么、情绪怎么样，落到笔记
#   2) 更新知识库：把今天重要的洞见/偏好提炼成可复用结论（自动去重合并）
#   3) 维护新鲜度：距上次做梦超过 24 小时就自动做梦（--auto）
# 会话开始时 build_injection 会检查：如果今天还没跑，就提醒先跑 daily。

DAILY_FILE = HOME / "daily_state.json"

# 所有派生路径常量已定义完毕：集中重算一次，保证 _recompute_paths 与上面
# 各处字面定义一致（今后新增派生路径只需改 _recompute_paths 一处）。
_recompute_paths()


def _daily_state() -> dict:
    if DAILY_FILE.exists():
        d = _slurp_json(DAILY_FILE)
        if isinstance(d, dict):
            return d
    return {"last_run": 0, "last_date": ""}


def _save_daily_state(st: dict) -> None:
    atomic_json(DAILY_FILE, st)


def daily(dry_run: bool = False, force: bool = False) -> dict:
    """每天例行。dry_run 只预览要写什么，不落盘；force 无视"今天已跑过"。"""
    ensure_home()
    today = fmt_date(now_ts())
    st = _daily_state()
    if st.get("last_date") == today and not force and not dry_run:
        return {"skipped": True, "reason": "今天已经例行过了（daily --force 可重跑）"}
    if READONLY and not dry_run:
        raise PermissionError("记忆库当前只读，无法执行每日例行。")

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
        "mood": mood_brief(),
    }


# ---------------------------------------------------------------- CLI

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
        r = daily(args.dry_run, args.force)
        if r.get("skipped"):
            print(r["reason"])
            return 0
        if args.dry_run:
            print(f"[预览] {r['date']} 的每日例行：")
            print(r["note_body"])
            print(f"\n将写入知识库 {r['knowledge_added']} 条新增 / {r['knowledge_merged']} 条合并"
                  + (f"；距上次做梦超时，将自动做梦" if r["dreamed"] == "would_run" else ""))
            return 0
        print(f"每日例行完成（{r['date']}）")
        print(f"  今日记忆 {r['memories_today']} 条，日记已写（含今日情绪与理智状态）")
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


if __name__ == "__main__":
    sys.exit(main())
