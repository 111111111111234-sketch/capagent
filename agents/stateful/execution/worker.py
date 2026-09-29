"""JSON-only subprocess worker for the existing straight-line Python subset.

The child interprets whitelisted AST nodes; it never evals/execs model text.
Backend objects, credentials and ledger handles stay in the parent process.
This is process separation with a restricted language, not an OS sandbox.
"""

from __future__ import annotations

import ast
import json
import math
import os
import selectors
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_MESSAGE = 131072
MAX_OUTPUT = 16384


def encode(value):
    chunks, size = [], 1
    for piece in json.JSONEncoder(allow_nan=False, ensure_ascii=False).iterencode(value):
        chunk = piece.encode()
        size += len(chunk)
        if size > MAX_MESSAGE:
            raise ValueError("worker message exceeds limit")
        chunks.append(chunk)
    return b"".join(chunks) + b"\n"


class Interpreter:
    def __init__(self, inputs, allowed, rpc):
        self.variables = {"INPUTS": inputs, "RESULT": None}
        self.allowed, self.rpc = set(allowed), rpc
        self.output = ""
        self.truncated = False

    def expression(self, node):
        if isinstance(node, ast.Constant):
            if type(node.value) not in {str, int, float, bool, type(None)}:
                raise ValueError("unsupported literal")
            return node.value
        if isinstance(node, ast.Name) and not node.id.startswith("_"):
            return self.variables[node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.expression(item) for item in node.elts]
        if isinstance(node, ast.Dict) and all(key is not None for key in node.keys):
            return {self.expression(key): self.expression(value) for key, value in zip(node.keys, node.values)}
        if isinstance(node, ast.Subscript):
            return self.expression(node.value)[self.expression(node.slice)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            value = self.expression(node.operand)
            if type(value) not in {int, float}:
                raise ValueError("unary minus requires a number")
            return -value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            args = [self.expression(arg) for arg in node.args]
            if any(kw.arg is None for kw in node.keywords):
                raise ValueError("keyword expansion is unsupported")
            kwargs = {kw.arg: self.expression(kw.value) for kw in node.keywords}
            if name == "print":
                if not set(kwargs) <= {"sep", "end"}:
                    raise ValueError("print only supports sep and end")
                sep, end = kwargs.get("sep", " "), kwargs.get("end", "\n")
                if not isinstance(sep, str) or not isinstance(end, str):
                    raise ValueError("print sep and end must be strings")
                for index, value in enumerate(args):
                    encode(value)  # Bound nested/aliased structures before formatting them.
                    self.write_output((sep if index else "") + str(value))
                self.write_output(end)
                return None
            if name in self.allowed:
                return self.rpc(name, args, kwargs)
        raise ValueError(f"unsupported expression: {type(node).__name__}")

    def write_output(self, value):
        room = MAX_OUTPUT - len(self.output.encode())
        data = value.encode()
        self.truncated |= len(data) > room
        self.output += data[:room].decode(errors="ignore")

    def run(self, code):
        if len(code.encode()) > 16000:
            raise ValueError("code exceeds limit")
        tree = ast.parse(code)
        if not tree.body or sum(1 for _ in ast.walk(tree)) > 4096:
            raise ValueError("empty or oversized syntax tree")
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                value = self.expression(statement.value)
                encode(value)  # Reject explosive alias expansion before the next statement.
                for target in statement.targets:
                    if (not isinstance(target, ast.Name) or target.id.startswith("_")
                            or target.id in self.allowed | {"INPUTS", "print"}):
                        raise ValueError("invalid assignment target")
                    self.variables[target.id] = value
            elif isinstance(statement, ast.Expr):
                self.expression(statement.value)
            else:
                raise ValueError(f"unsupported statement: {type(statement).__name__}")
        result = self.variables["RESULT"]
        if len(encode(result)) > 65536:
            raise ValueError("RESULT exceeds limit")
        return result


def child_main():
    import resource

    request = json.loads(sys.stdin.buffer.readline(MAX_MESSAGE + 1))
    cpu = max(1, math.ceil(request["timeout_s"])) + 1
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    # macOS does not consistently support an address-space limit. The AST and
    # JSON bounds apply there; the supervisor still enforces elapsed time.
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))

    def send(value):
        sys.stdout.buffer.write(encode(value))
        sys.stdout.buffer.flush()

    def rpc(name, args, kwargs):
        send({"type": "call", "name": name, "args": args, "kwargs": kwargs})
        reply = json.loads(sys.stdin.buffer.readline(MAX_MESSAGE + 1))
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["result"]

    interpreter = Interpreter(request["inputs"], request["allowed"], rpc)
    try:
        result = interpreter.run(request["code"])
        send({"type": "finished", "result": result, "stdout": interpreter.output,
              "truncated": interpreter.truncated, "error": None})
    except Exception as exc:
        send({"type": "finished", "result": None, "stdout": interpreter.output,
              "truncated": interpreter.truncated, "error": f"{type(exc).__name__}: {exc}"[:4096]})


def run_worker(code, inputs, allowed, *, call, checkpoint, timeout_s):
    """The parent alone invokes call(), so API ownership/thread affinity is preserved."""
    from .backend import ExecutionFault

    failure = None
    with tempfile.TemporaryDirectory(prefix="capx-worker-") as directory:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=directory, env={"PATH": os.defpath, "LC_ALL": "C"},
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        buffer = b""
        try:
            process.stdin.write(encode({"code": code, "inputs": inputs, "allowed": list(allowed), "timeout_s": timeout_s}))
            process.stdin.flush()
            while True:
                checkpoint()
                if b"\n" not in buffer:
                    if not selector.select(0.05):
                        continue
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        raise ExecutionFault("WORKER_EXITED", "worker exited without a final result")
                    buffer += chunk
                    if len(buffer) > MAX_MESSAGE:
                        raise ExecutionFault("WORKER_PROTOCOL_ERROR", "worker message too large")
                    continue
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                if message.get("type") == "call":
                    name = message.get("name")
                    if (set(message) != {"type", "name", "args", "kwargs"} or name not in allowed
                            or not isinstance(message["args"], list) or not isinstance(message["kwargs"], dict)):
                        raise ExecutionFault("WORKER_PROTOCOL_ERROR", "unapproved API call")
                    try:
                        if failure:
                            raise failure
                        value = call(name, *message["args"], **message["kwargs"])
                        reply = encode({"ok": True, "result": value})
                    except Exception as exc:
                        failure = exc if isinstance(exc, ExecutionFault) else ExecutionFault("API_RESULT_UNSUPPORTED", str(exc))
                        reply = encode({"ok": False, "error": str(failure)[:4096]})
                    process.stdin.write(reply)
                    process.stdin.flush()
                elif message.get("type") == "finished":
                    if set(message) != {"type", "result", "stdout", "truncated", "error"}:
                        raise ExecutionFault("WORKER_PROTOCOL_ERROR")
                    process.wait(timeout=1)
                    if process.returncode != 0:
                        raise ExecutionFault("WORKER_EXITED")
                    message["failure"] = failure
                    return message
                else:
                    raise ExecutionFault("WORKER_PROTOCOL_ERROR")
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    child_main()
