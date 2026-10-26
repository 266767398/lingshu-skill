#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""灵枢 本地可视化面板（仅监听 127.0.0.1）"""

from __future__ import annotations

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

# 本机随机 token：页面加载时注入，之后每个 API 请求都要带上。
# 目的是挡住「本机其他进程 / 恶意网页借 DNS rebinding 读走全部记忆」——
# 只靠 Origin/Host 校验挡不住不带 Origin 的直连请求。
TOKEN = os.environ.get("LINGSHU_PANEL_TOKEN") or secrets.token_urlsafe(24)


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
    records = brain.load_all()
    ordered = sorted(records, key=lambda m: m.get("created", 0), reverse=True)
    dreams = []
    ddir = brain.HOME / "dreams"
    if ddir.exists():
        for f in sorted(ddir.glob("*.md"), reverse=True)[:12]:
            dreams.append({"name": f.name, "mtime": brain.fmt_time(int(f.stat().st_mtime)),
                           "content": f.read_text(encoding="utf-8")[:3000]})
    pending = None
    pp = ddir / "pending.json" if ddir.exists() else None
    if pp and pp.exists():
        try:
            pending = json.loads(pp.read_text(encoding="utf-8-sig"))
        except Exception:
            pending = None
    return {
        "stats": brain.stats(),
        "persona": brain.load_persona(),
        "memories": [memory_view(m) for m in ordered[:80]],
        "dreams": dreams,
        "pending": pending,
        "mind": brain.mind_state(),
        "onboarding": brain.onboarding_state(),
    }


def workspace() -> dict:
    return {
        "status": growth.growth_status(),
        "knowledge": growth.list_knowledge(limit=100),
        "knowledge_stats": growth.knowledge_stats(),
        "tidy": growth.tidy_state(),
        "notes": growth.list_notes(limit=100),
        "note_stats": growth.note_stats(),
        "skills": growth.list_skills(),
        "dsh_plugins": growth.list_dsh_plugins(),
        "evolution": growth.load_evolution(limit=30),
        "onboarding": brain.onboarding_state(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "LingShuPanel/1.0"

    def log_message(self, fmt, *args):
        pass

    # -------------------------------------------------- helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        if length > 2_000_000:
            self._json({"error": "请求体过大"}, 413)
            self.close_connection = True
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8-sig"))
        except Exception:
            return {}

    def _origin_ok(self) -> bool:
        """只接受本机页面的请求，挡住跨站表单与 DNS rebinding。

        Host 必须是 127.0.0.1/localhost——否则恶意域名解析到 127.0.0.1 后，
        浏览器会把它当成同源，从而读走全部记忆。
        """
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
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
        return got == TOKEN

    # -------------------------------------------------- routes
    def do_GET(self):
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
        # 读取接口同样要校验来源 + token：记忆是隐私数据，不能被任意页面/进程读走
        if path.startswith("/api/") and (not self._origin_ok() or not self._token_ok()):
            self._json({"error": "forbidden"}, 403)
            return
        if path == "/api/overview":
            self._json(overview())
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
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._origin_ok() or not self._token_ok():
            self._json({"error": "forbidden"}, 403)
            return
        path = urlparse(self.path).path
        data = self._body()
        if data is None:
            return
        try:
            if path == "/api/remember":
                text = (data.get("text") or "").strip()
                if not text:
                    self._json({"error": "内容为空"}, 400)
                    return
                rec, how = brain.remember(
                    text,
                    kind=data.get("kind", "fact"),
                    importance=data.get("importance"),
                    tags=data.get("tags") or [],
                    entities=data.get("entities") or [],
                    source="user",
                )
                self._json({"ok": True, "how": how, "memory": memory_view(rec)})
                return

            if path == "/api/persona":
                updates = data.get("updates") or {}
                rejected = brain.update_persona(updates, why="面板编辑")
                self._json({"ok": True, "rejected": rejected, "persona": brain.load_persona()})
                return

            if path == "/api/persona/state":
                p = brain.load_persona()
                st = p.setdefault("state", {})
                for key in ("mood", "mood_note"):
                    if key in data:
                        st[key] = data[key]
                for key in ("energy", "closeness"):
                    if key in data:
                        st[key] = max(0, min(100, int(data[key])))
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
                    dr = brain.mind_tune(int(data.get("curiosity") or 0),
                                         int(data.get("reason") or 0),
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

            if path == "/api/seed":
                self._json({"error": "人格不再由预写种子生成，请用 /api/persona/edit"}, 410)
                return

            if path == "/api/dream":
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

            if path == "/api/memory":
                mid = data.get("id")
                records = brain.load_all()
                target = next((m for m in records if m["id"] == mid), None)
                if not target:
                    self._json({"error": "没有这条记忆"}, 404)
                    return
                for key in ("text", "gist", "kind", "pinned"):
                    if key in data:
                        target[key] = data[key]
                if "importance" in data:
                    imp = float(data["importance"])
                    target["importance"] = round(imp / 10 if imp > 1 else imp, 3)
                if "tags" in data:
                    target["tags"] = [t.strip() for t in data["tags"] if t.strip()]
                brain.save_all(records)
                self._json({"ok": True, "memory": memory_view(target)})
                return

            if path == "/api/forget":
                ok = brain.forget(data.get("id", ""))
                self._json({"ok": ok})
                return

            if path == "/api/knowledge":
                act = data.get("action", "add")
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
                    self._json({"ok": True, "result": growth.install_skill(data.get("name", ""),
                                                                          bool(data.get("force"))),
                                "workspace": workspace()})
                elif act == "uninstall":
                    self._json({"ok": True, "result": growth.uninstall_skill(data.get("name", "")),
                                "workspace": workspace()})
                elif act == "delete":
                    self._json({"ok": growth.remove_skill(data.get("name", "")), "workspace": workspace()})
                elif act == "check":
                    self._json({"ok": True, "checks": growth.check_skill(data.get("name"))})
                elif act == "show":
                    self._json({"ok": True, "skill": growth.read_skill(data.get("name", ""))})
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

            self._json({"error": "not found"}, 404)
        except Exception as exc:  # 面板需要把错误显示给用户
            self._json({"error": str(exc)}, 500)


def serve(port: int = 8765, open_browser: bool = True) -> None:
    brain.ensure_home()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
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
