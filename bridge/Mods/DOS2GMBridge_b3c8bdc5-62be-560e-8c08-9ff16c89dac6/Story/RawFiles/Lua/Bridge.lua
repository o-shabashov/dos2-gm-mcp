-- Bridge for the dos2-gm MCP server.
--
-- It talks over files in "Documents\Larian Studios\Divinity Original Sin 2 Definitive Edition\Osiris Data\dos2gm-mcp":
--   in.json      a command from the MCP server: {seq, cmd, args} or {seq, cancelled = true}
--   out.json     the bridge's reply: {ack_seq, cmd, result} or {ack_seq, cmd, error}
--   status.json  a heartbeat every ~2 s: version, game mode, level, Osiris readiness
-- Ext.IO can only read and write files, so instead of deleting them there is a seq counter: a command runs
-- when its seq is greater than the last acknowledged one. After a level reload the bridge takes that counter
-- from out.json and does not run the previous command again.

Bridge = {}

local VERSION = 3
local DIR = "dos2gm-mcp/"
local IN, OUT, STATUS = DIR .. "in.json", DIR .. "out.json", DIR .. "status.json"
local POLL_EVERY = 3        -- ticks between reads of in.json
local STATUS_EVERY = 60     -- polls between heartbeats

local lastSeq = nil
local tick = 0
local polls = 0

local function stringify(v)
    return Ext.Json.Stringify(v)
end

local function readJson(path)
    local raw = Ext.IO.LoadFile(path)
    if raw == nil or raw == "" then return nil end
    local ok, value = pcall(Ext.Json.Parse, raw)
    if ok and type(value) == "table" then return value end
    return nil
end

local function safe(fn, ...)
    local ok, value = pcall(fn, ...)
    if ok then return value end
    return nil
end

local function currentLevel()
    return safe(function() return Ext.Entity.GetCurrentLevel().LevelDesc.LevelName end)
end

-- Osiris is ready only once a level is loaded. While a campaign is loading, Ext.OsirisIsCallable already says
-- "yes", yet an Osi.* call crashes Script Extender (verified 2026-11-10 when starting a campaign from the menu).
local LIVE = {Running = true, Paused = true, GameMasterPause = true}

local function osirisReady()
    if safe(function() return Ext.Osiris.IsCallable() end) ~= true then return false end
    local st = safe(Ext.Server.GetGameState)
    return st == nil or LIVE[tostring(st)] == true
end

-- During GM pause Osi defers world actions (spawning, teleporting, CharacterSetCustomName) until unpause:
-- verified in the game 2026-11-10. Entity fields (CustomDisplayName) change immediately.
local function gmPaused()
    return tostring(safe(Ext.Server.GetGameState)) == "GameMasterPause"
end

local function setName(guid, name)
    Osi.CharacterSetCustomName(guid, name)
    pcall(function() Ext.Entity.GetCharacter(guid).CustomDisplayName = name end)
end

local function withPause(result)
    if gmPaused() then result.applies_on_unpause = true end
    return result
end

-- Character description for the MCP reply.
local function describe(guid)
    local c = Ext.Entity.GetCharacter(guid)
    if c == nil then return {guid = guid, missing = true} end
    local pos = safe(function() return {table.unpack(c.WorldPos)} end)
    local name = safe(function() return c.CustomDisplayName end)
    if name == nil or name == "" then
        name = safe(function() local _, text = Osi.CharacterGetDisplayName(guid); return text end)
    end
    return {
        guid = safe(function() return c.MyGuid end) or guid,
        name = name,
        template = safe(Osi.GetTemplate, guid),
        stats = safe(function() return c.Stats.Name end),
        level = safe(function() return c.Stats.Level end),
        vitality = safe(function() return c.Stats.CurrentVitality end),
        max_vitality = safe(function() return c.Stats.MaxVitality end),
        alignment = safe(Osi.GetFaction, guid),
        region = safe(function() return c.CurrentLevel end),
        pos = pos,
    }
end

local Commands = {}

function Commands.ping(args)
    return {version = VERSION, mode = safe(Ext.Utils.GetGameMode), level = currentLevel(), osiris = osirisReady(),
            state = tostring(safe(Ext.Server.GetGameState))}
end

-- Characters on the level: all of them, or those within args.radius of args.x, args.y, args.z.
function Commands.characters(args)
    local guids
    if args.x and args.radius then
        guids = Ext.Entity.GetCharacterGuidsAroundPosition(args.x, args.y or 0, args.z, args.radius)
    else
        guids = Ext.Entity.GetAllCharacterGuids(args.level or currentLevel())
    end
    local out = {}
    local limit = args.limit or 500
    for _, guid in pairs(guids or {}) do
        out[#out + 1] = describe(guid)
        if #out >= limit then break end
    end
    return out
end

function Commands.describe(args)
    return describe(args.guid)
end

-- A new character from a root template at the point x, y, z.
function Commands.spawn(args)
    assert(args.template, "template is required: a root template GUID")
    assert(args.x and args.y and args.z, "coordinates x, y, z are required")
    local guid = Osi.CharacterCreateAtPosition(args.x, args.y, args.z, args.template, args.play_spawn and 1 or 0)
    assert(guid ~= nil and guid ~= "", "CharacterCreateAtPosition returned an empty GUID for " .. tostring(args.template))
    if args.name then setName(guid, args.name) end
    if args.level then Osi.CharacterLevelUpTo(guid, args.level) end
    if args.alignment then Osi.SetFaction(guid, args.alignment) end
    return withPause(describe(guid))
end

function Commands.move(args)
    assert(args.guid, "guid is required")
    Osi.TeleportToPosition(args.guid, args.x, args.y, args.z, "", 0, 1)
    return withPause(describe(args.guid))
end

-- Edits an already placed character: name, level (up only), alignment.
function Commands.set(args)
    assert(args.guid, "guid is required")
    if args.name then setName(args.guid, args.name) end
    if args.level then Osi.CharacterLevelUpTo(args.guid, args.level) end
    if args.alignment then Osi.SetFaction(args.guid, args.alignment) end
    return withPause(describe(args.guid))
end

-- Take off stage (on = false) or bring back (on = true); an off-stage character stays in the level.
function Commands.stage(args)
    assert(args.guid, "guid is required")
    Osi.SetOnStage(args.guid, args.on and 1 or 0)
    return {guid = args.guid, on_stage = args.on and true or false}
end

-- Arbitrary Lua: a function body; the value of return goes into the reply.
function Commands.lua(args)
    assert(type(args.code) == "string", "code is required")
    local fn, err = load(args.code, "=mcp", "t", _ENV)
    assert(fn, err)
    return fn()
end

local function reply(msg, result, err)
    local out = {ack_seq = msg.seq, cmd = msg.cmd}
    if err ~= nil then out.error = tostring(err) else out.result = result end
    local ok, text = pcall(stringify, out)
    if not ok then
        text = stringify({ack_seq = msg.seq, cmd = msg.cmd, error = "the result is not JSON-serializable: " .. tostring(text)})
    end
    Ext.IO.SaveFile(OUT, text)
end

local function writeStatus()
    local ok, text = pcall(stringify, {
        version = VERSION, mode = safe(Ext.Utils.GetGameMode), level = currentLevel(),
        osiris = osirisReady(), last_seq = lastSeq, tick = tick, state = tostring(safe(Ext.Server.GetGameState)),
    })
    if ok then Ext.IO.SaveFile(STATUS, text) end
end

local function handle(msg)
    if msg.cancelled then
        reply(msg, nil, "cancelled by MCP")
        return
    end
    local cmd = Commands[msg.cmd]
    if cmd == nil then
        reply(msg, nil, "unknown command " .. tostring(msg.cmd))
        return
    end
    local ok, result = pcall(cmd, msg.args or {})
    if ok then reply(msg, result, nil) else reply(msg, nil, result) end
end

function Bridge.OnSessionLoaded()
    Ext.Utils.Print("[dos2gm-mcp] bridge v" .. VERSION .. " loaded, mode " .. tostring(safe(Ext.Utils.GetGameMode)))
    writeStatus()
end

local tickErrors = 0

local function poll()
    polls = polls + 1
    if lastSeq == nil then
        local last = readJson(OUT)
        lastSeq = last and tonumber(last.ack_seq) or 0
        writeStatus()
    end
    if polls % STATUS_EVERY == 0 then writeStatus() end

    local msg = readJson(IN)
    local seq = msg and tonumber(msg.seq)
    if seq == nil or seq <= lastSeq then return end
    -- Commands wait until Osiris is ready (level loading, main menu); a cancellation goes through at once.
    if not msg.cancelled and msg.cmd ~= "ping" and not osirisReady() then return end
    lastSeq = seq
    handle(msg)
end

function Bridge.OnTick()
    tick = tick + 1
    if tick % POLL_EVERY ~= 0 then return end
    local ok, err = pcall(poll)
    if not ok then
        tickErrors = tickErrors + 1
        if tickErrors <= 3 then Ext.Utils.Print("[dos2gm-mcp] tick error: " .. tostring(err)) end
    end
end

-- For tests outside the game.
Bridge._Commands = Commands
Bridge._Reset = function() lastSeq = nil; tick = 0; polls = 0 end

return Bridge
