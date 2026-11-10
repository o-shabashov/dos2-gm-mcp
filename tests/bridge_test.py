"""The bridge outside the game: the real BootstrapServer.lua and Bridge.lua in Lua 5.3 (lupa) with Ext and Osi
stubs, against the real live.Bridge client, through files in a temporary folder.

uv run --no-sync python tests/bridge_test.py

The last block (MCP tools on top of the bridge) reads a GM template from a stub campaign that the test builds in
the temporary folder. Building it needs Divine.exe from LSLib; without it the template checks are skipped.
"""
import dataclasses
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import lupa.lua53 as lupa_lua

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dos2gm.gm import GMError  # noqa: E402
from dos2gm.live import BRIDGE_FOLDER, Bridge  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
LUA_DIR = ROOT / "bridge" / "Mods" / BRIDGE_FOLDER / "Story" / "RawFiles" / "Lua"
storage = Path(tempfile.mkdtemp(prefix="osiris-data-"))
lua = lupa_lua.LuaRuntime(unpack_returned_tuples=True)
lock = threading.RLock()


def to_py(v):
    if lupa_lua.lua_type(v) == "table":
        keys = list(v.keys())
        if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
            return [to_py(v[k]) for k in sorted(keys)]
        if not keys:
            return []
        return {str(k): to_py(v[k]) for k in keys}
    return v


def to_lua(v):
    if isinstance(v, (dict, list)):
        return lua.table_from(v, recursive=True)
    return v


def load_file(path, _ctx=None):
    # Like Ext.IO.LoadFile: nil on any read error (the file is being replaced right now).
    try:
        return (storage / path).read_text("utf-8")
    except OSError:
        return None


def save_file(path, text, _len=None):
    f = storage / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, "utf-8")
    return True


MOCK = r"""
local py = ...
World = {
  ready = true,
  chars = {
    ["3859d997-53d4-4001-aaf9-0e858d84d2fa"] = {MyGuid = "3859d997-53d4-4001-aaf9-0e858d84d2fa", WorldPos = {15.1, 1.2, 7.8}, CustomDisplayName = "Skipper Finn",
                      Stats = {Name = "GEN_HumanCitizen", Level = 10, CurrentVitality = 314, MaxVitality = 314},
                      CurrentLevel = "GM_Town_Beach_Harbor_A", template = "Humans_Male_6aca1d99", faction = "Neutral NPC"},
    ["2168f04a-e229-45ef-879c-46b870c335f0"] = {MyGuid = "2168f04a-e229-45ef-879c-46b870c335f0", WorldPos = {14.5, 1.2, 3.4}, CustomDisplayName = "",
                    Stats = {Name = "GM_Fire_Demon", Level = 8, CurrentVitality = 218, MaxVitality = 218},
                    CurrentLevel = "GM_Town_Beach_Harbor_A", template = "Imp_af727a2f", faction = "Evil NPC"},
  },
  created = 0, subs = {},
}
Ext = {
  IO = {LoadFile = py.load_file, SaveFile = py.save_file},
  Json = {Stringify = py.stringify, Parse = py.parse},
  Utils = {GetGameMode = function() return "GameMaster" end, Print = py.print},
  OsirisIsCallable = function() return World.ready end,
  Osiris = {IsCallable = function() return World.ready end},
  Server = {GetGameState = function() return World.state or "Running" end},
  Entity = {
    GetCurrentLevel = function() return {LevelDesc = {LevelName = "GM_Town_Beach_Harbor_A"}} end,
    GetAllCharacterGuids = function(level)
      local out = {}
      for g, c in pairs(World.chars) do if level == nil or c.CurrentLevel == level then out[#out + 1] = g end end
      table.sort(out); return out
    end,
    GetCharacterGuidsAroundPosition = function(x, y, z, r)
      local out = {}
      for g, c in pairs(World.chars) do
        local dx, dz = c.WorldPos[1] - x, c.WorldPos[3] - z
        if dx * dx + dz * dz <= r * r then out[#out + 1] = g end
      end
      table.sort(out); return out
    end,
    GetCharacter = function(g) return World.chars[g] end,
  },
  Events = {
    Tick = {Subscribe = function(self, fn) World.subs.tick = fn end},
    SessionLoaded = {Subscribe = function(self, fn) World.subs.loaded = fn end},
  },
  Require = function(name)
    local fn = assert(loadfile(py.lua_dir .. "/" .. name, "t", _ENV)); return fn()
  end,
}
Osi = {
  CharacterCreateAtPosition = function(x, y, z, tpl, spawn)
    if not tpl:match("^%x+%-") then return nil end
    World.created = World.created + 1
    local g = "new-" .. World.created
    World.chars[g] = {MyGuid = g, WorldPos = {x, y, z}, CustomDisplayName = "", template = "Tpl_" .. tpl,
                      Stats = {Name = "GEN", Level = 1, CurrentVitality = 50, MaxVitality = 50},
                      CurrentLevel = "GM_Town_Beach_Harbor_A", faction = "Neutral NPC"}
    return g
  end,
  CharacterSetCustomName = function(g, t) World.chars[g].CustomDisplayName = t end,
  CharacterLevelUpTo = function(g, l) local s = World.chars[g].Stats; if l > s.Level then s.Level = l end end,
  SetFaction = function(g, f) World.chars[g].faction = f end,
  GetFaction = function(g) return World.chars[g].faction end,
  GetTemplate = function(g) return World.chars[g].template end,
  CharacterGetDisplayName = function(g) return "h123", "Fire Incarnate" end,
  TeleportToPosition = function(g, x, y, z) World.chars[g].WorldPos = {x, y, z} end,
  SetOnStage = function(g, on) World.chars[g].on_stage = on end,
}
"""

py = lua.table_from({
    "load_file": load_file, "save_file": save_file, "lua_dir": str(LUA_DIR).replace("\\", "/"),
    "stringify": lambda v: json.dumps(to_py(v), ensure_ascii=False),
    "parse": lambda s: to_lua(json.loads(s)),
    "print": lambda *a: print("   [lua]", *a),
})
lua.execute(MOCK, py)
lua.globals().Ext.Require("BootstrapServer.lua")
world = lua.globals().World
world.subs.loaded()

stop = threading.Event()


def ticker():
    while not stop.is_set():
        with lock:
            world.subs.tick()
        time.sleep(0.003)


threading.Thread(target=ticker, daemon=True).start()
bridge = Bridge(storage)
results, fails = {}, []

ROOT_TEMPLATE = "9b0e1879-e7fc-48e0-9c96-f1b6023ec0a9"
STUB_CAMPAIGN = "Test Campaign"
STUB_FOLDER = "Test_Campaign_5f0c7f0e-2f0b-4c59-9a55-6d1f1c1d2e01"


def check(name, cond, info=None):
    results[name] = "ok" if cond else f"FAIL {info}"
    if not cond:
        fails.append(name)


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except GMError as e:
        return str(e)
    return None


def stub_campaign(g, campaigns_dir):
    """A minimal campaign with one GM NPC template named "Bandit" (level 12, Evil NPC)."""
    from dos2gm import lsx
    cdir = campaigns_dir / STUB_FOLDER
    cdir.mkdir(parents=True)
    meta = lsx.empty_save("Config")
    lsx.children(lsx.region_node(meta, "Config"), create=True).append(lsx.node("ModuleInfo", [
        ("Name", lsx.T_FIXED, STUB_CAMPAIGN), ("UUID", lsx.T_FIXED, STUB_FOLDER[-36:])]))
    eggs = lsx.empty_save("GameMasterManager")
    lsx.children(lsx.region_node(eggs, "GameMasterManager"), create=True).append(lsx.node("GMExportCharacter", [
        ("Name", lsx.T_WSTRING, "Bandit"), ("TemplateID", lsx.T_FIXED, ROOT_TEMPLATE),
        ("Level", lsx.T_INT, 12), ("Alignment", lsx.T_FIXED, "Evil NPC")]))
    for name, tree in (("meta.lsf", meta), ("Eggs.lsf", eggs)):
        shutil.move(g.divine.build(tree), cdir / name)


try:
    p = bridge.call("ping")
    check("ping", p["mode"] == "GameMaster" and p["level"] == "GM_Town_Beach_Harbor_A" and p["osiris"] is True, p)
    time.sleep(0.05)
    st = bridge.status()
    check("pulse", st["alive"] and st["pulse"]["version"] == 3, st)

    chars = bridge.call("characters")
    check("characters", [c["name"] for c in chars] == ["Fire Incarnate", "Skipper Finn"], chars)
    skipper = chars[1]
    check("character fields", skipper["level"] == 10 and skipper["alignment"] == "Neutral NPC"
          and skipper["pos"] == [15.1, 1.2, 7.8] and skipper["stats"] == "GEN_HumanCitizen", skipper)
    near = bridge.call("characters", {"x": 15, "z": 8, "radius": 2})
    check("characters near", [c["guid"] for c in near] == ["3859d997-53d4-4001-aaf9-0e858d84d2fa"], near)

    new = bridge.call("spawn", {"template": ROOT_TEMPLATE, "x": 13, "y": 1.2, "z": 2,
                                "name": "Bandit", "level": 12, "alignment": "Evil NPC"})
    check("spawn", new["name"] == "Bandit" and new["level"] == 12 and new["alignment"] == "Evil NPC"
          and new["pos"] == [13, 1.2, 2], new)
    moved = bridge.call("move", {"guid": new["guid"], "x": 10, "y": 1.2, "z": 5})
    check("move", moved["pos"] == [10, 1.2, 5], moved)
    renamed = bridge.call("set", {"guid": new["guid"], "name": "Troll", "level": 3})
    check("set (level only up)", renamed["name"] == "Troll" and renamed["level"] == 12, renamed)
    check("stage", bridge.call("stage", {"guid": new["guid"], "on": False}) == {"guid": new["guid"], "on_stage": False})
    # GM pause: spawn and move are flagged, the name is written straight into the character's field.
    # The name is non-ASCII on purpose: it has to survive the JSON round trip through the files.
    with lock:
        world.state = "GameMasterPause"
    paused = bridge.call("spawn", {"template": ROOT_TEMPLATE, "x": 2, "y": 1, "z": 2,
                                   "name": "Jörmungandr"})
    check("pause flag on spawn", paused.get("applies_on_unpause") is True and paused["name"] == "Jörmungandr", paused)
    check("ping shows state", bridge.call("ping")["state"] == "GameMasterPause")
    with lock:
        world.state = None
    moved2 = bridge.call("move", {"guid": paused["guid"], "x": 3, "y": 1, "z": 3})
    check("no pause flag when running", "applies_on_unpause" not in moved2, moved2)
    check("lua", bridge.call("lua", {"code": "return 1 + 2"}) == 3)
    check("lua sees world", bridge.call("lua", {"code": "return Osi.GetFaction('2168f04a-e229-45ef-879c-46b870c335f0')"}) == "Evil NPC")
    err = raises(bridge.call, "lua", {"code": "error('boom')"})
    check("lua error comes back", err and "boom" in err, err)
    err = raises(bridge.call, "spawn", {"template": "no-such-template", "x": 0, "y": 0, "z": 0})
    check("spawn error comes back", err and "empty GUID" in err, err)
    err = raises(bridge.call, "teleport_all")
    check("unknown command", err and "unknown command" in err, err)

    # Osiris is not ready: the command waits, is cancelled on timeout and never runs afterwards.
    with lock:
        world.ready = False
        before = world.created
    err = raises(bridge.call, "spawn", {"template": ROOT_TEMPLATE, "x": 0, "y": 0, "z": 0},
                 timeout=0.3)
    check("waits while osiris busy", err and "Osiris" in err, err)
    with lock:
        world.ready = True
    time.sleep(0.1)
    with lock:
        check("cancelled command never runs", world.created == before, (before, world.created))

    # A fresh Lua state (level reload): the previous command is not run again.
    last = bridge.call("spawn", {"template": ROOT_TEMPLATE, "x": 1, "y": 1, "z": 1})
    with lock:
        count = world.created
        lua.globals().Bridge._Reset()
    time.sleep(0.1)
    with lock:
        check("no replay after reload", world.created == count, (count, world.created))
    check("works after reload", bridge.call("ping")["version"] == 3)
    check("last spawn returned guid", last["guid"].startswith("new-"), last)

    # MCP tools on top of the bridge; the "Bandit" GM template is read from a stub campaign in the temp folder.
    from dos2gm import paths, server as s
    from dos2gm.gm import GM
    s._bridge = bridge
    near = s.dos2_live_characters(near="Skipper Finn", radius=2)
    check("tool characters near", [c["name"] for c in near] == ["Skipper Finn"], near)
    cfg = dataclasses.replace(paths.load(), campaigns=storage / "GMCampaigns")
    if cfg.divine.exists():
        s._gm = GM(cfg)
        stub_campaign(s._gm, cfg.campaigns)
        b = s.dos2_live_spawn("Bandit", campaign=STUB_CAMPAIGN, near="Skipper Finn", dx=2, dz=-1)
        check("tool spawn from GM template", b["name"] == "Bandit" and b["level"] == 12 and b["alignment"] == "Evil NPC"
              and b["pos"] == [17.1, 1.2, 6.8], b)
        check("tool move near", s.dos2_live_move("Bandit", near="2168f04a-e229-45ef-879c-46b870c335f0", dx=0, dz=1)["pos"] == [14.5, 1.2, 4.4])
        check("tool set", s.dos2_live_set("Bandit", alignment="Neutral NPC")["alignment"] == "Neutral NPC")
    else:
        for name in ("tool spawn from GM template", "tool move near", "tool set"):
            results[name] = f"skipped: no Divine.exe at {cfg.divine} (set DOS2GM_DIVINE)"
    err = raises(s.dos2_live_move, "Nobody", x=0, y=0, z=0)
    check("tool unknown name", err and "not found" in err, err)
    check("tool lua", s.dos2_live_lua("return Ext.Utils.GetGameMode()") == "GameMaster")
finally:
    stop.set()
    time.sleep(0.02)
    shutil.rmtree(storage, ignore_errors=True)

print(json.dumps(results, ensure_ascii=False, indent=1))
print("FAILS:", fails or "none")
sys.exit(1 if fails else 0)
