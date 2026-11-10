"""dos2-gm MCP server: DOS2 Game Master mode campaigns through files and LSLib."""
import json
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import lsx, paths
from .gm import GM, GMError, game_running, stable_uuid
from .live import BRIDGE_FOLDER, BRIDGE_NAME, BRIDGE_UUID, BRIDGE_VERSION, Bridge

INSTRUCTIONS = """\
Game Master mode campaigns of Divinity: Original Sin 2 (GMCampaigns). The campaign parameter accepts a campaign
name, a folder name, a UUID, or a part of any of them.

Reading is safe at any time. Writing (dos2_put_*, dos2_delete_*, dos2_restore) is only for a closed campaign:
the game is closed or sitting in the main menu. While the game process is running, writes require
allow_running=true; set it only after the user has confirmed the game is in the main menu. Every write first
backs up the previous files; dos2_backups lists the backups, dos2_restore brings one back.

NPCs: the easy way is to base a template on one from any campaign (dos2_find_templates → base="Campaign:Name"),
then change its name, level, alignment, GM stat edits (bonus is added to the base stats), skills and items.
NPCs placed in scenes live in LevelCache: they can be read (dos2_scene); scenes themselves are edited in the game.

The live layer (dos2_live_*) works in the running game through a bridge mod: it needs Script Extender, the bridge
built and attached to the campaign (dos2_bridge_install), and the campaign loaded. Start with dos2_live_status:
it shows what is missing.
"""

mcp = MCPServer("dos2-gm", instructions=INSTRUCTIONS)
_gm: GM | None = None


def gm() -> GM:
    global _gm
    if _gm is None:
        _gm = GM(paths.load())
    return _gm


# --- overview -----------------------------------------------------------------------

@mcp.tool()
def dos2_status() -> dict:
    """Paths (campaigns, Divine.exe, backups), whether the game is running, and the list of GM campaigns."""
    g = gm()
    p = g.paths
    out = {"campaigns_dir": str(p.campaigns), "divine": str(p.divine), "divine_found": p.divine.exists(),
           "backups_dir": str(p.backups), "game_running": game_running()}
    try:
        out["campaigns"] = g.campaigns()
    except GMError as e:
        out["error"] = str(e)
    return out


@mcp.tool()
def dos2_campaign(campaign: str) -> dict:
    """Campaign summary: metadata and add-ons, scenes, maps with pins, vignettes, templates, sticky notes, journal."""
    g = gm()
    d = g.resolve(campaign)
    _, top = g.eggs(d)
    return {
        "meta": g.meta(d),
        "scenes": g.scenes(d),
        "maps": g.maps(d),
        "vignettes": [{"uuid": v["uuid"], "title": v["title"], "answers": v["answers"]} for v in g.vignettes(d)],
        "templates": [{"kind": s["kind"], "name": s["name"], "level": s.get("level")}
                      for s in map(g.template_summary, lsx.kids(top))],
        "notes": [{"title": n["title"], "kind": n["kind"], "folder": n["folder"]} for n in g.notes(d)],
        "journal": g.journal(d),
    }


@mcp.tool()
def dos2_campaign_meta(campaign: str, name: str | None = None, description: str | None = None,
                       author: str | None = None, allow_running: bool = False) -> dict:
    """Changes the campaign name, description and author in meta.lsf (same as editing campaign metadata in the game).
    The campaign folder keeps its name: the game identifies the campaign by it."""
    g = gm()
    d = g.resolve(campaign)
    if name is None and description is None and author is None:
        return {"changed": [], "meta": g.meta(d)}
    w = g.writer(d, "campaign-meta", allow_running)
    meta = g.read(d, "meta.lsf")
    info = lsx.kid(lsx.region_node(meta, "Config"), "ModuleInfo")
    for aid, typ, val in (("Name", lsx.T_FIXED, name), ("Description", lsx.T_WSTRING, description),
                          ("Author", lsx.T_WSTRING, author)):
        if val is not None:
            lsx.set_attr(info, aid, typ, val)
    w.stage("meta.lsf", meta)
    return {**w.commit(), "meta": g.meta(d)}


@mcp.tool()
def dos2_scene(campaign: str, scene: str, limit: int = 200) -> dict:
    """Scene contents from LevelCache: characters (name, root template, position, vitality, experience, alignment,
    encounter), encounters with their members, item count, the scene's sticky notes. scene is a scene name or ID."""
    g = gm()
    d = g.resolve(campaign)
    return g.scene_detail(d, g.resolve_scene(d, scene), limit)


@mcp.tool()
def dos2_templates(campaign: str, kind: str | None = None) -> list[dict]:
    """Campaign templates from Eggs.lsf: kind = "npc", "item", or empty for all."""
    g = gm()
    _, top = g.eggs(g.resolve(campaign))
    out = [g.template_summary(t) for t in lsx.kids(top)]
    return [s for s in out if not kind or s["kind"] == kind]


@mcp.tool()
def dos2_template(campaign: str, name: str) -> dict:
    """A full template: base stats, GM edits, abilities, talents, skills, skill bar, items."""
    g = gm()
    t = g.find_template(g.resolve(campaign), name)
    if t is None:
        raise GMError(f"No template '{name}'. List them with dos2_templates.")
    return g.template_detail(t)


@mcp.tool()
def dos2_find_templates(query: str = "", campaign: str | None = None, kind: str | None = None,
                        limit: int = 30) -> list[dict]:
    """Searches templates in all campaigns (or in one) by a substring of the name, StatName, root template,
    skills and items. A hit can be used as base for dos2_put_npc_template in the form "Campaign:Name"."""
    return gm().search_templates(query, campaign, kind, limit)


@mcp.tool()
def dos2_vignettes(campaign: str) -> list[dict]:
    """The campaign's vignettes in full: title, text, question, answers, image."""
    g = gm()
    return g.vignettes(g.resolve(campaign))


@mcp.tool()
def dos2_notes(campaign: str) -> list[dict]:
    """GM sticky notes: on an Overview map (kind=map) and in scenes, bound to an object (kind=scene)."""
    g = gm()
    return g.notes(g.resolve(campaign))


@mcp.tool()
def dos2_raw(campaign: str, file: str, xpath: str | None = None, max_chars: int = 20000) -> str:
    """A campaign file as LSX text, for exploring the formats. file is a path inside the campaign, e.g.
    "Eggs.lsf" or "LevelCache/<scene ID>.lsf". xpath is an ElementTree expression, e.g.
    ".//node[@id='Note']"; without it the whole file is returned. The output is cut to max_chars."""
    g = gm()
    d = g.resolve(campaign)
    target = (d / file).resolve()
    if not target.is_relative_to(d.resolve()) or not target.is_file():
        raise GMError(f"No file {file} in campaign {d.name}")
    root = g.divine.read(target)
    parts = [lsx.to_text(e) for e in root.findall(xpath)] if xpath else [lsx.to_text(root)]
    text = f"<!-- matches: {len(parts)} -->\n" + "\n".join(parts) if xpath else parts[0]
    return text if len(text) <= max_chars else text[:max_chars] + f"\n<!-- truncated, {len(text)} characters in total -->"


# --- NPC templates ---------------------------------------------------------------------

@mcp.tool()
def dos2_put_npc_template(
    campaign: str,
    name: str,
    base: str | None = None,
    template_id: str | None = None,
    stat_name: str | None = None,
    level: int | None = None,
    alignment: str | None = None,
    archetype: str | None = None,
    trader: bool | None = None,
    display_name: str | None = None,
    bonus: dict[str, int] | None = None,
    ability_bonus: dict[str, int] | None = None,
    skills: list[str] | None = None,
    items: list[dict] | None = None,
    items_from: str | None = None,
    reset_bonus: bool = False,
    talents: list[str] | None = None,
    remove_talents: list[str] | None = None,
    allow_running: bool = False,
) -> dict:
    """Creates or changes an NPC template (the GM's saved templates in the "Creatures and Items" panel).

    name: template name; a template with this name in the campaign is replaced.
    base: the template to copy from, "Campaign:Name" or just a name (then it is looked up in all campaigns).
      Without base, the existing template with this name is changed.
    template_id: root template (model, race, gender). stat_name: stats entry, e.g. GEN_HumanCitizen.
    level: NPC level. alignment: "Neutral NPC", "Evil NPC" or "Good NPC". trader: whether the NPC is a trader.
    display_name: the name shown above the head, overriding name.
    bonus: GM additions to the base stats, e.g. {"Constitution": 4, "Strength": 2}; 0 removes the edit.
    ability_bonus: the same for abilities, e.g. {"TwoHanded": 3, "WarriorLore": 2}.
    skills: the complete skill list, e.g. ["Target_SingleHandedAttack", "Shout_EnemyTaunt"]; sets the skills and
      fills the skill bar in that order.
    items: the complete item list: {"stats_id", "template_id", "amount", "slot", "level", "name"}; slot is a
      number or weapon/shield/helmet/breast/leggings/boots/gloves/belt/amulet/ring/ring2, backpack if omitted.
      Such items are written without a stats blob, like weapons in community-made templates.
    items_from: "Campaign:Name" of a template whose items are copied as is (with their stats blobs).
    reset_bonus: first drop all GM edits of the base (stats, abilities, talents), then apply your own.
    talents / remove_talents: talents by their ositools names, e.g. ["Executioner", "Leech"].
    """
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "npc-template", allow_running)
    root, top = g.eggs(d)
    base_node = g.template_ref(base) if base else None
    items_node = g.template_ref(items_from) if items_from else None
    t, created = g.put_template(
        top, name, base_node, template_id=template_id, stat_name=stat_name, level=level, alignment=alignment,
        archetype=archetype, trader=trader, display_name=display_name, bonus=bonus, ability_bonus=ability_bonus,
        skills=skills, items=items, items_from=items_node, reset_bonus=reset_bonus, talents=talents,
        remove_talents=remove_talents)
    detail = g.template_detail(t)
    w.stage("Eggs.lsf", root)
    result = w.commit()
    return {"created": created, "template": detail, **result}


@mcp.tool()
def dos2_put_item_template(
    campaign: str,
    name: str,
    base: str = "quest",
    template_id: str | None = None,
    stats_id: str | None = None,
    level: int | None = None,
    rarity: str | None = None,
    description: str | None = None,
    book: str | None = None,
    amount: int | None = None,
    boosts: list[str] | None = None,
    allow_running: bool = False,
) -> dict:
    """Creates or replaces an item template (the GM's saved item templates in the "Creatures and Items" panel).

    base: a built-in sample: quest (not equipment), weapon, weapon_generated (has ItemGeneration: real properties
    from boosts, e.g. Boost_Weapon_Damage_Bonus_Large), armor; or "Campaign:Name" of an existing item template.
    template_id: the item's root template (model). stats_id: stats entry (WPN_Axe_2H, LOOT_Ore_A_BlackRock_A…).
    rarity: Common, Uncommon, Rare, Epic, Legendary, Divine, Unique. description: item description.
    book: text of a book or note.
    """
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "item-template", allow_running)
    root, top = g.eggs(d)
    base_node = g.item_sample(base) if base in ("quest", "weapon", "weapon_generated", "armor") else g.template_ref(base)
    t, created = g.put_item_template(top, name, base_node, template_id=template_id, stats_id=stats_id, level=level,
                                     rarity=rarity, description=description, book=book, amount=amount, boosts=boosts)
    detail = g.template_summary(t)
    w.stage("Eggs.lsf", root)
    return {"created": created, "template": detail, **w.commit()}


@mcp.tool()
def dos2_put_journal(campaign: str, categories: list[dict], allow_running: bool = False) -> dict:
    """Writes the whole GM journal (the previous one goes to a backup).

    categories: [{"title": "...", "shared": false, "chapters": [{"title": "...", "shared": false,
    "paragraphs": [{"text": "...", "shared": false}]}]}]. List order = order in the journal.
    shared=true shows the entry to players (not verified against samples; the GM can also enable it in the game)."""
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "journal", allow_running)
    w.stage("Journal.lsf", g.journal_root(d, categories))
    out = w.commit()
    return {**out, "journal": g.journal(d)}


@mcp.tool()
def dos2_scene_settings(campaign: str, scene: str, name: str | None = None, folder: str | None = None,
                        atmosphere: str | None = None, ambient: str | None = None, music: str | None = None,
                        allow_running: bool = False) -> dict:
    """Changes a scene in GMScenes.lsf: name, folder, atmosphere (a GUID from ATM_*), ambient sound (Soundvol_*) and
    music (GM_*). An empty string restores the level's default value."""
    g = gm()
    d = g.resolve(campaign)
    s = g.resolve_scene(d, scene)
    w = g.writer(d, "scene-settings", allow_running)
    w.stage("GMScenes.lsf", g.set_scene(d, s, name=name, folder=folder, atmosphere=atmosphere, ambient=ambient,
                                        music=music))
    out = w.commit()
    root = g.read(d, "GMScenes.lsf")
    entry = next(lsx.attrs(x) for x in lsx.kids(lsx.region_node(root, "GameMasterScenes"), "GameMasterScene")
                 if lsx.get(x, "ID") == s["id"])
    return {**out, "scene": entry}


@mcp.tool()
def dos2_delete_template(campaign: str, name: str, allow_running: bool = False) -> dict:
    """Deletes an NPC or item template by name. The previous Eggs.lsf goes to a backup."""
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "delete-template", allow_running)
    root, top = g.eggs(d)
    removed = g.remove_where(top, "GMExportCharacter", lambda n: g.template_name(n).strip().lower() == name.strip().lower())
    removed += g.remove_where(top, "GMExportItem", lambda n: g.template_name(n).strip().lower() == name.strip().lower())
    if not removed:
        raise GMError(f"No template '{name}'")
    w.stage("Eggs.lsf", root)
    return {"removed": len(removed), **w.commit()}


# --- vignettes ---------------------------------------------------------------------------

@mcp.tool()
def dos2_put_vignette(
    campaign: str,
    title: str,
    text: str,
    answers: list[str] | None = None,
    question: str = "",
    image: str | None = None,
    stock_image: str = "GM_Vignette_Beach_Generic_A.png",
    folder: str = "",
    key: str | None = None,
    canvas: str = "GM_paperVersion_01.png",
    frame: str = "GM_Border_Deco_01_A.png",
    allow_running: bool = False,
) -> dict:
    """Creates or replaces a vignette.

    The vignette to replace is found by key (the UUID is derived from the key, so the same key always addresses
    the same vignette), otherwise by an equal title. image: path to your own PNG (a square with a transparent
    background, like Larian's); the file is copied to Vignettes/Illustrations and Previews. Without image the
    stock picture stock_image is used. folder: the vignette's folder in the resources list. answers: the answer
    options offered to players.
    """
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "vignette", allow_running)
    root, top = g.vignettes_root(d)
    if key:
        uid = stable_uuid("vignette", key)
    else:
        same = [v for v in g.vignettes(d) if (v["title"] or "").strip().lower() == title.strip().lower()]
        uid = same[0]["uuid"] if same else stable_uuid("vignette", title)
    size = None
    img_name = stock_image
    if image:
        from PIL import Image
        src = Path(image)
        if not src.is_file():
            raise GMError(f"No image {image}")
        with Image.open(src) as im:
            size = im.size
        img_name = src.name
        w.stage_copy(src, f"Vignettes/Illustrations/{src.name}")
        w.stage_copy(src, f"Vignettes/Illustrations/Previews/{src.name}")
    node = g.vignette_node(uid, title, text, answers or [], question, folder, img_name, size, canvas, frame)
    created = g.upsert(top, "Vignette", uid, node)
    w.stage("Vignettes.lsf", root)
    return {"created": created, "uuid": uid, "image": img_name, "image_size": size, **w.commit()}


@mcp.tool()
def dos2_delete_vignette(campaign: str, vignette: str, allow_running: bool = False) -> dict:
    """Deletes a vignette by UUID or title. Images in Illustrations are kept."""
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "delete-vignette", allow_running)
    root, top = g.vignettes_root(d)
    q = vignette.strip().lower()
    removed = g.remove_where(top, "Vignette", lambda n: q in ((lsx.get(n, "UUID") or "").lower(),
                                                               (lsx.get(n, "VignetteTitle") or "").strip().lower()))
    if not removed:
        raise GMError(f"No vignette '{vignette}'")
    w.stage("Vignettes.lsf", root)
    return {"removed": removed, **w.commit()}


# --- sticky notes -------------------------------------------------------------------------------

@mcp.tool()
def dos2_put_note(
    campaign: str,
    title: str,
    text: str,
    folder: str | None = None,
    map: str | None = None,
    x: float | None = None,
    y: float | None = None,
    key: str | None = None,
    allow_running: bool = False,
) -> dict:
    """Creates or changes a GM sticky note (players do not see it).

    A new note goes onto an Overview map: map is the map's title or UUID (from dos2_campaign), x and y are map
    pixels (600, 400 if omitted). A note in a scene is attached to an invisible marker item of the level, so new
    scene notes have to be placed in the game; for one that already exists, this tool changes the title, text
    and folder and keeps the binding. An existing note is found by key, otherwise by title. folder: the folder
    in the notes overview.
    """
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "note", allow_running)
    notes = g.notes(d)
    if key:
        uid = stable_uuid("note", key)
        old = next((n for n in notes if n["uuid"] == uid), None)
    else:
        old = next((n for n in notes if (n["title"] or "").strip().lower() == title.strip().lower()), None)
        uid = old["uuid"] if old else stable_uuid("note", title)

    if map is not None or old is None or old["kind"] == "map":
        maps = g.maps(d)
        if map is None and old is None:
            if len(maps) != 1:
                raise GMError(f"Specify map: {', '.join(m['title'] for m in maps) or 'the campaign has no maps'}")
            map = maps[0]["uuid"]
        if map is not None:
            mq = map.strip().lower()
            hit = [m for m in maps if mq in ((m["uuid"] or "").lower(), (m["title"] or "").strip().lower())]
            if not hit:
                raise GMError(f"No map '{map}'. Available: {', '.join(m['title'] for m in maps)}")
            map_uuid, map_title = hit[0]["uuid"], hit[0]["title"]
        else:
            map_uuid, map_title = old["map"], next((m["title"] for m in maps if m["uuid"] == old["map"]), old["map"])
        nx = x if x is not None else (old["x"] if old and old["kind"] == "map" else 600)
        ny = y if y is not None else (old["y"] if old and old["kind"] == "map" else 400)
        kw = {"map_uuid": map_uuid, "x": nx, "y": ny}
        target = {"map": map_title, "x": nx, "y": ny}
    else:
        kw = {"scene_id": old["scene"] or "", "bound_uuid": old["bound_to"] or ""}
        target = {"scene": old["scene"], "bound_to": old["bound_to"]}

    root, top = g.notes_root(d)
    keep_folder = (old["folder"] or "") if old else ""
    node = g.note_node(uid, title, text, folder if folder is not None else keep_folder, **kw)
    created = g.upsert(top, "Note", uid, node)
    w.stage("Notes.lsf", root)
    return {"created": created, "uuid": uid, **target, **w.commit()}


@mcp.tool()
def dos2_delete_note(campaign: str, note: str, allow_running: bool = False) -> dict:
    """Deletes a sticky note by UUID or title."""
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "delete-note", allow_running)
    root, top = g.notes_root(d)
    q = note.strip().lower()
    removed = g.remove_where(top, "Note", lambda n: q in ((lsx.get(n, "UUID") or "").lower(),
                                                         (lsx.get(n, "NoteTitle") or "").strip().lower()))
    if not removed:
        raise GMError(f"No sticky note '{note}'")
    w.stage("Notes.lsf", root)
    return {"removed": removed, **w.commit()}


# --- backups ----------------------------------------------------------------------------------

@mcp.tool()
def dos2_backup(campaign: str) -> dict:
    """Full copy of the campaign folder into the backups."""
    g = gm()
    d = g.resolve(campaign)
    bid = g.full_backup(d)
    return {"backup": bid, "path": str(g.backup_root(d) / bid)}


@mcp.tool()
def dos2_backups(campaign: str) -> list[dict]:
    """The campaign's backups, newest first: what was saved and which files the operation created."""
    g = gm()
    return g.backups(g.resolve(campaign))


@mcp.tool()
def dos2_restore(campaign: str, backup: str, allow_running: bool = False) -> dict:
    """Restores the files from a backup and deletes the files that operation created.
    The current state of those files is itself backed up first, labelled "before-restore"."""
    g = gm()
    return g.restore(g.resolve(campaign), backup, allow_running)


# --- live layer: the bridge mod in the running game -------------------------------------------------

BRIDGE_SRC = paths.PROJECT / "bridge"
GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_bridge: Bridge | None = None


def bridge() -> Bridge:
    global _bridge
    if _bridge is None:
        _bridge = Bridge(gm().paths.osiris_data)
    return _bridge


def _who(who: str) -> dict:
    """An in-game character by GUID or name (exact match, otherwise a unique substring)."""
    if GUID_RE.search(who):
        return bridge().call("describe", {"guid": who})
    chars = bridge().call("characters", {"limit": 5000}) or []
    q = who.strip().lower()
    exact = [c for c in chars if (c.get("name") or "").strip().lower() == q]
    hits = exact or [c for c in chars if q in (c.get("name") or "").lower()]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(sorted({c.get("name") or c["guid"] for c in (hits or chars)}))[:600]
    raise GMError(f"Character '{who}' {'not found' if not hits else 'is ambiguous, pass a GUID'}. On the level: {names}")


def _where(x, y, z, near, dx, dz) -> tuple[float, float, float]:
    if near:
        px, py, pz = _who(near)["pos"]
        return px + dx, py, pz + dz
    if None in (x, y, z):
        raise GMError("Specify x, y, z or near (the name of a character to place next to)")
    return x, y, z


@mcp.tool()
def dos2_live_status() -> dict:
    """Live layer readiness: whether the game is running, Script Extender is installed, the bridge is built, which
    campaigns it is attached to, and whether its heartbeat is coming in (the bridge writes it every ~2 s while a
    campaign is loaded)."""
    g = gm()
    p = g.paths
    se = {"game_dir": str(p.game) if p.game else None}
    if p.game:
        bin_ = p.game / "DefEd" / "bin"
        settings = bin_ / "OsirisExtenderSettings.json"
        se.update(eocapp=(bin_ / "EoCApp.exe").exists(), dxgi=(bin_ / "DXGI.dll").exists(),
                  settings=json.loads(settings.read_text("utf-8")) if settings.exists() else None)
    pak = p.mods / f"{BRIDGE_FOLDER}.pak"
    attached = []
    for d in g.campaign_dirs():
        try:
            if BRIDGE_NAME in g.meta(d)["addons"]:
                attached.append(d.name)
        except Exception:
            pass
    return {"game_running": game_running(), "script_extender": se,
            "bridge_pak": str(pak) if pak.exists() else None, "attached_to": attached, "bridge": bridge().status()}


@mcp.tool()
def dos2_bridge_install(campaign: str | None = None, allow_running: bool = False) -> dict:
    """Builds the bridge mod into Mods\\DOS2GMBridge_….pak and, if campaign is given, attaches it to the campaign
    as an add-on (meta.lsf → Dependencies, with a backup). Players need this .pak too, like any add-on."""
    g = gm()
    pak = g.paths.mods / f"{BRIDGE_FOLDER}.pak"
    files = g.divine.package(BRIDGE_SRC, pak)
    out = {"pak": str(pak), "files": files}
    if campaign:
        d = g.resolve(campaign)
        w = g.writer(d, "bridge-attach", allow_running)
        meta = g.read(d, "meta.lsf")
        if g.set_addon(meta, BRIDGE_UUID, BRIDGE_NAME, BRIDGE_VERSION, BRIDGE_FOLDER, True):
            w.stage("meta.lsf", meta)
        out.update(campaign=d.name, addons=None, **w.commit())
        out["addons"] = g.meta(d)["addons"]
    return out


@mcp.tool()
def dos2_mod_install(source: str, campaign: str | None = None, allow_running: bool = False) -> dict:
    """Builds a mod from the source folder (it contains Mods/<mod folder>/meta.lsx) into Mods\\<mod folder>.pak and,
    if campaign is given, attaches it to the campaign as an add-on. UUID, name, version and folder are taken from
    the mod's meta.lsx. Note: PublishVersion in meta.lsx must be a Definitive Edition editor version (e.g.
    909321303), otherwise the game marks the mod as made for the Classic Edition and does not enable it."""
    import xml.etree.ElementTree as ET
    g = gm()
    src = Path(source)
    metas = list(src.glob("Mods/*/meta.lsx"))
    if len(metas) != 1:
        raise GMError(f"{src} must contain exactly one Mods/<folder>/meta.lsx, found {len(metas)}")
    info = ET.parse(metas[0]).getroot().find(".//node[@id='ModuleInfo']")
    a = {x.get("id"): x.get("value") for x in info.findall("attribute")}
    pub = info.find(".//node[@id='PublishVersion']/attribute[@id='Version']")
    if pub is None or int(pub.get("value", "0")) < 0x36000000:
        raise GMError("PublishVersion in meta.lsx is below 3.6: the game will treat the mod as Classic. Set it to 909321303.")
    pak = g.paths.mods / f"{a['Folder']}.pak"
    files = g.divine.package(src, pak)
    out = {"pak": str(pak), "files": files, "module": a["Name"], "uuid": a["UUID"]}
    if campaign:
        d = g.resolve(campaign)
        w = g.writer(d, "mod-attach", allow_running)
        meta = g.read(d, "meta.lsf")
        if g.set_addon(meta, a["UUID"], a["Name"], int(a["Version"]), a["Folder"], True):
            w.stage("meta.lsf", meta)
        out.update(campaign=d.name, **w.commit())
        out["addons"] = g.meta(d)["addons"]
    return out


@mcp.tool()
def dos2_addon(campaign: str, pak: str, attach: bool = True, allow_running: bool = False) -> dict:
    """Attaches a ready-made third-party mod from a .pak file to the campaign (attach=False detaches it). The file
    is copied to Mods; UUID, name, version and folder are taken from meta.lsx inside the pak. Players need this
    .pak too."""
    import os
    import shutil
    import subprocess
    import tempfile
    import xml.etree.ElementTree as ET
    g = gm()
    src = Path(pak)
    if not src.is_file():
        raise GMError(f"No file {src}")
    tmp = Path(tempfile.mkdtemp(prefix="dos2gm-addon-"))
    env = dict(os.environ, DOTNET_ROLL_FORWARD="Major")
    subprocess.run([str(g.paths.divine), "-g", "dos2de", "-a", "extract-package", "-s", str(src), "-d", str(tmp),
                    "-x", "*/meta.lsx"], check=True, capture_output=True, env=env)
    metas = list(tmp.glob("Mods/*/meta.lsx"))
    if len(metas) != 1:
        raise GMError(f"{src.name} must contain exactly one Mods/<folder>/meta.lsx, found {len(metas)}")
    info = ET.parse(metas[0]).getroot().find(".//node[@id='ModuleInfo']")
    a = {x.get("id"): x.get("value") for x in info.findall("attribute")}
    shutil.rmtree(tmp, ignore_errors=True)
    d = g.resolve(campaign)
    w = g.writer(d, "addon-attach" if attach else "addon-detach", allow_running)
    dst = g.paths.mods / src.name
    if attach and src.resolve() != dst.resolve():
        shutil.copyfile(src, dst)
    meta = g.read(d, "meta.lsf")
    if g.set_addon(meta, a["UUID"], a["Name"], int(a["Version"]), a["Folder"], attach):
        w.stage("meta.lsf", meta)
    out = w.commit()
    return {"module": a["Name"], "author": a.get("Author"), "uuid": a["UUID"], "pak": str(dst), **out,
            "addons": g.meta(d)["addons"]}


@mcp.tool()
def dos2_bridge_detach(campaign: str, allow_running: bool = False) -> dict:
    """Detaches the bridge mod from the campaign (meta.lsf → Dependencies, with a backup). The .pak stays in Mods."""
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "bridge-detach", allow_running)
    meta = g.read(d, "meta.lsf")
    if g.set_addon(meta, BRIDGE_UUID, BRIDGE_NAME, BRIDGE_VERSION, BRIDGE_FOLDER, False):
        w.stage("meta.lsf", meta)
    out = w.commit()
    return {**out, "addons": g.meta(d)["addons"]}


@mcp.tool()
def dos2_live_characters(near: str | None = None, radius: float = 8.0, limit: int = 200) -> list[dict]:
    """Characters on the loaded level: GUID, name, root template, stats, level, vitality, alignment, position.
    near: a character's name or GUID; then only those within radius metres of that character."""
    if near:
        x, y, z = _who(near)["pos"]
        return bridge().call("characters", {"x": x, "y": y, "z": z, "radius": radius, "limit": limit})
    return bridge().call("characters", {"limit": limit})


@mcp.tool()
def dos2_live_spawn(
    template: str,
    campaign: str | None = None,
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    near: str | None = None,
    dx: float = 1.5,
    dz: float = 0.0,
    name: str | None = None,
    level: int | None = None,
    alignment: str | None = None,
) -> dict:
    """Spawns a character on the loaded level.

    template: a root template GUID, or the name of a GM template from campaign (then the model, level, alignment
    and name come from that template; its stat edits, skills and items are NOT carried over this way: only
    placing it from the "Creatures and Items" panel in the game applies them). Position: x, y, z, or near (a
    character's name/GUID) with offset dx, dz. During GM pause the character is created at once but moves into
    place only after unpausing: the result then has applies_on_unpause=true and the position is still 0, 0, 0
    (verified in the game).
    """
    if not GUID_RE.search(template):
        if not campaign:
            raise GMError("A template name is looked up in a campaign: specify campaign")
        t = gm().find_template(gm().resolve(campaign), template, kind="npc")
        if t is None:
            raise GMError(f"No NPC template '{template}' in campaign {campaign}")
        a = lsx.attrs(t)
        template = a["TemplateID"]
        name = name or a.get("CustomDisplayName") or a.get("Name")
        level = level or int(a.get("Level") or 0) or None
        alignment = alignment or a.get("Alignment")
    px, py, pz = _where(x, y, z, near, dx, dz)
    return bridge().call("spawn", {"template": template, "x": px, "y": py, "z": pz, "name": name,
                                   "level": level, "alignment": alignment})


@mcp.tool()
def dos2_live_move(who: str, x: float | None = None, y: float | None = None, z: float | None = None,
                   near: str | None = None, dx: float = 1.5, dz: float = 0.0) -> dict:
    """Moves a character (name or GUID) to the point x, y, z, or next to the character near with offset dx, dz.
    During GM pause the move is applied after unpausing (applies_on_unpause=true in the result)."""
    guid = _who(who)["guid"]
    px, py, pz = _where(x, y, z, near, dx, dz)
    return bridge().call("move", {"guid": guid, "x": px, "y": py, "z": pz})


@mcp.tool()
def dos2_live_set(who: str, name: str | None = None, level: int | None = None,
                  alignment: str | None = None) -> dict:
    """Changes a placed character: name above the head, level (up only), alignment ("Evil NPC" etc.)."""
    guid = _who(who)["guid"]
    return bridge().call("set", {"guid": guid, "name": name, "level": level, "alignment": alignment})


@mcp.tool()
def dos2_live_stage(who: str, on: bool) -> dict:
    """Takes a character off stage (on=false) or brings it back (on=true). An off-stage character stays in the level."""
    guid = _who(who)["guid"]
    return bridge().call("stage", {"guid": guid, "on": on})


@mcp.tool()
def dos2_live_lua(code: str, timeout: float = 5.0):
    """Runs Lua on the game server (Script Extender, in the bridge mod's context) and returns the value of return.
    Ext and Osi are available, e.g.: return Osi.CharacterGetHostCharacter(). The result must be JSON-serializable."""
    return bridge().call("lua", {"code": code}, timeout=timeout)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
