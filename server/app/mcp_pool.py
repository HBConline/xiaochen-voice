# -*- coding: utf-8 -*-
"""ToolPool: prewarmed stdio MCP sessions for the voice brain's tools loop.

Speaks the same minimal MCP subset as servers/home_mcp.py: newline-delimited
JSON-RPC 2.0 with initialize / tools/list / tools/call. Each server is a
subprocess started once and kept warm; a dead one restarts on the next call.

Exposed to the LLM as OpenAI tools (schemas()), and call() returns a plain
string — the tools loop feeds it back as a tool message and never sees RPC.
"""
import json
import os
import subprocess
import sys
import threading


class _Session:
    """One stdio JSON-RPC session to a child server process."""

    def __init__(self, script: str, timeout: float) -> None:
        self.script = script
        self.timeout = timeout
        self.proc = None
        self._rid = 0
        self._lock = threading.Lock()
        self.tools = []       # raw MCP tool descriptors from tools/list
        self._pending = {}    # id -> {"lines": [...]}  (server is line-oriented)
        self._reader = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        self.proc = subprocess.Popen(
            [sys.executable, self.script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, env=env,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        info = self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "chenvoice-pool", "version": "1.0"},
        })
        if info is None:
            raise RuntimeError(f"MCP server failed to initialize: {self.script}")
        self._notify("notifications/initialized", {})
        listed = self._rpc("tools/list", {})
        self.tools = (listed or {}).get("tools", [])

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()

    # --- IO ----------------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                rid = msg.get("id")
                with self._lock:
                    slot = self._pending.get(rid)
                if slot is not None:
                    slot["lines"].append(msg)
        except Exception:
            pass  # process died; next rpc() restarts or errors out

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except Exception:
            pass

    def _rpc(self, method: str, params: dict):
        """Request/response with timeout. Returns result dict or None."""
        if not self.alive():
            return None
        with self._lock:
            self._rid += 1
            rid = self._rid
            slot = {"lines": []}
            self._pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid,
                        "method": method, "params": params})
        except Exception:
            with self._lock:
                self._pending.pop(rid, None)
            return None
        waited = 0.0
        while waited < self.timeout:
            with self._lock:
                if slot["lines"]:
                    msg = slot["lines"].pop(0)
                    self._pending.pop(rid, None)
                    return msg.get("result") if "error" not in msg else None
            import time
            time.sleep(0.02)
            waited += 0.02
        with self._lock:
            self._pending.pop(rid, None)
        return None  # timeout: caller phrases it; the hung child is killed on close

    def call_tool(self, name: str, arguments: dict) -> str | None:
        """tools/call -> result text, or None on transport failure."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result is None:
            return None
        for item in result.get("content", []):
            if item.get("type") == "text":
                return item.get("text", "")
        return ""


class ToolPool:
    """Aggregates several MCP servers into one OpenAI tools surface."""

    def __init__(self, servers: list, timeout: float = 5.0) -> None:
        self._scripts = list(servers)
        self._timeout = timeout
        self._sessions: dict = {}   # tool name -> _Session
        self._by_script: dict = {}  # script -> _Session
        self._lock = threading.Lock()

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Prewarm every server and index its tools by name."""
        for script in self._scripts:
            sess = self._ensure(script)
            for tool in sess.tools:
                self._sessions[tool["name"]] = sess

    def alive(self) -> bool:
        return any(s.alive() for s in self._by_script.values())

    def close(self) -> None:
        for sess in self._by_script.values():
            sess.close()

    def _ensure(self, script: str) -> _Session:
        with self._lock:
            sess = self._by_script.get(script)
            if sess is not None and sess.alive():
                return sess
            sess = _Session(script, self._timeout)
            sess.start()
            self._by_script[script] = sess
            return sess

    # --- LLM surface -------------------------------------------------------

    def schemas(self) -> list:
        """OpenAI tools format, aggregated across servers."""
        out = []
        for name, sess in self._sessions.items():
            for tool in sess.tools:
                if tool.get("name") != name:
                    continue
                out.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("inputSchema",
                                               {"type": "object", "properties": {}}),
                    },
                })
        return out

    def call(self, name: str, arguments: dict) -> str:
        """Run one tool. Always returns speakable text — errors included."""
        with self._lock:
            sess = self._sessions.get(name)
            script = sess.script if sess else None
        if sess is None:
            return f"没有这个工具：{name}"
        try:
            if not sess.alive():
                sess = self._ensure(script)  # crashed: re-warm and re-index
                for tool in sess.tools:
                    self._sessions[tool["name"]] = sess
            text = sess.call_tool(name, arguments or {})
        except Exception as e:
            return f"工具调用失败：{e}"
        if text is None:
            # Timeout or transport death. Kill so the next call gets a fresh
            # process rather than a wedged one.
            sess.close()
            return f"工具 {name} 超时或无响应"
        return text
