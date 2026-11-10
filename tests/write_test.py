"""Write check on a copy of a campaign in a temporary folder. The real campaigns are only read.

uv run python tests/write_test.py <work folder> "My Campaign" [vignette image]

<work folder> is wiped and recreated: the copy of the campaign, its backups and a Mods folder go there.
The second argument is the name (or folder, UUID, or a part of one) of a campaign that exists on disk. It needs
at least one NPC template; the note and scene checks are skipped if it has no Overview map or no scene.

1. Rebuild without edits: LSF → LSX → LSF → LSX gives the same LSX (LSLib loses nothing).
2. An NPC template from a base, editing an existing one, a vignette, a sticky note, deletions, the bridge mod,
   an item template, the journal, scene settings.
3. Restoring from a backup brings back the original bytes.
"""
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
if len(sys.argv) < 3:
    sys.exit('usage: python tests/write_test.py <work folder> "<campaign>" [vignette image]\n'
             "No campaign given. Pass the name of a campaign that exists on disk (it is copied to the work folder,\n"
             "the original is only read); list them with:\n"
             '  python -c "from dos2gm import server as s; print(s.dos2_status())"')
work = Path(sys.argv[1])
image = sys.argv[3] if len(sys.argv) > 3 else None

from dos2gm import paths  # noqa: E402
from dos2gm.gm import GM  # noqa: E402

real = paths.load().campaigns
src = GM(paths.load()).resolve(sys.argv[2])
if work.exists():
    shutil.rmtree(work)
(work / "GMCampaigns").mkdir(parents=True)
shutil.copytree(src, work / "GMCampaigns" / src.name)
os.environ["DOS2GM_CAMPAIGNS"] = str(work / "GMCampaigns")
os.environ["DOS2GM_BACKUPS"] = str(work / "backups")
os.environ["DOS2GM_MODS"] = str(work / "Mods")

from dos2gm import lsx, server as s  # noqa: E402
from dos2gm.live import BRIDGE_NAME  # noqa: E402

g = s.gm()
camp = src.name
cdir = g.resolve(camp)
if work.resolve() not in cdir.resolve().parents:
    sys.exit(f"Refusing to write: {cdir} is outside the work folder {work}")
log = {}
fails = []


def check(name, cond, info=None):
    log[name] = "ok" if cond else f"FAIL {info}"
    if not cond:
        fails.append(name)


def skip(why, *names):
    for name in names:
        log[name] = f"skipped: {why}"


def sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


# 1. Rebuild without edits for the files of all real campaigns (read only; the rebuilt files go to the cache).
for d in real.iterdir():
    for rel in ("Eggs.lsf", "Vignettes.lsf", "Notes.lsf", "OverviewMaps.lsf", "Journal.lsf", "meta.lsf"):
        f = d / rel
        if not f.exists():
            continue
        a = g.divine.read(f)
        built = g.divine.build(a)
        b = g.divine.read(built)
        built.unlink()
        same = lsx.to_text(a) == lsx.to_text(b)
        if not same:
            check(f"roundtrip {d.name[:24]}/{rel}", False, "LSX differs")
log["roundtrip"] = "done"

orig_eggs = sha(cdir / "Eggs.lsf")
orig_vign = sha(cdir / "Vignettes.lsf")

# The base for the new templates: the first NPC template of the campaign that has a GM edits block.
base = None
for n in s.dos2_templates(camp, kind="npc"):
    t = s.dos2_template(camp, n["name"])
    if t["bonus"]:
        base = t
        break
if base is None:
    sys.exit(f"Campaign {src.name} has no NPC template to use as a base: save one in the game "
             "or pass another campaign.")
base_ref = f"{camp}:{base['name']}"
SKILLS = ["Target_SingleHandedAttack", "Shout_EnemyTaunt", "Target_EnemyDeathWish"]

# 2a. A new NPC based on an existing template.
r = s.dos2_put_npc_template(
    camp, "Test: demon", base=base_ref, level=10, alignment="Evil NPC",
    display_name="Test: demon", bonus={"Constitution": 4, "Strength": 3},
    ability_bonus={"TwoHanded": 2}, skills=SKILLS,
)
t = s.dos2_template(camp, "Test: demon")
check("npc created", r["created"] and t["level"] == 10 and t["alignment"] == "Evil NPC", r)
check("npc bonus", t["bonus"]["stats"].get("Constitution") == 4 and t["bonus"]["abilities"].get("TwoHanded") == 2, t["bonus"])
check("npc skills", t["skills"] == SKILLS, t["skills"])
check("npc skillbar", [x[1] for x in t["skillbar"]] == t["skills"] and t["skillbar_slots"] == 145, t["skillbar"])
check("npc items kept from base", t["items"] == base["items"], t["items"])
check("npc base stats kept from base", t["base"] == base["base"], t["base"])
check("npc base talents decoded", not any(x.startswith("#") for x in t["base"]["talents"]), t["base"]["talents"])

# 2a'. A base that carries GM edits: reset_bonus drops them, talents are set by name.
r = s.dos2_put_npc_template(camp, "Test: troll", base=f"{camp}:Test: demon", reset_bonus=True,
                            bonus={"Armor": 160}, talents=["Executioner", "Leech"])
t = s.dos2_template(camp, "Test: troll")
check("reset bonus", t["bonus"]["stats"] == {"Armor": 160} and not t["bonus"]["abilities"], t["bonus"])
check("talents set", t["bonus"]["talents"] == ["Leech", "Executioner"], t["bonus"])
s.dos2_put_npc_template(camp, "Test: troll", remove_talents=["Leech"])
t = s.dos2_template(camp, "Test: troll")
check("talent removed", t["bonus"]["talents"] == ["Executioner"] and t["bonus"].get("removed_talents") == ["Leech"], t["bonus"])
s.dos2_delete_template(camp, "Test: troll")

# 2b. Editing an existing template without a base: stats and items.
s.dos2_put_npc_template(camp, "Test: skipper", base=base_ref)
r = s.dos2_put_npc_template(camp, "Test: skipper", bonus={"Strength": 2}, trader=True,
                            items=[{"stats_id": "CON_Food_Fish_A", "template_id": "5263f807-9993-4c1a-873d-f7d867291dba",
                                    "amount": 3}])
t = s.dos2_template(camp, "Test: skipper")
check("existing updated", not r["created"] and t["trader"] and t["bonus"]["stats"].get("Strength") == 2, t)
check("existing item", t["items"][0]["amount"] == 3 and t["items"][0]["slot"] == 15, t["items"])
check("existing stays one", [x["name"] for x in s.dos2_templates(camp)].count("Test: skipper") == 1)

# 2c. A vignette with the same key replaces the previous one; the UUID depends on the key alone.
before = s.dos2_vignettes(camp)
r1 = s.dos2_put_vignette(camp, "Test vignette", "Old text.", key="test-vignette")
r = s.dos2_put_vignette(camp, "Test vignette", "New text.", answers=["Step onto the pier", "Stay aboard"],
                        key="test-vignette")
after = s.dos2_vignettes(camp)
check("vignette uuid is stable for a key", r1["created"] and not r["created"]
      and r["uuid"] == r1["uuid"] == "a3036ed0-a46f-51c0-b909-434e1bbd7b1a", (r1, r))
check("vignette count", len(after) == len(before) + 1, (len(before), len(after)))
v = next(x for x in after if x["uuid"] == r["uuid"])
check("vignette text", v["text"] == "New text." and v["answers"] == ["Step onto the pier", "Stay aboard"], v)
if image:
    r = s.dos2_put_vignette(camp, "Pier", "The planks creak.", image=image, folder="Scene 0")
    ok = (cdir / "Vignettes" / "Illustrations" / Path(image).name).exists() and \
         (cdir / "Vignettes" / "Illustrations" / "Previews" / Path(image).name).exists()
    check("vignette image copied", r["created"] and ok and r["image_size"], r)

# 2d. A sticky note on a map; a repeat with the same title changes the text and keeps the place.
notes_before = s.dos2_notes(camp)
maps = g.maps(cdir)
if maps:
    r = s.dos2_put_note(camp, "Test: pier", "The skipper waits by the boat.", folder="Test", map=maps[0]["uuid"],
                        x=500, y=300)
    r2 = s.dos2_put_note(camp, "Test: pier", "The skipper waits by the boat and swears.")
    notes = s.dos2_notes(camp)
    check("note created", r["created"] and not r2["created"] and len(notes) == len(notes_before) + 1, (r, r2))
    mine = next(n for n in notes if n["title"] == "Test: pier")
    check("note kept place", mine["x"] == 500 and mine["folder"] == "Test" and "swears" in mine["text"], mine)
else:
    skip("the campaign has no Overview map", "note created", "note kept place", "restore notes")

# 2e. Deletions.
if maps:
    s.dos2_delete_note(camp, "Test: pier")
s.dos2_delete_template(camp, "Test: demon")
s.dos2_delete_template(camp, "Test: skipper")
left = [x["name"] for x in s.dos2_templates(camp)]
check("deleted", s.dos2_notes(camp) == notes_before and "Test: demon" not in left and "Test: skipper" not in left)

# 2f. The bridge mod: building the .pak into the temporary Mods, attaching it to the campaign and detaching.
addons = [a for a in g.meta(cdir)["addons"] if a != BRIDGE_NAME]
if BRIDGE_NAME in g.meta(cdir)["addons"]:
    s.dos2_bridge_detach(camp)
r = s.dos2_bridge_install(camp)
check("bridge pak built", Path(r["pak"]).exists() and any(f.endswith("Bridge.lua") for f in r["files"])
      and any(f.endswith("OsiToolsConfig.json") for f in r["files"]), r)
check("bridge attached", r["addons"] == addons + [BRIDGE_NAME], r)
r2 = s.dos2_bridge_install(camp)
check("bridge attach is idempotent", r2["addons"] == addons + [BRIDGE_NAME] and r2["changed"] == [], r2)
check("status sees attach", s.dos2_status() and cdir.name in s.dos2_live_status()["attached_to"])
r = s.dos2_bridge_detach(camp)
check("bridge detached", r["addons"] == addons, r)

# 2g. Items, the journal, scene settings.
r = s.dos2_put_item_template(camp, "Test: axe", base="weapon_generated", template_id="22a7e83e-5b7d-423d-9028-58f8af55603d",
                             stats_id="WPN_Axe_2H", level=11, rarity="Legendary", description="Smells of herring.",
                             boosts=["Boost_Weapon_Damage_Bonus_Large"])
items = [t for t in s.dos2_templates(camp, kind="item") if t["name"] == "Test: axe"]
check("item template", r["created"] and items and items[0]["level"] == 11, (r, items))
raw = s.dos2_raw(camp, "Eggs.lsf", ".//node[@id='GMExportItem']", max_chars=10_000_000)
check("item boosts", "Boost_Weapon_Damage_Bonus_Large" in raw and "Smells of herring." in raw)
s.dos2_delete_template(camp, "Test: axe")
j = s.dos2_put_journal(camp, [{"title": "Quests", "chapters": [{"title": "Chapter", "paragraphs": [{"text": "Paragraph 1"}, {"text": "Paragraph 2", "shared": True}]}]}])
check("journal", j["journal"]["entries"] == ["Quests", "Chapter", "Paragraph 1", "Paragraph 2"], j["journal"])
scenes = g.scenes(cdir)
if scenes:
    sc = s.dos2_scene_settings(camp, scenes[0]["id"], atmosphere="806f4ade-4857-4cf7-bc42-b02f0645fb10", music="68614067-8174-4f93-8ad9-0e1f80123e36")
    check("scene settings", sc["scene"]["Atmosphere"] == "806f4ade-4857-4cf7-bc42-b02f0645fb10" and sc["scene"]["Music"].startswith("68614067"), sc["scene"])
else:
    skip("the campaign has no scenes", "scene settings")

# 3. Backups and going back to the very first state.
bks = s.dos2_backups(camp)
first_eggs = [b for b in bks if b["label"] == "npc-template"][-1]
s.dos2_restore(camp, first_eggs["id"])
first_vign = [b for b in s.dos2_backups(camp) if b["label"] == "vignette"][-1]
s.dos2_restore(camp, first_vign["id"])
check("restore eggs bytes", sha(cdir / "Eggs.lsf") == orig_eggs)
check("restore vignettes bytes", sha(cdir / "Vignettes.lsf") == orig_vign)
if maps:
    first_note = [b for b in s.dos2_backups(camp) if b["label"] == "note"][-1]
    s.dos2_restore(camp, first_note["id"])
    check("restore notes", s.dos2_notes(camp) == notes_before)
full = s.dos2_backup(camp)
check("full backup", (Path(full["path"]) / "manifest.json").exists(), full)
check("game not running", not s.dos2_status()["game_running"])

print(json.dumps(log, ensure_ascii=False, indent=1))
print("FAILS:", fails or "none")
sys.exit(1 if fails else 0)
