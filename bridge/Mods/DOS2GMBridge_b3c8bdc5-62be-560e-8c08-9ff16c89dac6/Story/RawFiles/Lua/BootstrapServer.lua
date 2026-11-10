-- Server (the host is the Game Master): loads the bridge and drives it on every tick.
Ext.Require("Bridge.lua")

Ext.Events.Tick:Subscribe(function()
    Bridge.OnTick()
end)

Ext.Events.SessionLoaded:Subscribe(function()
    Bridge.OnSessionLoaded()
end)
