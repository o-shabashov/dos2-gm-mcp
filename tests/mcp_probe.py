"""Starts the server over stdio and checks the MCP handshake, the tool list and one call.

    uv run python tests/mcp_probe.py                      # this project through uv
    uv run python tests/mcp_probe.py docker run -i --rm IMAGE   # any other command
"""
import json
import subprocess
import sys
from pathlib import Path

cmd = sys.argv[1:] or ["uv", "--directory", str(Path(__file__).resolve().parents[1]), "run", "--no-sync", "dos2-gm-mcp"]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                     encoding="utf-8")


def send(obj):
    p.stdin.write(json.dumps(obj) + "\n")
    p.stdin.flush()


def recv():
    while True:
        line = p.stdout.readline()
        if not line:
            raise SystemExit("server closed: " + p.stderr.read()[-2000:])
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)


fails = []
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}}})
init = recv()["result"]
print("server:", init["serverInfo"])
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools = recv()["result"]["tools"]
print("tools:", len(tools))
if len(tools) < 30:
    fails.append("too few tools")
send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "dos2_status", "arguments": {}}})
r = recv()
text = r["result"]["content"][0]["text"] if "result" in r else json.dumps(r)
print("dos2_status:", text[:300])
if "campaigns_dir" not in text:
    fails.append("dos2_status gave no paths")
p.terminate()
print("FAILS:", fails or "none")
raise SystemExit(1 if fails else 0)
