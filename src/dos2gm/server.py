"""dos2-gm MCP server: DOS2 Game Master mode campaigns through files and LSLib."""
import json
import re
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

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

CAMPAIGN = ("The campaign to work on: its name, its folder name, its UUID, or a unique part of any of them. "
            "dos2_status lists the campaigns.")
ALLOW_RUNNING = ("Keep false. Set true only after the user has confirmed that the running game sits in the main "
                 "menu: a write into a campaign that is loaded in the game gets lost or damages it.")


def tool(title: str, /, *, read_only: bool = False, destructive: bool = False, idempotent: bool = False,
         open_world: bool = False, **params: str):
    """Registers an MCP tool with a title, behaviour annotations and a description for each parameter."""
    def decorator(fn):
        unknown = set(params) - set(fn.__annotations__)
        if unknown:
            raise TypeError(f"{fn.__name__}: no such parameters {sorted(unknown)}")
        for p, text in params.items():
            fn.__annotations__[p] = Annotated[fn.__annotations__[p], Field(description=text)]
        notes = ToolAnnotations(title=title, read_only_hint=read_only, destructive_hint=destructive,
                                idempotent_hint=idempotent or read_only, open_world_hint=open_world)
        return mcp.tool(title=title, annotations=notes)(fn)
    return decorator


def gm() -> GM:
    global _gm
    if _gm is None:
        _gm = GM(paths.load())
    return _gm


# --- overview -----------------------------------------------------------------------

@tool("Server status and campaign list", read_only=True)
def dos2_status() -> dict:
    """Reports the server's setup and lists the GM campaigns. Returns the campaigns folder, the path to Divine.exe and
    whether it exists, the backups folder, whether the game process is running, and for every campaign its name,
    folder, UUID, add-ons, scene count and last-modified time.

    Read-only, no parameters. Call it first in a session to choose a campaign and check setup: if divine_found is
    false, run dos2_fetch_lslib before any other tool. For the readiness of the running game (Script Extender,
    bridge mod) use dos2_live_status instead.
    """
    g = gm()
    p = g.paths
    out = {"campaigns_dir": str(p.campaigns), "divine": str(p.divine), "divine_found": p.divine.exists(),
           "backups_dir": str(p.backups), "game_running": game_running()}
    if not out["divine_found"]:
        out["hint"] = "Divine.exe (LSLib) is missing: call dos2_fetch_lslib once, or set DOS2GM_DIVINE."
    try:
        out["campaigns"] = g.campaigns()
    except GMError as e:
        out["error"] = str(e)
    return out


@tool("Download LSLib (one-time setup)", idempotent=True, open_world=True,
      force="Download and unpack again even when Divine.exe is already present. Default false.")
def dos2_fetch_lslib(force: bool = False) -> dict:
    """One-time setup: downloads the official LSLib release by Norbyte (Divine.exe, MIT license) from GitHub, verifies
    its pinned SHA-256 and unpacks it into the server's data folder. Every tool that reads or writes campaign files
    needs Divine.exe, so run this when dos2_status reports divine_found=false.

    Needs internet access. Writes only into the data folder; the game and the campaigns are not touched. When
    Divine.exe is already there it returns at once without downloading. Returns {result, source}, or {error} on a
    network failure or checksum mismatch (then nothing is unpacked).
    """
    from . import fetch_lslib
    try:
        return {"result": fetch_lslib.fetch(force), "source": fetch_lslib.URL}
    except (fetch_lslib.FetchError, OSError) as e:
        return {"error": str(e)}


@tool("Campaign overview", read_only=True,
      campaign=CAMPAIGN)
def dos2_campaign(campaign: str) -> dict:
    """Reads one campaign and returns a compact overview: metadata and attached add-on mods, scenes, Overview maps with
    their pins, vignettes (uuid, title, answers), NPC and item templates (kind, name, level), sticky notes (title,
    kind, folder) and the whole journal.

    Read-only. Use it to orient yourself before reading details or editing. For full content follow up with
    dos2_scene (who stands in a scene), dos2_template (one template), dos2_vignettes or dos2_notes. To pick a
    campaign in the first place use dos2_status.
    """
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


@tool("Edit campaign name, description, author", destructive=True, idempotent=True,
      campaign=CAMPAIGN,
      name="New campaign name as shown in the game's campaign list. Omit to keep the current one.",
      description="New campaign description. Omit to keep the current one.",
      author="New author name. Omit to keep the current one.",
      allow_running=ALLOW_RUNNING)
def dos2_campaign_meta(campaign: str, name: str | None = None, description: str | None = None,
                       author: str | None = None, allow_running: bool = False) -> dict:
    """Changes the campaign's name, description and author in meta.lsf, the same fields the game edits in its campaign
    settings. Only the fields you pass are changed. With none of them given nothing is written and the current
    metadata is returned.

    The campaign folder keeps its name, because the game identifies the campaign by it. The previous file is backed up first; undo with dos2_restore.
    Fails with an error while the game process is running unless allow_running=true. Returns the backup id and the resulting metadata.
    """
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


@tool("Read a scene's contents", read_only=True,
      campaign=CAMPAIGN,
      scene="Scene name or scene ID, as listed by dos2_campaign.",
      limit="Maximum number of characters to list (default 200).")
def dos2_scene(campaign: str, scene: str, limit: int = 200) -> dict:
    """Reads what the Game Master has placed in one scene, from the campaign files (LevelCache): characters with name,
    root template, position, vitality, experience, alignment and encounter; encounters with their members; the item
    count; the scene's sticky notes.

    Read-only, and it shows the saved state, not a running game: for the live level use dos2_live_characters. Placed
    characters cannot be edited through files; change them in the game or with the dos2_live_* tools. Fails with an
    error if the scene has never been loaded in the game (it has no LevelCache file yet).
    """
    g = gm()
    d = g.resolve(campaign)
    return g.scene_detail(d, g.resolve_scene(d, scene), limit)


@tool("List saved templates", read_only=True,
      campaign=CAMPAIGN,
      kind="Filter by kind: \"npc\" or \"item\". Omit to list both.")
def dos2_templates(campaign: str, kind: str | None = None) -> list[dict]:
    """Lists the campaign's saved Game Master templates (the "Creatures and Items" panel, stored in Eggs.lsf) as short
    summaries. NPC entries have name, stat_name, template_id, level, alignment, skills and item stats ids; item
    entries have name, stats_id, template_id and level.

    Read-only. Use dos2_template to get one template in full, and dos2_find_templates to search by substring across
    all campaigns, including Larian's sample ones.
    """
    g = gm()
    _, top = g.eggs(g.resolve(campaign))
    out = [g.template_summary(t) for t in lsx.kids(top)]
    return [s for s in out if not kind or s["kind"] == kind]


@tool("Get one template in full", read_only=True,
      campaign=CAMPAIGN,
      name="Exact template name (case-insensitive), as listed by dos2_templates.")
def dos2_template(campaign: str, name: str) -> dict:
    """Returns one saved Game Master template of the campaign in full: for an NPC its base stats, the GM's stat edits,
    abilities, talents, skills, skill bar and items with slots; for an item all of its attributes.

    Read-only. Fails with an error when the campaign has no template with that name. Use it to inspect a template
    before changing it with dos2_put_npc_template or dos2_put_item_template. To list the available names use
    dos2_templates; to search across campaigns use dos2_find_templates.
    """
    g = gm()
    t = g.find_template(g.resolve(campaign), name)
    if t is None:
        raise GMError(f"No template '{name}'. List them with dos2_templates.")
    return g.template_detail(t)


@tool("Search templates across campaigns", read_only=True,
      query="Case-insensitive substring matched against the template name, StatName, root template, skills and "
            "items. Empty matches everything.",
      campaign="Search only this campaign (name, folder, UUID or a part). Omit to search all campaigns.",
      kind="Filter by kind: \"npc\" or \"item\". Omit for both.",
      limit="Maximum number of hits to return (default 30).")
def dos2_find_templates(query: str = "", campaign: str | None = None, kind: str | None = None,
                        limit: int = 30) -> list[dict]:
    """Searches saved Game Master templates in every campaign on disk (or in one) and returns matching summaries with
    the campaign each one lives in. Larian's sample campaigns are included, so this works as a library of ready
    NPCs.

    Read-only. Use it to find a template to copy: pass a hit to dos2_put_npc_template as base="Campaign:Name". To
    list one campaign's templates without searching use dos2_templates.
    """
    return gm().search_templates(query, campaign, kind, limit)


@tool("List vignettes in full", read_only=True,
      campaign=CAMPAIGN)
def dos2_vignettes(campaign: str) -> list[dict]:
    """Returns every vignette of the campaign with its full content: uuid, title, text, question, answer options, image
    and folder. Vignettes are the illustrated story cards the Game Master shows to players, optionally with a vote.

    Read-only. When titles are enough, dos2_campaign is cheaper. Create or change a vignette with dos2_put_vignette
    and remove one with dos2_delete_vignette.
    """
    g = gm()
    return g.vignettes(g.resolve(campaign))


@tool("List GM sticky notes", read_only=True,
      campaign=CAMPAIGN)
def dos2_notes(campaign: str) -> list[dict]:
    """Returns all Game Master sticky notes of the campaign with their full text. Each note has uuid, title, text,
    folder and kind: a "map" note sits on an Overview map (map UUID, x and y in map pixels), a "scene" note is bound
    to an object inside a scene (scene ID and the bound object). Players never see these notes.

    Read-only. When titles are enough, dos2_campaign is cheaper. Create or change a note with dos2_put_note and
    remove one with dos2_delete_note.
    """
    g = gm()
    return g.notes(g.resolve(campaign))


@tool("Read a campaign file as LSX", read_only=True,
      campaign=CAMPAIGN,
      file="Path of the file inside the campaign folder, e.g. \"Eggs.lsf\", \"meta.lsf\" or \"LevelCache/<scene "
           "ID>.lsf\".",
      xpath="ElementTree XPath to return only matching nodes, e.g. \".//node[@id='Note']\". Omit to get the whole "
            "file.",
      max_chars="Cut the output to this many characters (default 20000).")
def dos2_raw(campaign: str, file: str, xpath: str | None = None, max_chars: int = 20000) -> str:
    """Converts any binary campaign file (.lsf) to LSX text and returns it, whole or filtered by XPath. This is the
    escape hatch for exploring data the other tools do not expose.

    Read-only. Prefer the structured tools (dos2_campaign, dos2_scene, dos2_template, dos2_vignettes, dos2_notes)
    when they cover the need: raw files are large. With xpath the output starts with a match count; output longer
    than max_chars is truncated with a note. Fails with an error if the file does not exist in the campaign.
    """
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

@tool("Create or update an NPC template", destructive=True, idempotent=True,
      campaign=CAMPAIGN,
      name="Template name. An existing template with this name in the campaign is updated or replaced.",
      base="Template to copy from: \"Campaign:Name\", or just a name that is unique across all campaigns (find one "
           "with dos2_find_templates). Omit to change the existing template called name in place.",
      template_id="Root template GUID: the model, race and gender. Omit to keep the base's.",
      stat_name="Stats entry that gives the base stats, e.g. GEN_HumanCitizen. Omit to keep the base's.",
      level="NPC level. Omit to keep the base's.",
      alignment="Attitude towards the party: \"Neutral NPC\", \"Evil NPC\" (hostile) or \"Good NPC\" (ally).",
      archetype="AI archetype name that drives combat behaviour, copied verbatim into the template. Omit to keep.",
      trader="Whether the NPC offers trade. Omit to keep.",
      display_name="Name shown above the character's head, when it should differ from the template name.",
      bonus="GM additions to the base stats, e.g. {\"Constitution\": 4, \"Strength\": 2}. A value of 0 removes that "
            "edit.",
      ability_bonus="GM additions to abilities, e.g. {\"TwoHanded\": 3, \"WarriorLore\": 2}. 0 removes the edit.",
      skills="The complete skill list, e.g. [\"Target_SingleHandedAttack\", \"Shout_EnemyTaunt\"]. Replaces the skills "
             "and fills the skill bar in this order.",
      items="The complete item list; replaces the items. Each entry: {\"stats_id\", \"template_id\", \"amount\", \"slot\", "
            "\"level\", \"name\"}. slot is a number or one of weapon, shield, helmet, breast, leggings, boots, gloves, "
            "belt, amulet, ring, ring2; backpack when omitted.",
      items_from="\"Campaign:Name\" of a template whose items are copied as they are, including generated stats.",
      reset_bonus="First drop every GM edit inherited from the base (stats, abilities, talents), then apply yours. "
                  "Default false.",
      talents="Talents to add, by their Script Extender names, e.g. [\"Executioner\", \"Leech\"].",
      remove_talents="Talents to remove, by the same names.",
      allow_running=ALLOW_RUNNING)
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
    """Creates a saved NPC template in the campaign (the Game Master's "Creatures and Items" panel), or changes the one
    that already has this name. The usual way is to copy a ready NPC with base and adjust it: name, level,
    alignment, GM stat edits, skills, items. Parameters you omit keep the base's values.

    Writes Eggs.lsf. The previous file is backed up first; undo with dos2_restore. Fails with an error while the game process is running unless allow_running=true.
    The template only defines what the GM can place: it does not put a character into a scene (place it in the game,
    or use dos2_live_spawn). Returns created (true for a new template), the resulting template in full and the
    backup id. For item templates use dos2_put_item_template.
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


@tool("Create or replace an item template", destructive=True, idempotent=True,
      campaign=CAMPAIGN,
      name="Item template name. An existing item template with this name is replaced.",
      base="What to start from. A built-in sample: \"quest\" (plain item, default), \"weapon\", \"weapon_generated\" "
           "(has generated properties, required for boosts) or \"armor\"; or \"Campaign:Name\" of an existing item "
           "template.",
      template_id="Root template GUID of the item (its model). Omit to keep the base's.",
      stats_id="Stats entry, e.g. WPN_Axe_2H or LOOT_Ore_A_BlackRock_A. Omit to keep the base's.",
      level="Item level. Omit to keep the base's.",
      rarity="One of Common, Uncommon, Rare, Epic, Legendary, Divine, Unique.",
      description="Custom description shown in the item tooltip.",
      book="Text shown when the item is read as a book or note.",
      amount="Stack size.",
      boosts="Generated-property boosts, e.g. [\"Boost_Weapon_Damage_Bonus_Large\"]. Only valid with a base that has "
             "item generation, such as weapon_generated; otherwise the call fails.",
      allow_running=ALLOW_RUNNING)
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
    """Creates a saved item template in the campaign (the Game Master's "Creatures and Items" panel), or replaces the
    one with the same name. Use it for quest items, readable notes and books, and custom weapons or armor the GM
    hands out.

    Writes Eggs.lsf. The previous file is backed up first; undo with dos2_restore. Fails with an error while the game process is running unless allow_running=true.
    Returns created (true for a new template), a summary of the template and the backup id. For NPC templates use
    dos2_put_npc_template; to give an NPC template items, pass them in its items parameter instead.
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


@tool("Replace the GM journal", destructive=True, idempotent=True,
      campaign=CAMPAIGN,
      categories="The whole journal, in display order: [{\"title\": \"...\", \"shared\": false, \"chapters\": [{\"title\": "
                 "\"...\", \"shared\": false, \"paragraphs\": [{\"text\": \"...\", \"shared\": false}]}]}]. shared=true is "
                 "meant to show the entry to players.",
      allow_running=ALLOW_RUNNING)
def dos2_put_journal(campaign: str, categories: list[dict], allow_running: bool = False) -> dict:
    """Replaces the campaign's whole Game Master journal (categories, chapters, paragraphs) with the structure you
    pass. Entries that are not in categories are removed, so read the current journal with dos2_campaign first and
    send it back with your changes.

    Writes Journal.lsf. The previous file is backed up first; undo with dos2_restore. Fails with an error while the game process is running unless allow_running=true.
    The shared flag was not verified against Larian's samples; the GM can also share entries in the game. Returns
    the backup id and the journal as it now reads.
    """
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "journal", allow_running)
    w.stage("Journal.lsf", g.journal_root(d, categories))
    out = w.commit()
    return {**out, "journal": g.journal(d)}


@tool("Edit scene name, atmosphere, music", destructive=True, idempotent=True,
      campaign=CAMPAIGN,
      scene="Scene name or scene ID, as listed by dos2_campaign.",
      name="New scene name. Omit to keep.",
      folder="Folder the scene is filed under in the GM's scene list. Omit to keep.",
      atmosphere="Atmosphere GUID (one of the game's ATM_* atmospheres). Empty string restores the level default.",
      ambient="Ambient sound name (Soundvol_*). Empty string restores the level default.",
      music="Music track name (GM_*). Empty string restores the level default.",
      allow_running=ALLOW_RUNNING)
def dos2_scene_settings(campaign: str, scene: str, name: str | None = None, folder: str | None = None,
                        atmosphere: str | None = None, ambient: str | None = None, music: str | None = None,
                        allow_running: bool = False) -> dict:
    """Changes the settings of one scene in GMScenes.lsf: its name, folder, atmosphere (lighting and weather), ambient
    sound and music. Only the fields you pass change; an empty string resets that field to the level's default.

    It does not change what is placed in the scene: characters and items are placed in the game or with the
    dos2_live_* tools. The previous file is backed up first; undo with dos2_restore. Fails with an error while the game process is running unless allow_running=true.
    Returns the backup id and the scene entry as it now reads.
    """
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


@tool("Delete a saved template", destructive=True,
      campaign=CAMPAIGN,
      name="Exact template name (case-insensitive), as listed by dos2_templates.",
      allow_running=ALLOW_RUNNING)
def dos2_delete_template(campaign: str, name: str, allow_running: bool = False) -> dict:
    """Deletes a saved NPC or item template from the campaign by name. If an NPC template and an item template share
    the name, both are removed. Characters already placed in scenes from that template stay as they are.

    Writes Eggs.lsf. The previous file is backed up first; undo with dos2_restore. Fails with an error while the game process is running unless allow_running=true.
    Fails with an error when no template has that name. Returns the number of templates removed and the backup id.
    """
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

@tool("Create or replace a vignette", destructive=True, idempotent=True,
      campaign=CAMPAIGN,
      title="Vignette title. Without key, an existing vignette with the same title is replaced.",
      text="The story text shown on the card.",
      answers="Answer options offered to the players for a vote. Omit for a vignette without a vote.",
      question="The question shown above the answers. Default empty.",
      image="Absolute path to your own PNG (square, transparent background works best). It is copied into the "
            "campaign. Omit to use stock_image.",
      stock_image="File name of one of the game's stock vignette pictures, used when image is omitted.",
      folder="Folder the vignette is filed under in the GM's resources list. Default none.",
      key="Stable identifier of your choice. The vignette's UUID is derived from it, so the same key always "
          "addresses the same vignette even after its title changes.",
      canvas="File name of the paper background. Keep the default unless you know another stock canvas.",
      frame="File name of the border decoration. Keep the default unless you know another stock frame.",
      allow_running=ALLOW_RUNNING)
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
    """Creates a vignette (an illustrated story card the Game Master shows to players, optionally with a vote), or
    replaces an existing one. The vignette to replace is found by key if given, otherwise by an equal title;
    otherwise a new one is created.

    Writes Vignettes.lsf and, with image, copies the picture into the campaign's Vignettes/Illustrations. The previous file is backed up first; undo with dos2_restore.
    Fails with an error while the game process is running unless allow_running=true. Fails with an error if the image file does not exist. Returns created, the vignette's uuid, the image
    name and size, and the backup id. Read existing vignettes with dos2_vignettes.
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


@tool("Delete a vignette", destructive=True,
      campaign=CAMPAIGN,
      vignette="The vignette's UUID or its exact title (case-insensitive), as listed by dos2_vignettes.",
      allow_running=ALLOW_RUNNING)
def dos2_delete_vignette(campaign: str, vignette: str, allow_running: bool = False) -> dict:
    """Deletes one vignette from the campaign, found by UUID or exact title. Its picture files stay in the campaign's
    Vignettes/Illustrations folder, so other vignettes that use the same image keep working.

    Writes Vignettes.lsf. The previous file is backed up first; undo with dos2_restore. Fails with an error while the game process is running unless allow_running=true.
    Fails with an error when nothing matches. Returns the UUIDs removed and the backup id.
    """
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

@tool("Create or update a GM sticky note", destructive=True, idempotent=True,
      campaign=CAMPAIGN,
      title="Note title. Without key, an existing note with the same title is updated.",
      text="Note text. Line breaks are kept.",
      folder="Folder the note is filed under in the notes overview. Omit to keep the current one.",
      map="Overview map to put the note on: the map's title or UUID from dos2_campaign. May be omitted when the "
          "campaign has exactly one map or the note already exists.",
      x="Horizontal position on the map in map pixels. Default 600 for a new note.",
      y="Vertical position on the map in map pixels. Default 400 for a new note.",
      key="Stable identifier of your choice. The note's UUID is derived from it, so the same key always addresses "
          "the same note even after its title changes.",
      allow_running=ALLOW_RUNNING)
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
    """Creates a Game Master sticky note on an Overview map, or changes an existing note. Players never see these
    notes. An existing note is found by key if given, otherwise by an equal title; if none matches, a new one is
    created on the map.

    A note inside a scene hangs on an invisible marker item of the level, so new scene notes have to be placed in
    the game; for one that already exists this tool changes title, text and folder and keeps its binding.

    Writes Notes.lsf. The previous file is backed up first; undo with dos2_restore. Fails with an error while the game process is running unless allow_running=true.
    Fails with an error if the map is not found or has to be specified. Returns created, the note's uuid, where it
    sits and the backup id. Read notes with dos2_notes; remove one with dos2_delete_note.
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


@tool("Delete a GM sticky note", destructive=True,
      campaign=CAMPAIGN,
      note="The note's UUID or its exact title (case-insensitive), as listed by dos2_notes.",
      allow_running=ALLOW_RUNNING)
def dos2_delete_note(campaign: str, note: str, allow_running: bool = False) -> dict:
    """Deletes one Game Master sticky note from the campaign, found by UUID or exact title. Works for notes on an
    Overview map and for notes bound to an object in a scene.

    Writes Notes.lsf. The previous file is backed up first; undo with dos2_restore. Fails with an error while the game process is running unless allow_running=true.
    Fails with an error when nothing matches. Returns the UUIDs removed and the backup id. To change a note's text
    instead, use dos2_put_note.
    """
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

@tool("Back up the whole campaign",
      campaign=CAMPAIGN)
def dos2_backup(campaign: str) -> dict:
    """Copies the entire campaign folder into the backups folder as a new backup labelled "full". Nothing in the
    campaign changes, and it is safe while the game is running.

    Every write tool already backs up the files it touches, so use this for a complete snapshot before a risky
    series of edits or before a play session. Returns the backup id and its path. List backups with dos2_backups
    and bring one back with dos2_restore.
    """
    g = gm()
    d = g.resolve(campaign)
    bid = g.full_backup(d)
    return {"backup": bid, "path": str(g.backup_root(d) / bid)}


@tool("List campaign backups", read_only=True,
      campaign=CAMPAIGN)
def dos2_backups(campaign: str) -> list[dict]:
    """Lists the campaign's backups, newest first. Each entry has the id (timestamp plus operation label, e.g.
    "...-npc-template" or "...-full"), the label, the files that were saved, and the files that the operation
    created.

    Read-only. Use it to find the id to pass to dos2_restore when an edit has to be undone. Returns an empty list
    when the campaign has no backups yet.
    """
    g = gm()
    return g.backups(g.resolve(campaign))


@tool("Restore a backup", destructive=True,
      campaign=CAMPAIGN,
      backup="Backup id exactly as listed by dos2_backups.",
      allow_running=ALLOW_RUNNING)
def dos2_restore(campaign: str, backup: str, allow_running: bool = False) -> dict:
    """Undoes an earlier write: puts back the files saved in the given backup and deletes the files that the backed-up
    operation had created. Restoring a "full" backup brings back every file of the campaign.

    The current state of the affected files is itself backed up first under the label "before-restore", so a
    restore can be undone too. Fails with an error while the game process is running unless allow_running=true. Fails with an error when the backup id does not exist. Returns the id of
    the before-restore backup and restored_from.
    """
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


@tool("Live layer readiness", read_only=True)
def dos2_live_status() -> dict:
    """Checks everything the live tools (dos2_live_*) need and reports what is missing: whether the game is running,
    whether Script Extender is installed next to the game and its settings, whether the bridge mod .pak is built,
    which campaigns have it attached, and whether the bridge's heartbeat is coming in (it writes one about every
    2 seconds while a campaign is loaded in Game Master mode).

    Read-only, no parameters. Call it before the first live tool of a session and whenever a live tool times out.
    If the bridge is not built or attached, run dos2_bridge_install with the game closed. For file-level setup
    (campaign list, Divine.exe) use dos2_status.
    """
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


@tool("Build and attach the bridge mod", destructive=True, idempotent=True,
      campaign="Campaign to attach the bridge to as an add-on (name, folder, UUID or a part). Omit to only build "
               "the .pak.",
      allow_running=ALLOW_RUNNING)
def dos2_bridge_install(campaign: str | None = None, allow_running: bool = False) -> dict:
    """Builds this server's bridge mod into the game's Mods folder (DOS2GMBridge_....pak, overwriting the previous
    build) and, when campaign is given, adds it to that campaign's add-ons in meta.lsf. The bridge is what the
    dos2_live_* tools talk to; it needs Script Extender in the game.

    The previous file is backed up first; undo with dos2_restore. Attaching: Fails with an error while the game process is running unless allow_running=true.
    In multiplayer every player needs the same .pak and Script Extender, as with any add-on; after a rebuild, send
    them the new file. Returns the .pak path, the packed files and the campaign's add-on list. Check the result
    with dos2_live_status; remove with dos2_bridge_detach.
    """
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


@tool("Build a mod folder and attach it", destructive=True, idempotent=True,
      source="Absolute path to the mod's source folder; it must contain exactly one Mods/<mod folder>/meta.lsx.",
      campaign="Campaign to attach the mod to as an add-on (name, folder, UUID or a part). Omit to only build the "
               ".pak.",
      allow_running=ALLOW_RUNNING)
def dos2_mod_install(source: str, campaign: str | None = None, allow_running: bool = False) -> dict:
    """Packs your own mod from a source folder into the game's Mods folder as <mod folder>.pak (overwriting a previous
    build) and, when campaign is given, adds it to that campaign's add-ons. UUID, name, version and folder come
    from the mod's meta.lsx.

    Use it for mods you develop yourself; for a ready third-party .pak use dos2_addon, and for this server's bridge
    use dos2_bridge_install. Fails if PublishVersion in meta.lsx is below a Definitive Edition editor version (use
    909321303): the game would mark the mod as Classic and refuse it. The previous file is backed up first; undo with dos2_restore. Attaching: Fails with an error while the game process is running unless allow_running=true.
    Returns the .pak path, packed files, module name and UUID, and the add-on list.
    """
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


@tool("Attach or detach a .pak add-on", destructive=True, idempotent=True,
      campaign=CAMPAIGN,
      pak="Absolute path to the mod's .pak file.",
      attach="true (default) attaches the mod; false removes it from the campaign's add-ons.",
      allow_running=ALLOW_RUNNING)
def dos2_addon(campaign: str, pak: str, attach: bool = True, allow_running: bool = False) -> dict:
    """Attaches a ready-made mod from a .pak file to the campaign as an add-on, or detaches it with attach=false. On
    attach the file is copied into the game's Mods folder; UUID, name, version and folder are read from the
    meta.lsx inside the pak. Detaching leaves the .pak in Mods.

    Writes meta.lsf. The previous file is backed up first; undo with dos2_restore. Fails with an error while the game process is running unless allow_running=true.
    Players need the same .pak, as with any add-on. Fails if the file is missing or holds no single meta.lsx.
    Returns the module name, author, UUID, the .pak path and the campaign's add-on list. To build a mod from a
    source folder use dos2_mod_install.
    """
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


@tool("Detach the bridge mod", destructive=True, idempotent=True,
      campaign=CAMPAIGN,
      allow_running=ALLOW_RUNNING)
def dos2_bridge_detach(campaign: str, allow_running: bool = False) -> dict:
    """Removes this server's bridge mod from the campaign's add-ons in meta.lsf. After that the dos2_live_* tools no
    longer work for this campaign, and players no longer need the bridge .pak. The .pak itself stays in the game's
    Mods folder, and nothing happens if the bridge was not attached.

    The previous file is backed up first; undo with dos2_restore. Fails with an error while the game process is running unless allow_running=true.
    Returns the backup id and the campaign's add-on list. Attach again with dos2_bridge_install.
    """
    g = gm()
    d = g.resolve(campaign)
    w = g.writer(d, "bridge-detach", allow_running)
    meta = g.read(d, "meta.lsf")
    if g.set_addon(meta, BRIDGE_UUID, BRIDGE_NAME, BRIDGE_VERSION, BRIDGE_FOLDER, False):
        w.stage("meta.lsf", meta)
    out = w.commit()
    return {**out, "addons": g.meta(d)["addons"]}


@tool("List characters on the live level", read_only=True,
      near="Only list characters around this one (GUID or name). Omit to list the whole level.",
      radius="Radius in metres around near (default 8). Ignored without near.",
      limit="Maximum number of characters to return (default 200).")
def dos2_live_characters(near: str | None = None, radius: float = 8.0, limit: int = 200) -> list[dict]:
    """Lists the characters on the level currently loaded in the running game, player characters included: GUID, name,
    root template, stats entry, level, vitality, alignment and position. With near it returns only those within
    radius of that character.

    Read-only; needs the game running with the bridge mod (see dos2_live_status), otherwise it fails with a timeout
    error. Use it to get GUIDs and positions for dos2_live_move, dos2_live_set, dos2_live_stage and
    dos2_live_spawn. For what is saved in the campaign files rather than the running game, use dos2_scene.
    """
    if near:
        x, y, z = _who(near)["pos"]
        return bridge().call("characters", {"x": x, "y": y, "z": z, "radius": radius, "limit": limit})
    return bridge().call("characters", {"limit": limit})


@tool("Spawn a character in the live game",
      template="Root template GUID, or the name of a saved GM NPC template (then campaign is required).",
      campaign="Campaign that holds the GM template named in template. Not needed for a root template GUID.",
      x="World X coordinate in metres. Give all of x, y, z, or use near instead.",
      y="World Y (height) coordinate in metres. Give all of x, y, z, or use near instead.",
      z="World Z coordinate in metres. Give all of x, y, z, or use near instead.",
      near="Place relative to this character (GUID or name) instead of giving x, y, z.",
      dx="Offset in metres along X from the near character (default 1.5). Ignored without near.",
      dz="Offset in metres along Z from the near character (default 0). Ignored without near.",
      name="Name shown above the head. Defaults to the GM template's name.",
      level="Character level. Defaults to the GM template's level.",
      alignment="\"Neutral NPC\", \"Evil NPC\" (hostile) or \"Good NPC\" (ally). Defaults to the GM template's.")
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
    """Creates a new character on the level loaded in the running game, at a point or next to another character. Each
    call adds one more character, so repeating it makes duplicates; take one away with dos2_live_stage.

    From a GM template only the model, level, alignment and name are used: its stat edits, skills and items are NOT
    applied this way (placing it from the game's "Creatures and Items" panel applies them). During the Game Master
    pause the character is created at once but moves into place only after unpausing: the result then has
    applies_on_unpause=true and its position still reads 0, 0, 0. Needs the bridge (see dos2_live_status). Returns
    the new character's GUID and state.
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


@tool("Teleport a live character", idempotent=True,
      who="The character to act on: its GUID, its exact name, or a unique part of its name (see "
          "dos2_live_characters).",
      x="World X coordinate in metres. Give all of x, y, z, or use near instead.",
      y="World Y (height) coordinate in metres. Give all of x, y, z, or use near instead.",
      z="World Z coordinate in metres. Give all of x, y, z, or use near instead.",
      near="Place relative to this character (GUID or name) instead of giving x, y, z.",
      dx="Offset in metres along X from the near character (default 1.5). Ignored without near.",
      dz="Offset in metres along Z from the near character (default 0). Ignored without near.")
def dos2_live_move(who: str, x: float | None = None, y: float | None = None, z: float | None = None,
                   near: str | None = None, dx: float = 1.5, dz: float = 0.0) -> dict:
    """Teleports one character on the live level to the point x, y, z, or next to another character with the offset
    dx, dz. Works on NPCs and on player characters.

    Changes only the running game, not the campaign files. During the Game Master pause the move is applied after
    unpausing (the result has applies_on_unpause=true). Fails with an error when who matches no character or more
    than one (then pass a GUID), or when neither a full x, y, z nor near is given. Needs the bridge (see
    dos2_live_status). Get positions from dos2_live_characters.
    """
    guid = _who(who)["guid"]
    px, py, pz = _where(x, y, z, near, dx, dz)
    return bridge().call("move", {"guid": guid, "x": px, "y": py, "z": pz})


@tool("Rename, level up or re-align a live character", idempotent=True,
      who="The character to act on: its GUID, its exact name, or a unique part of its name (see "
          "dos2_live_characters).",
      name="New name shown above the character's head. Omit to keep.",
      level="New level. Levels can only go up; a lower value is ignored by the game. Omit to keep.",
      alignment="\"Neutral NPC\", \"Evil NPC\" (hostile to the party) or \"Good NPC\" (ally). Omit to keep.")
def dos2_live_set(who: str, name: str | None = None, level: int | None = None,
                  alignment: str | None = None) -> dict:
    """Changes a character that already stands on the live level: its displayed name, its level and its alignment
    towards the party. Only the fields you pass change.

    Changes only the running game, not the campaign files or the template the character came from. Setting
    alignment to "Evil NPC" makes the character hostile, which can start combat. Fails with an error when who
    matches no character or more than one (then pass a GUID). Needs the bridge (see dos2_live_status). Returns the
    character's new state. To move a character use dos2_live_move; to hide one use dos2_live_stage.
    """
    guid = _who(who)["guid"]
    return bridge().call("set", {"guid": guid, "name": name, "level": level, "alignment": alignment})


@tool("Hide or show a live character", idempotent=True,
      who="The character to act on: its GUID, its exact name, or a unique part of its name (see "
          "dos2_live_characters).",
      on="false takes the character off stage (hidden and inactive); true brings it back.")
def dos2_live_stage(who: str, on: bool) -> dict:
    """Takes a character on the live level off stage or brings it back. An off-stage character is invisible and takes
    no part in the scene but stays in the level with its state, so this is the reversible way to remove an NPC or
    to prepare one for a later entrance; nothing is deleted.

    Changes only the running game. Fails with an error when who matches no character or more than one (then pass a
    GUID). Needs the bridge (see dos2_live_status). Returns the character's new state.
    """
    guid = _who(who)["guid"]
    return bridge().call("stage", {"guid": guid, "on": on})


@tool("Run Lua in the live game", destructive=True,
      code="Lua source to run. Use return to send a value back, e.g. return Osi.CharacterGetHostCharacter().",
      timeout="Seconds to wait for the game's answer (default 5).")
def dos2_live_lua(code: str, timeout: float = 5.0):
    """Runs arbitrary Lua on the game server through Script Extender, in the bridge mod's context, and returns the
    value of its return statement. The Ext and Osi APIs are available. This is the escape hatch for anything the
    other live tools do not cover: items, statuses, combat, level changes.

    It can change or break anything in the running session, so prefer the dedicated tools (dos2_live_characters,
    dos2_live_spawn, dos2_live_move, dos2_live_set, dos2_live_stage) when they fit. The returned value must be
    JSON-serializable. Lua errors come back as an error with the message; a call that outlives timeout fails. Needs
    the bridge (see dos2_live_status). Do not call Osi functions while a level is still loading.
    """
    return bridge().call("lua", {"code": code}, timeout=timeout)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
