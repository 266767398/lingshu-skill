#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 本地可视化面板（仅监听 127.0.0.1）"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain  # noqa: E402
import growth  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# 只读门控名单：这些 POST 端点会写数据目录，数据目录不可写时统一 403
_WRITE_ENDPOINTS = {
    "/api/remember", "/api/persona", "/api/persona/state", "/api/persona/restore",
    "/api/persona/edit", "/api/persona/mind", "/api/persona/interview",
    "/api/persona/self", "/api/persona/rollback",
    "/api/memory", "/api/memory/review", "/api/knowledge", "/api/knowledge/review",
    "/api/note", "/api/skill",
    "/api/skill/self-review", "/api/evolve", "/api/dream", "/api/dream/apply",
    "/api/dream/draft", "/api/memory/bulk",
    "/api/emotion", "/api/mode", "/api/link",
    "/api/forget", "/api/daily",
}

# 本机随机 token：页面加载时注入，之后每个 API 请求都要带上。
# 注意：token 会随首页响应下发，本机进程 GET / 即可提取，所以它防的不是本机进程，
# 而是浏览器上下文里的误用（恶意网页跨站调用本机端口）；跨站防护主要靠 Host/Origin 校验。
TOKEN = os.environ.get("LINGSHU_PANEL_TOKEN") or secrets.token_urlsafe(24)
# 环境变量给了空串/空白时不能退化成空 token（否则任何请求都能过鉴权）——直接拒启
if not TOKEN.strip():
    print("[面板] LINGSHU_PANEL_TOKEN 为空，拒绝启动（空 token 等于不设防）", file=sys.stderr)
    raise SystemExit(2)
TOKEN = TOKEN.strip()


def _int_param(url, key: str, default: int, low: int, high: int) -> int:
    try:
        val = int((parse_qs(url.query).get(key) or [default])[0])
    except (TypeError, ValueError):
        val = default
    return max(low, min(high, val))


def memory_view(m: dict) -> dict:
    out = dict(m)
    out["retention"] = round(brain.retention(m), 3)
    out["display"] = brain.display_text(m)
    out["created_text"] = brain.fmt_time(m.get("created", 0))
    return out


def overview() -> dict:
    # 逐条 try/except 降级：单条坏记忆/坏文件不能让整个 overview 500（面板全页挂掉）
    def _safe(label: str, fn, default):
        try:
            return fn()
        except Exception as exc:
            brain.log_event("panel", f"overview {label} 降级：{exc}", level="warn")
            return default

    records = brain.load_all() + brain.load_archive()
    ordered = sorted(records, key=lambda m: _created_key(m), reverse=True)
    memories = []
    for m in ordered[:80]:
        try:
            memories.append(memory_view(m))
        except Exception as exc:
            brain.log_event("panel", f"overview 跳过坏记忆 {m.get('id')}: {exc}", level="warn")
    dreams = []
    ddir = brain.HOME / "dreams"
    if ddir.exists():
        for f in sorted(ddir.glob("*.md"), reverse=True)[:12]:
            try:
                dreams.append({"name": f.name, "mtime": brain.fmt_time(int(f.stat().st_mtime)),
                               "content": f.read_text(encoding="utf-8")[:3000]})
            except (OSError, ValueError):
                continue
    return {
        "stats": _safe("stats", brain.stats, {}),
        "persona": _safe("persona", lambda: _persona_view(), {}),
        "memories": memories,
        "dreams": dreams,
        "pending": _safe("pending", brain.load_dream_pending, []),
        "mind": _safe("mind", brain.mind_state, {}),
        "mood": _safe("mood", brain.mood_now, {}),
        "emotion_arc": _safe("emotion_arc", lambda: brain.emotion_arc(7), []),
        "desires": _safe("desires", brain.desire_state, {}),
        "emotion_groups": _safe("emotion_groups", lambda: brain.EMOTION_GROUPS, {}),
        "desire_defs": _safe("desire_defs", lambda: brain.DESIRES, {}),
        "rationality": _safe("rationality", brain.rationality, {}),
        "emotion_cfg": _safe("emotion_cfg", brain.emotion_cfg, {}),
        "daily_state": _safe("daily_state", brain._daily_state, {}),
        "last_self_review": load_config_last_review(),
        "onboarding": _safe("onboarding", brain.onboarding_state, {}),
        "readonly": bool(brain.READONLY),
        "model_tier": brain.model_tier(),
        "mode": _safe("mode", lambda: str(brain.load_config().get("mode", "auto") or "auto").lower(), "auto"),
        "review_counts": _safe("review_counts", brain.review_counts, {}),
        "persona_drift": _safe("persona_drift", lambda: brain.persona_drift(7), []),
        "emotion_alerts": _safe("emotion_alerts", lambda: brain.emotion_checks_recent(10), []),
        "dream_draft": _safe("dream_draft", brain.load_dream_draft, None),
        "mode_detail": _safe("mode_detail", lambda: brain.resolve_mode_detail(""), {}),
        "log_summary": _safe("log_summary", _log_summary, {"warn": 0, "error": 0}),
        "home": str(brain.HOME),
    }


def _persona_view() -> dict:
    """完整人格 + 回归后的 state（energy/closeness 读时向基线懒回归，不写盘）。"""
    p = brain.load_persona()
    st = p.get("state") or {}
    if st:
        p["state"] = brain._state_regress(st)
    return p


def _created_key(m: dict):
    """排序键：created 可能是坏类型（手改/LLM 产出），坏值沉底不抛异常。"""
    try:
        return float(m.get("created", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _imp_key(m: dict) -> float:
    """importance 安全转 float：坏值（None/非数字字符串/list 等）沉底为 0。"""
    try:
        return float(m.get("importance") or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_num(v, default=None):
    """把请求里的值安全转 float：失败返回 default（默认 None，调用方据此走 400）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default=None):
    """把请求里的值安全转 int：失败返回 default（默认 None，调用方据此走 400）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _log_summary() -> dict:
    """最近 200 条里 error / warn（不含 error）各自多少条，一次读取避免重复解析。"""
    recent = brain.read_logs(200, level=None)
    errs = sum(1 for r in recent if r.get("level") == "error")
    warns = sum(1 for r in recent if r.get("level") == "warn")
    return {"warn": warns, "error": errs}


def load_config_last_review() -> float:
    try:
        return float(brain.load_config().get("last_self_review", 0) or 0)
    except Exception:
        return 0


def filtered_memories(url) -> list:
    """组合筛选：层 + 关键词 + 标签 + 情绪 + 重要性区间 + 时间范围 + 排序。

    面板的搜索框只做字面过滤；真正的模糊回忆走 /api/search（recall）。
    """
    qs = parse_qs(url.query)

    def one(key: str, default: str = "") -> str:
        return (qs.get(key) or [default])[0]

    layer = one("layer").strip()
    tag = one("tag").strip().lower()
    emotion = one("emotion").strip().lower()
    text = one("q").strip().lower()
    sort = one("sort", "created")
    limit = _int_param(url, "limit", 200, 1, 500)

    def num(key: str, default: float) -> float:
        try:
            return float(one(key, str(default)) or default)
        except (TypeError, ValueError):
            return default

    imp_min, imp_max = num("imp_min", 0.0), num("imp_max", 1.0)
    # 兼容「1–10 分」输入：大于 1 视为十分制
    if imp_min > 1:
        imp_min /= 10
    if imp_max > 1:
        imp_max /= 10

    days = _int_param(url, "days", 0, 0, 36500)
    since = brain.now_ts() - days * 86400 if days else 0

    rows = []
    for m in brain.load_all() + brain.load_archive():
        if layer and m.get("layer") != layer:
            continue
        if tag and tag not in [str(t).lower() for t in (m.get("tags") or [])]:
            continue
        if emotion and emotion not in str(m.get("emotion") or "").lower():
            continue
        if text:
            blob = " ".join([str(m.get("text", "")),
                             " ".join(str(t) for t in (m.get("tags") or [])),
                             " ".join(str(e) for e in (m.get("entities") or []))]).lower()
            if text not in blob:
                continue
        imp = _imp_key(m)
        if imp < imp_min or imp > imp_max:
            continue
        if since and _created_key(m) < since:
            continue
        rows.append(m)

    if sort == "importance":
        rows.sort(key=_imp_key, reverse=True)
    elif sort == "retention":
        rows.sort(key=lambda m: brain.retention(m), reverse=True)
    else:
        rows.sort(key=_created_key, reverse=True)
    return [memory_view(m) for m in rows[:limit]]


def workspace() -> dict:
    """工作台数据汇总。任何一块读取失败都降级成空值并记日志——
    否则一个坏掉的 knowledge/notes 文件会让所有写接口在写成功之后报 400，
    前端以为失败又重试，等于重复写入。"""
    out: dict = {}
    errors: list = []

    def safe(label, fn, fallback):
        try:
            out[label] = fn()
        except Exception as exc:   # noqa: BLE001 —— 单块失败不影响其它块
            out[label] = fallback
            errors.append({"label": label, "type": type(exc).__name__})
            brain.log_event("panel", f"workspace 读取 {label} 失败：{exc}", level="error")

    safe("status", growth.growth_status, {})
    safe("knowledge", lambda: growth.list_knowledge(limit=100), [])
    safe("knowledge_stats", growth.knowledge_stats, {})
    safe("tidy", growth.tidy_state, {})
    safe("notes", lambda: growth.list_notes(limit=100), [])
    safe("note_stats", growth.note_stats, {})
    safe("skills", growth.list_skills, [])
    safe("dsh_plugins", growth.list_dsh_plugins, [])
    safe("evolution", lambda: growth.load_evolution(limit=30), [])
    safe("onboarding", brain.onboarding_state, {})
    safe("pending_knowledge", growth.knowledge_pending, [])
    out["readonly"] = bool(brain.READONLY)
    out["model_tier"] = brain.model_tier()
    if errors:
        # 只回标签和异常类型；完整的异常全文已在 log_event 里，别把本机路径/堆栈塞进 HTTP 响应
        out["workspace_errors"] = errors
    return out


def _bad_list(data: dict, *keys) -> str | None:
    """这些字段必须是数组（前端传字符串会被当成字符列表逐字拆开）。"""
    for key in keys:
        val = data.get(key)
        if val is not None and not isinstance(val, list):
            return f"{key} 必须是数组"
    return None


# 这些写端点里有只读 action：只读模式下按 action 放行（skill 的 list/check/show、
# emotion 的 now/check），别把纯查询按钮也一刀切 403
_READONLY_ACTIONS = {
    "/api/skill": {"", "list", "check", "show"},
    "/api/emotion": {"", "now", "check"},
}


def _readonly_allowed(path: str, data: dict) -> bool:
    if path == "/api/daily" and bool(data.get("dry_run")):
        return True
    if path == "/api/skill/self-review":
        return True
    allowed = _READONLY_ACTIONS.get(path)
    if allowed is None:
        return False
    return str(data.get("action") or "") in allowed


class Handler(BaseHTTPRequestHandler):
    server_version = "LingShuPanel/1.0"

    def setup(self):
        super().setup()
        # 慢/坏客户端最多等 30 秒：没有这个，不读完请求体的连接能让线程永久挂住
        # （类属性 timeout 在 Python 3.13 的 BaseHTTPRequestHandler 上不生效，必须在 socket 上设置）
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        # 客户端刷新/关页面会把连接掐断（WinError 10053/10054），响应写不出去是正常现象，
        # 静默收尾即可——不让 ConnectionAbortedError 刷满控制台
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    # -------------------------------------------------- helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")  # 防止把响应当 HTML 解析
        self.send_header("Referrer-Policy", "no-referrer")
        # 页面有内联 <script>/<style>，明确放开；其余资源只能同源，禁止外链脚本/图片域名
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                         "frame-ancestors 'none'")  # 防恶意网页 iframe 嵌入面板（点击劫持）
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self):
        """读并解析 JSON 请求体。

        返回 dict = 正常；返回 None = 已经回了错误响应（调用方直接 return），
        绝不再把"解析失败"当成空对象放过去——否则非法请求会被当成合法空操作。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:  # 负的 Content-Length 视为空请求体
            return {}
        if length > 2_000_000:
            self._json({"error": "请求体过大"}, 413)
            self.close_connection = True
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8-sig")
        except Exception:
            self._json({"error": "无法读取请求体"}, 400)
            return None
        if not raw.strip():
            return {}
        try:
            obj = json.loads(raw)
        except Exception:
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return None
        if not isinstance(obj, dict):
            self._json({"error": "请求体必须是 JSON 对象"}, 400)
            return None
        return obj

    def _origin_ok(self) -> bool:
        """只接受本机页面的请求，挡住跨站表单与 DNS rebinding。

        Host 必须是 127.0.0.1/localhost——否则恶意域名解析到 127.0.0.1 后，
        浏览器会把它当成同源，从而读走全部记忆。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):       # IPv6：[::1]:8765
            host = host.split("]")[0] + "]"
        else:
            host = host.split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in (f"http://{self.headers.get('Host', '')}",
                          f"http://127.0.0.1:{self.server.server_port}",
                          f"http://localhost:{self.server.server_port}")

    def _token_ok(self) -> bool:
        """所有 API 请求必须带正确 token（页面注入，非浏览器可借机偷读）。"""
        got = ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
        # encode 成 bytes 再比：非 ASCII header（恶意/异常客户端）直接传 str 会让
        # compare_digest 抛 TypeError，且 do_POST 的鉴权调用不在 try 里，会断连
        return hmac.compare_digest(got.encode("utf-8"), TOKEN.encode("utf-8"))

    # -------------------------------------------------- routes
    def do_GET(self):
        # 与 do_POST 对齐的兜底：任意一条数据损坏都不能让整个接口断连无响应
        try:
            self._route_get()
        except FileExistsError as exc:
            print(f"[面板] 文件已存在: {exc}", file=sys.stderr)
            self._json({"error": "同名文件已存在"}, 409)
        except PermissionError as exc:
            print(f"[面板] 文件被占用: {exc}", file=sys.stderr)
            self._json({"error": "文件被占用或没有写入权限"}, 400)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception:
            import traceback
            traceback.print_exc()
            brain.log_event("panel", "GET 接口内部错误", level="error",
                            trace=traceback.format_exc()[-400:])
            self._json({"error": "面板内部错误，详情见运行日志"}, 500)

    def _route_get(self):
        url = urlparse(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            html = ASSETS / "panel.html"
            if not html.exists():
                self._send(404, b"panel.html missing", "text/plain; charset=utf-8")
                return
            # 注入随机 token：页面里每个 API 请求都会带上，杜绝无 Origin 直连偷读
            content = html.read_bytes().decode("utf-8")
            content = content.replace("/*__PANEL_TOKEN__*/", TOKEN)
            self._send(200, content.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            # 浏览器自动请求，返回透明 1x1 PNG，避免控制台刷 404
            self._send(200, bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000d4944415478da63fcffff3f030005fe02fea72d1e480000000049454e44ae426082"),
                "image/png")
            return
        # 读取接口同样要校验来源 + token：记忆是隐私数据，不能被任意页面/进程读走
        if path.startswith("/api/") and (not self._origin_ok() or not self._token_ok()):
            self._json({"error": "forbidden"}, 403)
            return
        if path == "/api/overview":
            self._json(overview())
            return
        if path == "/api/self-update":
            self._json(growth.update_check("lingshu"))
            return
        if path == "/api/workspace":
            self._json(workspace())
            return
        if path == "/api/growth/review":
            days = _int_param(url, "days", 7, 1, 365)
            self._json(growth.growth_review(days))
            return
        if path == "/api/workspace/search":
            q = (parse_qs(url.query).get("q") or [""])[0]
            scope = (parse_qs(url.query).get("scope") or ["knowledge"])[0]
            if scope == "notes":
                rows = [{"note": r, "score": s} for r, s in growth.search_notes(q, 30)]
            else:
                rows = [{"knowledge": r, "score": s} for r, s in growth.search_knowledge(q, 30)]
            self._json(rows)
            return
        if path == "/api/search":
            q = (parse_qs(url.query).get("q") or [""])[0]
            k = _int_param(url, "k", 12, 1, 100)
            results = brain.recall(q, k=k, strengthen=False)
            self._json([{"memory": memory_view(m), "score": s, "parts": p} for m, s, p in results])
            return
        if path == "/api/memories":
            self._json(filtered_memories(url))
            return
        if path == "/api/inject-preview":
            q = (parse_qs(url.query).get("q") or [""])[0]
            try:
                packet = brain.build_injection(q)
            except Exception as exc:
                self._json({"error": f"生成注入包失败：{exc}"}, 500)
                return
            self._json({"packet": packet})
            return
        if path == "/api/conflicts":
            limit = _int_param(url, "limit", 50, 1, 200)
            self._json({"conflicts": brain.detect_conflicts(limit)})
            return
        if path == "/api/graph":
            limit = _int_param(url, "limit", 300, 10, 800)
            self._json(brain.assoc_graph(limit))
            return
        if path == "/api/assoc":
            mid = (parse_qs(url.query).get("id") or [""])[0]
            self._json({"id": mid, "neighbors": brain.assoc_of(mid)})
            return
        if path == "/api/mode/explain":
            q = (parse_qs(url.query).get("q") or [""])[0]
            self._json(brain.resolve_mode_detail(q))
            return
        if path == "/api/logs":
            limit = _int_param(url, "limit", 200, 1, 2000)
            level = (parse_qs(url.query).get("level") or [""])[0] or None
            if level == "all":
                level = None
            self._json({"logs": brain.read_logs(limit, level=level)})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._origin_ok() or not self._token_ok():
            self._json({"error": "forbidden"}, 403)
            return
        path = urlparse(self.path).path
        data = self._body()
        if data is None:
            return
        # 只读门控：写接口在数据目录不可写时直接 403（展示/预览接口放行）
        if brain.READONLY and path in _WRITE_ENDPOINTS and not _readonly_allowed(path, data):
            self._json({"error": "readonly", "message": "数据目录当前只读，写操作已被禁用。"}, 403)
            return
        try:
            if path == "/api/remember":
                text = (data.get("text") or "").strip()
                if not text:
                    self._json({"error": "内容为空"}, 400)
                    return
                tags = data.get("tags") or []
                entities = data.get("entities") or []
                if not isinstance(tags, list) or not isinstance(entities, list):
                    self._json({"error": "tags / entities 必须是数组"}, 400)
                    return
                rec, how = brain.remember(
                    text,
                    kind=data.get("kind", "fact"),
                    importance=data.get("importance"),
                    tags=tags,
                    entities=entities,
                    source="user",
                    emotion=(data.get("emotion") or None),
                )
                self._json({"ok": True, "how": how, "memory": memory_view(rec)})
                return

            if path == "/api/persona":
                updates = data.get("updates") or {}
                rejected = brain.update_persona(updates, why="面板编辑")
                self._json({"ok": True, "rejected": rejected, "persona": brain.load_persona()})
                return

            if path == "/api/persona/state":
                # 读-改-写整段加锁：否则和 CLI/做梦的并发人格写入互相覆盖
                with brain._locked(brain.HOME / "persona.json"):
                    p = brain.load_persona()
                    st = p.setdefault("state", {})
                    # 先按时间结算衰减（精力回升/亲密回淡），再叠加面板新值，口径与读路径一致
                    regressed = brain._state_regress(st)
                    st.update(regressed)
                    for key in ("mood", "mood_note"):
                        if key in data:
                            st[key] = data[key]
                    for key in ("energy", "closeness"):
                        if key in data:
                            v = _to_int(data[key])
                            if v is None:
                                self._json({"error": f"{key} 必须是数字"}, 400)
                                return
                            st[key] = max(0, min(100, v))
                    st["updated"] = brain.now_ts()
                    p.setdefault("history", []).append({
                        "ts": brain.now_ts(), "field": "state", "from": None,
                        "to": {k: data[k] for k in data}, "why": "面板编辑",
                    })
                    brain.save_persona(p)
                self._json({"ok": True, "persona": p})
                return

            if path == "/api/persona/edit":
                p = brain.edit_persona(
                    identity=data.get("identity") or None,
                    traits=data.get("traits") or None,
                    drives=data.get("drives") or None,
                    style=data.get("style") or None,
                    values=data.get("values") or None,
                    likes=data.get("likes") or None,
                    dislikes=data.get("dislikes") or None,
                    background=data.get("background") or None,
                    why=data.get("why") or "面板编辑",
                )
                self._json({"ok": True, "persona": p, "onboarding": brain.onboarding_state()})
                return

            if path == "/api/persona/mind":
                if data.get("mode") == "tune":
                    cur, rsn = _to_int(data.get("curiosity")), _to_int(data.get("reason"))
                    if cur is None or rsn is None:
                        self._json({"error": "curiosity / reason 必须是数字"}, 400)
                        return
                    dr = brain.mind_tune(cur, rsn,
                                         why=data.get("why") or "面板调节")
                else:
                    dr = brain.mind_set(data.get("curiosity"), data.get("reason"), why="面板设定")
                self._json({"ok": True, "drives": dr, "mind": brain.mind_state(),
                            "persona": brain.load_persona()})
                return

            if path == "/api/mind/weigh":
                r = brain.mind_weigh(data.get("topic", ""), data.get("benefit", 5),
                                     data.get("cost", 5), data.get("confidence", 0.6),
                                     data.get("urgency", 0.5))
                self._json({"ok": True, "result": r})
                return

            if path == "/api/emotion":
                act = data.get("action", "now")
                if act == "check":
                    r = brain.emotion_consistency_check(data.get("text", ""))
                    self._json({"ok": True, "result": r,
                                "alerts": brain.emotion_checks_recent(10)})
                    return
                if act == "feel":
                    intensity = _to_num(data.get("intensity"), 0.6)
                    if intensity is None:
                        self._json({"error": "intensity 必须是数字"}, 400)
                        return
                    brain.feel(data.get("emotion", ""), intensity,
                               why=data.get("why", ""), about=data.get("about", ""),
                               decay_hours=data.get("decay_hours"))
                    self._json({"ok": True, "mood": brain.mood_now(),
                                "rationality": brain.rationality(),
                                "emotion_arc": brain.emotion_arc(7),
                                "desires": brain.desire_state()})
                    return
                if act == "evaluate":
                    r = brain.emotion_evaluate(data.get("event", ""), data.get("polarity", "auto"),
                                               data.get("importance", 0.6),
                                               why=data.get("why", ""), about=data.get("about", ""))
                    self._json({"ok": True, "result": r, "mood": brain.mood_now(),
                                "rationality": brain.rationality(),
                                "emotion_arc": brain.emotion_arc(7),
                                "desires": brain.desire_state()})
                    return
                if act == "mirror":
                    r = brain.emotion_mirror(data.get("emotion", ""), data.get("intensity", 0.5))
                    self._json({"ok": True, "result": r, "mood": brain.mood_now(),
                                "rationality": brain.rationality(),
                                "emotion_arc": brain.emotion_arc(7),
                                "desires": brain.desire_state()})
                    return
                if act == "config":
                    updates = {}
                    if data.get("empathy"):
                        updates["empathy"] = {"level": data["empathy"]}
                    if data.get("gain") is not None:
                        updates["empathy"] = dict(updates.get("empathy", {}),
                                                  gain=max(0.0, min(1.0, float(data["gain"]))))
                    if data.get("max_intensity") is not None:
                        updates["empathy"] = dict(updates.get("empathy", {}),
                                                  max_intensity=max(0.0, min(1.0, float(data["max_intensity"]))))
                    if data.get("suppressed") is not None:
                        updates["suppressed"] = data.get("suppressed") or []
                    if data.get("notes") is not None:
                        updates["notes"] = data.get("notes")
                    cfg = brain.save_emotion_cfg(updates)
                    self._json({"ok": True, "emotion_cfg": cfg, "mood": brain.mood_now(),
                                "rationality": brain.rationality()})
                    return
                if act == "desire":
                    amount = _to_num(data.get("amount"), 15)
                    if amount is None:
                        self._json({"error": "amount 必须是数字"}, 400)
                        return
                    brain.desire_satisfy(data.get("emotion", ""), amount)
                    self._json({"ok": True, "mood": brain.mood_now(),
                                "rationality": brain.rationality(),
                                "emotion_arc": brain.emotion_arc(7),
                                "desires": brain.desire_state()})
                    return
                # 走到这里只允许 now（或缺省不传）→ 返回当前心情快照；拼错/未知 action 明确报错，
                # 别"假成功"让人以为写进去了
                if act not in ("", "now"):
                    self._json({"error": f"未知操作：{act}"}, 400)
                    return
                self._json({"ok": True, "mood": brain.mood_now(),
                            "rationality": brain.rationality(),
                            "emotion_arc": brain.emotion_arc(7),
                            "desires": brain.desire_state()})
                return

            if path == "/api/link":
                act = data.get("action", "link")
                if act == "link":
                    r = brain.link_memory(data.get("a", ""), data.get("b", ""),
                                          data.get("relation", "相关"),
                                          data.get("weight", 1.0), data.get("why", ""))
                elif act == "unlink":
                    r = {"ok": brain.unlink_memory(data.get("a", ""), data.get("b", ""))}
                elif act == "auto":
                    r = brain.assoc_auto(dry_run=bool(data.get("dry_run")))
                else:
                    self._json({"error": "未知操作"}, 400)
                    return
                self._json({"ok": True, "result": r, "graph": brain.assoc_graph()})
                return

            if path == "/api/daily":
                dry = bool(data.get("dry_run"))
                # 真跑会整理/归档记忆：必须显式 confirm，避免空 body 的误触/重放直接写盘
                if not dry and data.get("confirm") is not True:
                    self._json({"error": "真跑每日整理需要显式传 confirm: true（或用 dry_run 先预演）"}, 400)
                    return
                r = brain.daily(dry_run=dry, force=bool(data.get("force")))
                self._json({"ok": True, "result": r, "overview": overview() if not dry else None,
                            "workspace": workspace() if not dry else None})
                return

            if path == "/api/skill/self-review":
                r = growth.self_review(force=bool(data.get("force")),
                                       days=_to_int(data.get("days", 3) or 3, 3) or 3)
                self._json({"ok": True, "result": r})
                return

            if path == "/api/seed":
                self._json({"error": "人格不再由预写种子生成，请用 /api/persona/edit"}, 410)
                return

            if path == "/api/dream":
                # 做梦会重放/巩固/遗忘记忆：必须显式 confirm，空 body 不触发真实写盘
                if data.get("confirm") is not True:
                    self._json({"error": "开始做梦需要显式传 confirm: true"}, 400)
                    return
                report = brain.dream(trigger="panel")
                self._json({"ok": True, "report": report, "overview": overview()})
                return

            if path == "/api/dream/apply":
                result = data.get("result") or {}
                if isinstance(result, str):
                    result = json.loads(result)
                applied = brain.apply_dream(result)
                self._json({"ok": True, "applied": applied, "overview": overview()})
                return

            if path == "/api/dream/draft":
                draft = data.get("draft")
                if draft is None:
                    brain.clear_dream_draft()  # 传 null = 清空草稿
                    self._json({"ok": True, "draft": None})
                    return
                if isinstance(draft, str):
                    try:
                        draft = json.loads(draft)  # 允许前端直接传 JSON 文本
                    except json.JSONDecodeError as exc:
                        self._json({"error": f"草稿 JSON 解析失败：{exc}"}, 400)
                        return
                if not isinstance(draft, dict):
                    self._json({"error": "草稿必须是 JSON 对象"}, 400)
                    return
                brain.save_dream_draft(draft)
                self._json({"ok": True, "draft": brain.load_dream_draft()})
                return

            if path == "/api/memory":
                mid = data.get("id")
                # 读-改-写整体加锁：save_all 是整库快照覆写，不加锁会覆盖掉并发写入
                with brain._locked(brain.HOME / "hippocampus.jsonl"):
                    records = brain.load_all()
                    target = next((m for m in records if m["id"] == mid), None)
                    if not target:
                        self._json({"error": "没有这条记忆"}, 404)
                        return
                    for key in ("text", "gist", "pinned"):
                        if key in data:
                            target[key] = data[key]
                    if "kind" in data:
                        kind = str(data["kind"] or "").strip()
                        if kind not in brain.KINDS:  # 白名单：防恶意/手滑写入怪异类型
                            self._json({"error": f"kind 只能是：{'、'.join(brain.KINDS)}"}, 400)
                            return
                        target["kind"] = kind
                    if "importance" in data:
                        imp = _to_num(data["importance"])
                        if imp is None:
                            self._json({"error": "importance 必须是数字"}, 400)
                            return
                        target["importance"] = round(max(0.0, min(1.0, imp / 10 if imp > 1 else imp)), 3)
                    if "tags" in data:
                        tags = data["tags"]
                        if not isinstance(tags, list):
                            self._json({"error": "tags 必须是数组"}, 400)
                            return
                        target["tags"] = [str(t).strip() for t in tags if str(t).strip()]
                    brain.save_all(records)
                self._json({"ok": True, "memory": memory_view(target)})
                return

            if path == "/api/forget":
                ok = brain.forget(data.get("id", ""))
                self._json({"ok": ok})
                return

            if path == "/api/knowledge":
                act = data.get("action", "add")
                if act in ("add", "update"):
                    bad = _bad_list(data, "points", "tags", "entities")
                    if bad:
                        self._json({"error": bad}, 400)
                        return
                if act == "add":
                    rec, how = growth.add_knowledge(
                        data.get("title", ""), data.get("body", ""), data.get("topic", ""),
                        data.get("points"), data.get("tags"), data.get("entities"))
                    self._json({"ok": True, "how": how, "knowledge": rec, "workspace": workspace()})
                elif act == "update":
                    rec = growth.update_knowledge(
                        data.get("id", ""), title=data.get("title"), topic=data.get("topic"),
                        body=data.get("body"), points=data.get("points"),
                        tags=data.get("tags"), entities=data.get("entities"),
                        pinned=data.get("pinned"))
                    self._json({"ok": True, "knowledge": rec, "workspace": workspace()})
                elif act == "delete":
                    self._json({"ok": growth.delete_knowledge(data.get("id", "")), "workspace": workspace()})
                elif act == "review":
                    rec = growth.set_knowledge_status(data.get("id", ""), "confirmed")
                    self._json({"ok": bool(rec), "knowledge": rec, "workspace": workspace()})
                elif act == "discard":
                    rec = growth.set_knowledge_status(data.get("id", ""), "discarded")
                    self._json({"ok": bool(rec), "knowledge": rec, "workspace": workspace()})
                elif act == "tidy":
                    self._json({"ok": True, "report": growth.tidy_knowledge(), "workspace": workspace()})
                elif act == "tidy/apply":
                    result = data.get("result") or {}
                    if isinstance(result, str):
                        result = json.loads(result)
                    self._json({"ok": True, "applied": growth.apply_tidy(result), "workspace": workspace()})
                else:
                    self._json({"error": "未知操作"}, 400)
                return

            if path == "/api/note":
                act = data.get("action", "write")
                if act in ("write", "update"):
                    bad = _bad_list(data, "tags", "memory_ids")
                    if bad:
                        self._json({"error": bad}, 400)
                        return
                if act == "write":
                    rec, how = growth.write_note(
                        data.get("type", "journal"), data.get("title", ""), data.get("body", ""),
                        data.get("mood", ""), data.get("tags"), data.get("memory_ids"),
                        append_same_day=bool(data.get("append", True)))
                    self._json({"ok": True, "how": how, "note": rec, "workspace": workspace()})
                elif act == "update":
                    rec = growth.update_note(data.get("id", ""), title=data.get("title"),
                                             body=data.get("body"), mood=data.get("mood"),
                                             tags=data.get("tags"))
                    self._json({"ok": True, "note": rec, "workspace": workspace()})
                elif act == "delete":
                    self._json({"ok": growth.delete_note(data.get("id", "")), "workspace": workspace()})
                else:
                    self._json({"error": "未知操作"}, 400)
                return

            if path == "/api/skill":
                act = data.get("action", "list")
                if act == "new":
                    res = growth.create_skill(data.get("name", ""), data.get("description", ""),
                                              data.get("body", ""), bool(data.get("force")))
                    self._json({"ok": True, "skill": res, "workspace": workspace()})
                elif act == "edit":
                    res = growth.edit_skill(data.get("name", ""), data.get("body"),
                                            data.get("append"), data.get("description"))
                    self._json({"ok": True, "skill": res, "workspace": workspace()})
                elif act == "install":
                    # 面板安装忽略 from_dir：本地 CLI 才能指外部目录，防远程乱指
                    res = growth.install_skill(data.get("name", ""),
                                               bool(data.get("force")),
                                               None,
                                               accept_risk=bool(data.get("yes")))
                    self._json({"ok": True, "result": res, "workspace": workspace()})
                    return
                elif act == "uninstall":
                    # 会删全局技能目录里的东西，记一笔日志，事后能查是谁在面板上删的；
                    # managed_only 只允许卸载灵枢装过的技能，防止误删用户其他全局插件
                    res = growth.uninstall_skill(data.get("name", ""), managed_only=True)
                    brain.log_event("panel", f"卸载技能 {data.get('name', '')} → {res.get('state')}")
                    self._json({"ok": True, "result": res,
                                "workspace": workspace()})
                elif act == "delete":
                    ok = growth.remove_skill(data.get("name", ""))
                    brain.log_event("panel", f"删除自建技能 {data.get('name', '')} → {ok}")
                    self._json({"ok": ok, "workspace": workspace()})
                elif act == "check":
                    self._json({"ok": True, "checks": growth.check_skill(data.get("name"))})
                elif act == "show":
                    self._json({"ok": True, "skill": growth.read_skill(data.get("name", ""))})
                elif act == "list":
                    self._json({"ok": True, "skills": growth.list_skills(), "workspace": workspace()})
                else:
                    self._json({"error": "未知操作"}, 400)
                return

            if path == "/api/evolve":
                row = growth.log_evolution(data.get("kind", "self"), data.get("title", ""),
                                           data.get("detail", ""), data.get("refs") or [])
                self._json({"ok": True, "evolution": row, "workspace": workspace()})
                return

            if path == "/api/persona/interview":
                p = brain.persona_interview(data.get("name", ""), data.get("traits", ""),
                                            data.get("style", ""), data.get("values", ""),
                                            data.get("callme", ""), data.get("self_name", ""),
                                            bool(data.get("unsure")))
                self._json({"ok": True, "persona": p, "onboarding": brain.onboarding_state()})
                return

            if path == "/api/persona/self":
                p = brain.persona_self_author(data.get("name", ""), data.get("traits", ""),
                                              data.get("likes", ""), data.get("dislikes", ""),
                                              data.get("habits", ""), data.get("insights", ""),
                                              data.get("style", ""), data.get("values", ""),
                                              data.get("callme", ""), data.get("self_name", ""))
                self._json({"ok": True, "persona": p, "onboarding": brain.onboarding_state()})
                return

            if path == "/api/persona/rollback":
                idx = _to_int(data.get("index"))
                if idx is None:
                    self._json({"error": "index 必须是数字"}, 400)
                    return
                try:
                    p = brain.persona_rollback(idx)
                except (ValueError, IndexError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"ok": True, "persona": p})
                return

            if path == "/api/memory/review":
                mid, act = data.get("id", ""), data.get("action", "")
                if act not in ("confirm", "discard"):
                    self._json({"error": "action 只能是 confirm / discard"}, 400)
                    return
                try:
                    r = brain.set_memory_review(mid, act)
                except (ValueError, PermissionError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"ok": True, "memory": r, "review_counts": brain.review_counts()})
                return

            if path == "/api/memory/bulk":
                try:
                    r = brain.bulk_memory_action(data.get("ids") or [],
                                                 data.get("action", ""), data.get("value"))
                except (ValueError, PermissionError) as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"ok": True, "result": r, "review_counts": r.get("review_counts")})
                return

            if path == "/api/mode":
                mode = str(data.get("mode", "") or "").lower()
                if mode not in ("work", "human", "auto"):
                    self._json({"error": "mode 只能是 work / human / auto"}, 400)
                    return
                brain.config_set("mode", mode)
                self._json({"ok": True, "mode": mode})
                return

            self._json({"error": "not found"}, 404)
        except FileExistsError as exc:
            # 已有同名技能/扩展/笔记：409 冲突比 500 内部错误更符合事实，前端也能直接提示
            # 系统异常的 str() 含本机路径，只回通用文案，细节进控制台日志
            print(f"[面板] 文件已存在: {exc}", file=sys.stderr)
            self._json({"error": "同名文件已存在"}, 409)
        except PermissionError as exc:
            print(f"[面板] 文件被占用: {exc}", file=sys.stderr)
            self._json({"error": "文件被占用或没有写入权限"}, 400)
        except (ValueError, KeyError) as exc:
            # 业务异常 / 参数错：消息本身就是给用户看的，直接回显
            self._json({"error": str(exc)}, 400)
        except Exception:  # 含 TypeError/AttributeError 等服务端编程错误：日志留全栈，响应只给通用文案，避免回显本机路径
            import traceback
            traceback.print_exc()
            brain.log_event("panel", "POST 接口内部错误", level="error",
                            trace=traceback.format_exc()[-400:])
            self._json({"error": "面板内部错误，详情见运行日志"}, 500)


def serve(port: int = 8765, open_browser: bool = True) -> None:
    brain.ensure_home()
    brain._resolve_readonly(False)   # 面板也要探测只读：数据目录不可写时 POST 写接口返回 403
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"[错误] 端口 {port} 被占用或不可用：{exc}", file=sys.stderr)
        print("换一个端口试试：python scripts/panel_server.py --port 8766", file=sys.stderr)
        raise SystemExit(1) from exc
    httpd.daemon_threads = True   # 退出时不等挂住的请求线程，防止 Ctrl+C 后进程残留
    url = f"http://127.0.0.1:{port}"
    print(f"灵枢 · 记忆面板：{url}")
    print(f"记忆库：{brain.HOME}")
    print("Ctrl+C 停止")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    serve(a.port, not a.no_browser)
