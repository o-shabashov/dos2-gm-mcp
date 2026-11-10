"""Live layer: commands to the running game through the bridge mod (bridge/).

Talks over files in Osiris Data/dos2gm-mcp; the format is described in Bridge.lua.
"""
import json
import os
import time
import uuid
from pathlib import Path

from .gm import GMError

BRIDGE_UUID = "b3c8bdc5-62be-560e-8c08-9ff16c89dac6"
BRIDGE_FOLDER = f"DOS2GMBridge_{BRIDGE_UUID}"
BRIDGE_NAME = "DOS2 GM MCP Bridge"
BRIDGE_VERSION = 268435456  # 1.0.0.0
ALIVE_SECONDS = 10


class Bridge:
    def __init__(self, osiris_data: Path):
        self.dir = osiris_data / "dos2gm-mcp"
        self._seq = 0

    def _read(self, name: str) -> dict | None:
        try:
            return json.loads((self.dir / name).read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, PermissionError, UnicodeDecodeError):
            return None

    def _write_in(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f"in.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        for _ in range(50):  # the game may hold the file open for reading
            try:
                os.replace(tmp, self.dir / "in.json")
                return
            except PermissionError:
                time.sleep(0.02)
        tmp.unlink(missing_ok=True)
        raise GMError("Could not write in.json: the file is busy")

    def status(self) -> dict:
        st = self._read("status.json")
        f = self.dir / "status.json"
        age = time.time() - f.stat().st_mtime if f.exists() else None
        return {"dir": str(self.dir), "alive": age is not None and age < ALIVE_SECONDS,
                "seconds_since_pulse": round(age, 1) if age is not None else None, "pulse": st}

    def _next_seq(self) -> int:
        prev = max(self._seq, (self._read("out.json") or {}).get("ack_seq") or 0,
                   (self._read("in.json") or {}).get("seq") or 0)
        self._seq = max(int(time.time() * 1000), int(prev) + 1)
        return self._seq

    def call(self, cmd: str, args: dict | None = None, timeout: float = 5.0):
        seq = self._next_seq()
        self._write_in({"seq": seq, "cmd": cmd, "args": args or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self._read("out.json")
            if out and out.get("ack_seq") == seq:
                if "error" in out:
                    raise GMError(f"Bridge: {out['error']}")
                return out.get("result")
            time.sleep(0.03)
        self._write_in({"seq": seq, "cancelled": True})
        st = self.status()
        why = ("the bridge is alive but the command did not go through: Osiris is not ready yet "
               "(is a level loading?)" if st["alive"] else
               "no bridge heartbeat: the game is not running, no campaign is loaded, Script Extender is missing, "
               "or the bridge is not attached to the campaign as an add-on")
        raise GMError(f"The bridge did not answer within {timeout:g} s: {why}. The command was cancelled.")
