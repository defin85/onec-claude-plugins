#!/usr/bin/env python3
"""Короткая проверка языкового сервера BSL по stdio.

Запускает `bsl-analyzer-app --stdio` в каталоге --root, выполняет initialize и,
если указан --file, запросы documentSymbol, hover, definition, references и
workspace/symbol. Печатает JSON-отчёт. Ничего не пишет в --root сам; что пишет
сервер, зависит от анализатора (кеши индекса). Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


class Client:
    def __init__(self, cmd: list[str], cwd: str):
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.inbox: queue.Queue = queue.Queue()
        self.next_id = 0
        self.progress: list[dict] = []
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        for line in self.proc.stderr:
            self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())
            del self.stderr_tail[:-20]

    def _read(self) -> None:
        out = self.proc.stdout
        while True:
            headers = {}
            while True:
                line = out.readline()
                if not line:
                    self.inbox.put(None)
                    return
                line = line.decode("ascii", "replace").strip()
                if not line:
                    break
                key, _, value = line.partition(":")
                headers[key.lower()] = value.strip()
            body = out.read(int(headers.get("content-length", "0")))
            self.inbox.put(json.loads(body))

    def send(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict | None, timeout: float) -> tuple[dict | None, float]:
        self.next_id += 1
        rid = self.next_id
        started = time.monotonic()
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self.send(payload)
        deadline = started + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.inbox.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if msg is None:
                raise RuntimeError("сервер закрыл stdout: " + " | ".join(self.stderr_tail[-5:]))
            self._handle_server_message(msg)
            if msg.get("id") == rid and "method" not in msg:
                return msg, time.monotonic() - started
        return None, time.monotonic() - started

    def _handle_server_message(self, msg: dict) -> None:
        method = msg.get("method")
        if method == "$/progress":
            value = msg.get("params", {}).get("value", {})
            self.progress.append({"t": round(time.monotonic(), 2), "kind": value.get("kind"),
                                  "title": value.get("title"), "message": value.get("message")})
        if method and "id" in msg:  # запрос сервера к клиенту: ответить пустым результатом
            result = None
            if method == "workspace/configuration":
                result = [None for _ in msg.get("params", {}).get("items", [])]
            self.send({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def wait_index(self, seconds: float) -> float | None:
        """Ждать завершения первого прогресса индексации; вернуть время или None."""
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            if any(p["kind"] == "end" for p in self.progress):
                return time.monotonic() - started
            self.pump(0.5)
        return None

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                msg = self.inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if msg is None:
                return
            self._handle_server_message(msg)

    def close(self) -> None:
        try:
            self.request("shutdown", None, 5)
            self.notify("exit")
            self.proc.wait(timeout=5)
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.kill()


def summarize(result) -> object:
    if isinstance(result, list):
        return {"count": len(result), "first": result[:3]}
    if isinstance(result, dict) and "contents" in result:
        contents = result["contents"]
        text = contents.get("value") if isinstance(contents, dict) else str(contents)
        return {"hover": (text or "")[:300]}
    return result


def find_position(text: str, needle: str) -> dict | None:
    """Позиция имени: сначала в объявлении процедуры или функции, иначе первое вхождение."""
    lines = text.splitlines()
    starts = ("процедура", "функция", "procedure", "function", "асинх", "async")
    for prefer_decl in (True, False):
        for line_no, line in enumerate(lines):
            col = line.find(needle)
            if col < 0:
                continue
            head = line.lstrip().lower()
            if prefer_decl and not head.startswith(starts):
                continue
            if not prefer_decl and head.startswith("//"):
                continue
            return {"line": line_no, "character": col + 1}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="корень рабочего пространства (где bsl-analyzer.toml)")
    parser.add_argument("--file", help="BSL-файл для запросов навигации")
    parser.add_argument("--symbol", help="имя символа в --file для hover/definition/references")
    parser.add_argument("--query", default="", help="запрос workspace/symbol")
    parser.add_argument("--timeout", type=float, default=60.0, help="таймаут одного запроса, с")
    parser.add_argument("--settle", type=float, default=0.0, help="ожидание индексации после didOpen, с")
    parser.add_argument("--wait-index", type=float, default=0.0,
                        help="ждать окончания индексации ($/progress end) не дольше N с")
    parser.add_argument("--server", default=shutil.which("bsl-analyzer-app") or "bsl-analyzer-app")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    report: dict = {"server": args.server, "root": str(root)}
    started = time.monotonic()
    client = Client([args.server, "--stdio"], str(root))
    try:
        init, elapsed = client.request("initialize", {
            "processId": os.getpid(),
            "rootUri": root.as_uri(),
            "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
            "capabilities": {
                "window": {"workDoneProgress": True},
                "workspace": {"workspaceFolders": True, "configuration": True, "symbol": {}},
                "textDocument": {"hover": {"contentFormat": ["markdown", "plaintext"]},
                                 "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                                 "definition": {}, "references": {}},
            },
        }, args.timeout)
        report["initialize_seconds"] = round(elapsed, 2)
        if not init or "result" not in init:
            report["error"] = init or "initialize timeout"
            return 1
        caps = init["result"].get("capabilities", {})
        report["serverInfo"] = init["result"].get("serverInfo")
        report["providers"] = sorted(k for k, v in caps.items() if k.endswith("Provider") and v)
        client.notify("initialized", {})
        if args.wait_index:
            waited = client.wait_index(args.wait_index)
            report["index_seconds"] = round(waited, 2) if waited is not None else f">{args.wait_index}"

        if args.file:
            path = Path(args.file).resolve()
            text = path.read_text(encoding="utf-8-sig")
            uri = path.as_uri()
            client.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": "bsl", "version": 1, "text": text}})
            if args.settle:
                client.pump(args.settle)
            doc = {"textDocument": {"uri": uri}}
            res, t = client.request("textDocument/documentSymbol", doc, args.timeout)
            report["documentSymbol"] = {"seconds": round(t, 2), "result": summarize(res and res.get("result")),
                                        "error": res and res.get("error")}
            pos = find_position(text, args.symbol) if args.symbol else None
            if pos:
                for method, extra in (("textDocument/hover", {}), ("textDocument/definition", {}),
                                      ("textDocument/references", {"context": {"includeDeclaration": True}})):
                    res, t = client.request(method, {**doc, "position": pos, **extra}, args.timeout)
                    report[method.split("/")[1]] = {"seconds": round(t, 2),
                                                    "result": summarize(res and res.get("result")),
                                                    "error": res and res.get("error"),
                                                    "timeout": res is None}
        res, t = client.request("workspace/symbol", {"query": args.query}, args.timeout)
        report["workspaceSymbol"] = {"seconds": round(t, 2), "result": summarize(res and res.get("result")),
                                     "error": res and res.get("error"), "timeout": res is None}
        report["progress"] = client.progress[-10:]
        report["total_seconds"] = round(time.monotonic() - started, 2)
        return 0
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        client.close()
        report["stderr_tail"] = client.stderr_tail[-5:]
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
