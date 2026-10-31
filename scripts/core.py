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


def clear_logs(ts=None, kind: str | None = None, msg: str | None = None) -> int:
    """清理运行日志：不给条件就全清；给 ts/kind/msg 就只删匹配的那条。

    返回删掉的条数。只读模式下不删（返回 0）。日志清理本身绝不当崩溃源。
    """
    if READONLY or not LOG_FILE.exists():
        return 0
    try:
        lines = [x for x in LOG_FILE.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    except OSError:
        return 0
    if ts is None and kind is None and msg is None:
        n = len(lines)
        try:
            LOG_FILE.write_text("", encoding="utf-8")
        except OSError:
            return 0
        return n
    keep, removed = [], 0
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            keep.append(ln)   # 解析不了的畸形行：全清以外的操作不动它，别误删
            continue
        hit = True
        if ts is not None:
            try:
                hit = hit and int(r.get("ts") or 0) == int(ts)
            except (TypeError, ValueError):
                hit = False
        if kind is not None:
            hit = hit and str(r.get("kind") or "") == str(kind)
        if msg is not None:
            hit = hit and str(r.get("msg") or "") == str(msg)
        if hit:
            removed += 1
        else:
            keep.append(ln)
    if removed:
        try:
            LOG_FILE.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        except OSError:
            return 0
    return removed


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


def write_text_atomic(path: Path, text: str) -> None:
    """整文件文本写入走 tmp + os.replace：直接 write_text 中途崩溃/被占用
    会留半截文件（梦境报告、AGENTS.md 这类被宿主解析的文件半截更糟）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    _replace_retry(tmp, path)


def _replace_retry(tmp: Path, path: Path) -> None:
    """os.replace 覆盖，遇 Windows 文件占用按指数退避重试；最终失败则清掉 tmp 并抛出。

    Windows 上 os.replace 覆盖"正被别的进程打开读取"的文件会抛 PermissionError。
    写方之间有锁互斥，但读方（stats/recall/面板）不加锁；退避等待读句柄释放。
    指数退避：0.05s 起步每次翻倍，封顶 0.6s，总预算约 10s——实测防病毒/索引器
    持读句柄能超过 4s，固定 12×短间隔的重试扛不住这种长占用。
    """
    delay = 0.05
    deadline = time.time() + 10.0
    attempt = 0
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            attempt += 1
            if time.time() >= deadline:
                log_event("io", f"写 {path.name} 失败：文件被占用，重试 {attempt} 次"
                                f"（约 10s）仍无果，可能有进程长期持有读句柄",
                          level="error", path=path.name)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.6)


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
    patch_fields = ("access_count", "stability", "last_access",
                    "used_count", "used_at", "used_hits")
    # detail 已从 patch 字段剔除：快照是锁外旧值，dream 淡化该记忆后写回旧 detail
    # 会把淡化抵消掉。强化只碰访问统计，不改内容。
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


# ---------------------------------------------------------------- 回忆使用反馈
# "存得好不好"不能只看条数：注入的记忆到底有没有被用上、哪些记忆从没被命中过，
# 这两个便宜计数才能回答"分层 + 遗忘曲线"是否真的让记忆更准。
RETRIEVAL_FILE = HOME / "last_retrieval.json"
USAGE_FILE = HOME / "recall_usage.jsonl"
USAGE_MIN_SHARED = 2      # 与回复共享的关键词元至少 2 个
USAGE_MIN_RATIO = 0.08    # 且至少覆盖记忆词元的 8%（粗判，不追求精确）

# 常用虚词二元组：它们在哪句话里都出现，不能算"用上了这条记忆"
_USAGE_STOP = {
    "我们", "你们", "他们", "这个", "那个", "就是", "什么", "因为", "所以",
    "但是", "可以", "已经", "不是", "没有", "一个", "自己", "现在", "时候",
    "这样", "如果", "还是", "然后", "觉得", "知道", "需要", "应该", "一下",
    "一些", "的话", "真的", "非常", "比较", "而且", "或者", "不过", "其中",
    "以及", "对于", "关于", "之后", "之前", "这种", "那种", "一直", "可能",
}


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
    _sync_state()


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
    global RETRIEVAL_FILE, USAGE_FILE
    LOG_FILE = HOME / "lingshu.log"
    WORKING_FILE = HOME / "working.jsonl"
    VERBATIM_DIR = HOME / "verbatim"
    ASSOC_FILE = HOME / "assoc.jsonl"
    PERSONA_SNAP_DIR = HOME / "persona_snapshots"
    DAILY_FILE = HOME / "daily_state.json"
    RETRIEVAL_FILE = HOME / "last_retrieval.json"
    USAGE_FILE = HOME / "recall_usage.jsonl"
    _sync_state()


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


# ---------------------------------------------------------------- 拆分后状态同步
# v2.22 起代码按领域拆到 memory/emotion/persona/dream/cli，各模块在 import 时
# 从 core 复制了一份 HOME/READONLY/派生路径。--home / --ro 切换后必须把 core 的
# 最新状态回灌给所有子模块，否则它们仍在读写旧目录（单文件时天然共享，拆分后不会）。
_SYNC_TARGETS: list = []


def _sync_state() -> None:
    """把 core 的当前全局状态同步到各子模块（brain.py 启动时登记同步目标）。"""
    if not _SYNC_TARGETS:
        return
    snap = {k: v for k, v in dict(globals()).items() if not k.startswith("__")}
    for m in _SYNC_TARGETS:
        m.__dict__.update(snap)


__all__ = [k for k in dict(globals()) if not k.startswith("__")]
