"""A Game Master mode campaign: reading, editing, backups.

Campaign files (GMCampaigns/<folder>):
  meta.lsf          name, UUID, add-ons
  GMScenes.lsf      scenes: ID, base level, name
  OverviewMaps.lsf  Overview maps and their pins
  Vignettes.lsf     vignettes
  Notes.lsf         GM sticky notes
  Journal.lsf       GM journal
  Eggs.lsf          NPC templates (GMExportCharacter) and item templates (GMExportItem)
  LevelCache/<scene ID>.lsf  level snapshot: placed NPCs, items, encounters
"""
import json
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from . import lsx
from .lsx import T_BOOL, T_FIXED, T_FLOAT, T_INT, T_INT8, T_SCRATCH, T_UINT, T_USHORT, T_WSTRING
from .paths import Paths

GM_MODULE = "00550ab2-ac92-410c-8d94-742f7629de0e"
# Fixed namespace: the same key always yields the same UUID, here and in any external script
# that derives vignette UUIDs with uuid5 from this namespace.
UUID_NS = uuid.UUID("6a1f0c2e-7b0e-4f8e-9d55-3a6c1b2d9e10")
SKILLBAR_SLOTS = 145
BACKPACK_SLOT = 15
FRAME = 640
STOCK_SIZE = (1300, 1300)
EQUIP_SLOTS = {
    "helmet": 0, "breast": 1, "leggings": 2, "weapon": 3, "shield": 4, "ring": 5, "belt": 6,
    "boots": 7, "gloves": 8, "amulet": 9, "ring2": 10, "wings": 11, "horns": 12, "overhead": 13,
}
ALIGNMENTS = ("Neutral NPC", "Evil NPC", "Good NPC")
# DOS2 talents by bit number in the four 32-bit Talents masks (ositools, Enumerations.inl,
# stats::TalentType; TalentArray = BitArray<4>). Checked against Larian templates: monsters have bit 3 = Flanking.
TALENTS = (
    "None ItemMovement ItemCreation Flanking AttackOfOpportunity Backstab Trade Lockpick ChanceToHitRanged "
    "ChanceToHitMelee Damage ActionPoints ActionPoints2 Criticals IncreasedArmor Sight ResistFear ResistKnockdown "
    "ResistStun ResistPoison ResistSilence ResistDead Carry Throwing Repair ExpGain ExtraStatPoints ExtraSkillPoints "
    "Durability Awareness Vitality FireSpells WaterSpells AirSpells EarthSpells Charm Intimidate Reason Luck "
    "Initiative InventoryAccess AvoidDetection AnimalEmpathy Escapist StandYourGround SurpriseAttack LightStep "
    "ResurrectToFullHealth Scientist Raistlin MrKnowItAll WhatARush FaroutDude Leech ElementalAffinity "
    "FiveStarRestaurant Bully ElementalRanger LightningRod Politician WeatherProof LoneWolf Zombie Demon IceKing "
    "Courageous GoldenMage WalkItOff FolkDancer SpillNoBlood Stench Kickstarter WarriorLoreNaturalArmor "
    "WarriorLoreNaturalHealth WarriorLoreNaturalResistance RangerLoreArrowRecover RangerLoreEvasionBonus "
    "RangerLoreRangedAPBonus RogueLoreDaggerAPBonus RogueLoreDaggerBackStab RogueLoreMovementBonus "
    "RogueLoreHoldResistance NoAttackOfOpportunity WarriorLoreGrenadeRange RogueLoreGrenadePrecision WandCharge "
    "DualWieldingDodging Human_Inventive Human_Civil Elf_Lore Elf_CorpseEating Dwarf_Sturdy Dwarf_Sneaking "
    "Lizard_Resistance Lizard_Persuasion Perfectionist Executioner ViolentMagic QuickStep Quest_SpidersKiss_Str "
    "Quest_SpidersKiss_Int Quest_SpidersKiss_Per Quest_SpidersKiss_Null Memory Quest_TradeSecrets Quest_GhostTree "
    "BeastMaster LivingArmor Torturer Ambidextrous Unstable ResurrectExtraHealth NaturalConductor Quest_Rooted "
    "PainDrinker DeathfogResistant Sourcerer Rager Elementalist Sadist Haymaker Gladiator Indomitable WildMag "
    "Jitterbug Soulcatcher MasterThief GreedyVessel MagicCycles"
).split()
TALENT_INDEX = {name.lower(): i for i, name in enumerate(TALENTS)}


def talent_names(masks: list[int]) -> list[str]:
    out = []
    for word, m in enumerate(masks):
        for bit in range(32):
            if m >> bit & 1:
                i = word * 32 + bit
                out.append(TALENTS[i] if i < len(TALENTS) else f"#{i}")
    return out


def _set_talent_bits(block: ET.Element, nid: str, names: list[str], on: bool) -> None:
    words = lsx.kids(block, nid)
    ch = lsx.children(block, create=True)
    while len(words) < 4:
        w = lsx.node(nid, [("Object", T_UINT, 0)])
        anchor = words[-1] if words else None
        ch.insert(list(ch).index(anchor) + 1 if anchor is not None else len(ch), w)
        words.append(w)
    for name in names:
        i = TALENT_INDEX.get(name.strip().lower())
        if i is None or i == 0:
            raise GMError(f"No talent '{name}'. Use ositools names: Executioner, Leech, StandYourGround…")
        w = words[i // 32]
        val = _int(lsx.get(w, "Object"), 0)
        val = val | (1 << i % 32) if on else val & ~(1 << i % 32)
        lsx.set_attr(w, "Object", T_UINT, val)


class GMError(RuntimeError):
    pass


def stable_uuid(kind: str, key: str) -> str:
    return str(uuid.uuid5(UUID_NS, key if kind == "vignette" else f"{kind}:{key}"))


def game_running() -> bool:
    if sys.platform != "win32":
        return False
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq EoCApp.exe", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True, errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return "eocapp.exe" in r.stdout.lower()


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _vec(v: str | None) -> list[float] | None:
    if not v:
        return None
    try:
        return [round(float(x), 3) for x in v.split()]
    except ValueError:
        return None


def _match(query: str, *values) -> bool:
    q = query.strip().lower()
    return any(q in (v or "").lower() for v in values)


class GM:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.divine = lsx.Divine(paths.divine, paths.cache)

    # --- campaigns ------------------------------------------------------------

    def campaign_dirs(self) -> list[Path]:
        root = self.paths.campaigns
        if not root.exists():
            raise GMError(f"No campaigns folder {root}. The game has never been run, or the path is wrong (DOS2GM_CAMPAIGNS).")
        return sorted(d for d in root.iterdir() if d.is_dir() and (d / "meta.lsf").exists())

    def read(self, cdir: Path, rel: str) -> ET.Element | None:
        f = cdir / rel
        return self.divine.read(f) if f.exists() else None

    def meta(self, cdir: Path) -> dict:
        root = self.read(cdir, "meta.lsf")
        cfg = lsx.region_node(root, "Config")
        info = lsx.kid(cfg, "ModuleInfo")
        a = lsx.attrs(info)
        deps = lsx.kid(cfg, "Dependencies")
        addons = [lsx.get(m, "Name") for m in lsx.kids(deps, "ModuleShortDesc")] if deps is not None else []
        return {
            "name": a.get("Name"), "uuid": a.get("UUID"), "folder": cdir.name,
            "author": a.get("Author") or None, "description": a.get("Description") or None,
            "players": _int(a.get("NumPlayers")), "addons": addons,
        }

    def resolve(self, query: str) -> Path:
        dirs = self.campaign_dirs()
        q = query.strip().lower()
        hits = []
        for d in dirs:
            folder = d.name.lower()
            if q in (folder, folder.replace("_", " ")):
                return d
            try:
                m = self.meta(d)
            except Exception:
                m = {}
            if q in ((m.get("name") or "").lower(), (m.get("uuid") or "").lower()):
                return d
            if q in folder or q in folder.replace("_", " ") or q in (m.get("name") or "").lower():
                hits.append(d)
        if len(hits) == 1:
            return hits[0]
        names = ", ".join(d.name for d in (hits or dirs))
        if not hits:
            raise GMError(f"Campaign '{query}' not found. Available: {names}")
        raise GMError(f"'{query}' matches several campaigns: {names}")

    def campaigns(self) -> list[dict]:
        out = []
        for d in self.campaign_dirs():
            try:
                m = self.meta(d)
            except Exception as e:  # a broken meta must not break the list
                m = {"folder": d.name, "error": str(e)}
            m["scenes"] = len(list((d / "LevelCache").glob("*.lsf"))) - int((d / "LevelCache" / "Globals.lsf").exists()) \
                if (d / "LevelCache").exists() else 0
            m["modified"] = datetime.fromtimestamp(max(f.stat().st_mtime for f in d.rglob("*") if f.is_file())).isoformat(timespec="minutes")
            out.append(m)
        return out

    # --- scenes and maps ---------------------------------------------------------

    def scenes(self, cdir: Path) -> list[dict]:
        root = self.read(cdir, "GMScenes.lsf")
        if root is None:
            return []
        out = []
        for s in lsx.kids(lsx.region_node(root, "GameMasterScenes"), "GameMasterScene"):
            a = lsx.attrs(s)
            out.append({
                "id": a.get("ID"), "name": a.get("GameMasterSceneName"), "level": a.get("BasedOnLevel"),
                "folder": a.get("GameMasterScenePath") or None,
                "has_cache": (cdir / "LevelCache" / f"{a.get('ID')}.lsf").exists(),
            })
        return out

    def resolve_scene(self, cdir: Path, query: str) -> dict:
        scenes = self.scenes(cdir)
        q = query.strip().lower()
        for s in scenes:
            if q in ((s["id"] or "").lower(), (s["name"] or "").lower()):
                return s
        hits = [s for s in scenes if _match(q, s["id"], s["name"], s["level"])]
        if len(hits) == 1:
            return hits[0]
        names = ", ".join(f"{s['name']} ({s['id']})" for s in (hits or scenes)) or "no scenes"
        raise GMError(f"Scene '{query}' {'not found' if not hits else 'is ambiguous'}. Candidates: {names}")

    def maps(self, cdir: Path) -> list[dict]:
        root = self.read(cdir, "OverviewMaps.lsf")
        if root is None:
            return []
        out = []
        for m in lsx.kids(lsx.region_node(root, "GameMasterManager"), "OverviewMap"):
            a = lsx.attrs(m)
            pins = []
            for p in lsx.kids(m, "OverviewMapPins"):
                pa = lsx.attrs(p)
                pins.append({
                    "name": pa.get("Object"), "scene": pa.get("GameMasterSceneName") or None,
                    "scene_id": pa.get("GameMasterSceneId") or None,
                    "x": round(float(pa.get("OverviewMapPinX", 0)), 1), "y": round(float(pa.get("OverviewMapPinY", 0)), 1),
                    "visible": pa.get("OverviewMapPinVisible") == "True",
                })
            out.append({"uuid": a.get("UUID"), "title": a.get("OverviewMapTitle"),
                        "background": a.get("OverviewMapBackground"), "folder": a.get("OverviewMapPath") or None,
                        "pins": pins})
        return out

    def scene_detail(self, cdir: Path, scene: dict, limit: int = 200) -> dict:
        root = self.read(cdir, f"LevelCache/{scene['id']}.lsf")
        if root is None:
            raise GMError(f"Scene '{scene['name']}' has no LevelCache file: it has never been loaded in the game.")
        out = {"scene": scene, "characters": [], "encounters": [], "notes": []}

        groups = {}
        cgi = lsx.region_node(root, "CombatGroupsInfo")
        if cgi is not None:
            for g in lsx.kids(cgi, "CombatGroupInfo"):
                groups[lsx.get(g, "Id")] = {"id": lsx.get(g, "Id"), "name": lsx.get(g, "CombatGroupName"), "members": []}

        # EntityWorld lists the handles of every entity's components: that is how a character finds its
        # combat component (across 269 scenes of 7 campaigns, each of the 2171 characters has exactly one).
        entity_of = {}
        ew = lsx.region_node(root, "EntityWorld")
        if ew is not None:
            for i, e in enumerate(lsx.kids(ew, "Entity")):
                for c in lsx.kids(e, "Component"):
                    entity_of[lsx.get(c, "Handle")] = i
        combat = {}
        cc = lsx.region_node(root, "CombatComponents")
        if cc is not None:
            handles = [lsx.get(c, "Object") for c in lsx.kids(lsx.kid(cc, "Creators"), "Creator")] \
                if lsx.kid(cc, "Creators") is not None else []
            for h, n in zip(handles, lsx.kids(cc, "CombatComponent")):
                if h in entity_of:
                    combat[entity_of[h]] = lsx.attrs(n)

        chars_root = lsx.region_node(root, "Characters")
        factory = lsx.kid(chars_root, "CharacterFactory") if chars_root is not None else None
        creators = lsx.kids(lsx.kid(factory, "Creators"), "Creator") if factory is not None else []
        chars = lsx.kids(lsx.kid(factory, "Characters"), "Character") if factory is not None else []
        for i, ch in enumerate(chars):
            a = lsx.attrs(ch)
            cr = lsx.attrs(creators[i]) if i < len(creators) else {}
            stats = lsx.kid(ch, "Stats")
            name_node = lsx.kid(ch, "CustomDisplayName")
            info = {
                "index": i,
                "uuid": cr.get("UUID"),
                "name": lsx.get(name_node, "CustomDisplayName") if name_node is not None else None,
                "template_id": a.get("OriginalTemplate") or cr.get("TemplateID"),
                "position": _vec(a.get("Translate")),
                "vitality": _int(a.get("Vitality")),
                "experience": _int(lsx.get(stats, "Experience")) if stats is not None else None,
                "archetype": a.get("Archetype"),
                "trader": a.get("Trader") == "True",
            }
            c = combat.get(entity_of.get(cr.get("Handle")))
            if c is not None:
                info["alignment"] = c.get("Alignment")
                gid = c.get("CombatGroupID") or None
                if gid:
                    info["encounter"] = groups.get(gid, {}).get("name") or gid
                    groups.setdefault(gid, {"id": gid, "name": None, "members": []})["members"].append(i)
            out["characters"].append(info)
        out["character_count"] = len(chars)
        if len(chars) > limit:
            out["characters"] = out["characters"][:limit]
            out["truncated"] = True
        out["encounters"] = list(groups.values())

        items = lsx.region_node(root, "Items")
        if items is not None:
            fac = lsx.kid(items, "ItemFactory")
            out["item_count"] = len(lsx.kids(lsx.kid(fac, "Items"), "Item")) if fac is not None and lsx.kid(fac, "Items") is not None else 0
        out["notes"] = [n for n in self.notes(cdir) if n.get("scene") == scene["id"]]
        return out

    # --- vignettes, sticky notes, journal ---------------------------------------------

    def vignettes(self, cdir: Path) -> list[dict]:
        root = self.read(cdir, "Vignettes.lsf")
        if root is None:
            return []
        out = []
        for v in lsx.kids(lsx.region_node(root, "GameMasterManager"), "Vignette"):
            a = lsx.attrs(v)
            polls = lsx.kid(v, "VignettePolls")
            out.append({
                "uuid": a.get("UUID"), "title": a.get("VignetteTitle"), "text": a.get("VignetteText"),
                "folder": a.get("VignettePath") or None, "image": a.get("VignetteImage"),
                "custom_image": a.get("Flags") == "1",
                "question": (lsx.get(polls, "VignetteQuestion") or None) if polls is not None else None,
                "answers": [lsx.get(x, "Object") for x in lsx.kids(polls, "VignetteAnswers")] if polls is not None else [],
            })
        return out

    def notes(self, cdir: Path) -> list[dict]:
        root = self.read(cdir, "Notes.lsf")
        if root is None:
            return []
        out = []
        for n in lsx.kids(lsx.region_node(root, "GameMasterManager"), "Note"):
            a = lsx.attrs(n)
            note = {"uuid": a.get("UUID"), "title": a.get("NoteTitle"), "text": a.get("NoteText"),
                    "folder": a.get("NotePath") or None}
            if a.get("UI"):
                note.update(kind="map", map=a.get("UI"), x=round(float(a.get("NoteUIX", 0)), 1),
                            y=round(float(a.get("NoteUIY", 0)), 1))
            else:
                note.update(kind="scene", scene=a.get("NoteScene") or None, bound_to=a.get("NoteBoundObjectUUID") or None)
            out.append(note)
        return out

    def journal(self, cdir: Path) -> dict:
        root = self.read(cdir, "Journal.lsf")
        if root is None:
            return {}
        top = lsx.region_node(root, "GMJournal")
        texts = [a.get("value") for a in top.iter("attribute") if a.get("type") == str(T_WSTRING) and a.get("value")]
        return {"text": lsx.get(top, "GMJournalText") or None, "shared": lsx.get(top, "GMJournalShared") == "True",
                "entries": texts[1:] if texts and texts[0] == lsx.get(top, "GMJournalText") else texts}

    # --- templates ----------------------------------------------------------------

    def eggs(self, cdir: Path) -> tuple[ET.Element, ET.Element]:
        root = self.read(cdir, "Eggs.lsf")
        if root is None:
            root = lsx.empty_save("GameMasterManager", self._version(cdir))
        return root, lsx.region_node(root, "GameMasterManager")

    def _version(self, cdir: Path) -> ET.Element | None:
        meta = self.read(cdir, "meta.lsf")
        return meta.find("version") if meta is not None else None

    @staticmethod
    def template_name(t: ET.Element) -> str:
        if t.get("id") == "GMExportItem":
            return lsx.get(t, "CustomDisplayName") or lsx.get(t, "StatsID") or "?"
        return lsx.get(t, "Name") or "?"

    @classmethod
    def template_summary(cls, t: ET.Element) -> dict:
        a = lsx.attrs(t)
        if t.get("id") == "GMExportItem":
            return {"kind": "item", "name": cls.template_name(t), "stats_id": a.get("StatsID"),
                    "template_id": a.get("TemplateID") or a.get("ID"), "level": _int(a.get("Level"))}
        return {
            "kind": "npc", "name": a.get("Name"), "stat_name": a.get("StatName"), "template_id": a.get("TemplateID"),
            "level": _int(a.get("Level")), "alignment": a.get("Alignment"),
            "skills": [lsx.get(s, "ID") for s in lsx.kids(t, "Skill")],
            "items": [lsx.get(i, "StatsID") for i in lsx.kids(t, "Item")],
        }

    @classmethod
    def template_detail(cls, t: ET.Element) -> dict:
        d = cls.template_summary(t)
        a = lsx.attrs(t)
        if d["kind"] == "item":
            d["attributes"] = a
            return d
        attrs = lsx.kids(t, "Attribute")

        def block(n: ET.Element | None) -> dict:
            if n is None:
                return {}
            vals = {k: _int(v, v) for k, v in lsx.attrs(n).items() if k != "HasReflection"}
            ab = lsx.kid(n, "Abilities")
            abilities = {k: _int(v, v) for k, v in lsx.attrs(ab).items()} if ab is not None else {}
            talents = [_int(lsx.get(x, "Object"), 0) for x in lsx.kids(n, "Talents")]
            removed = [_int(lsx.get(x, "Object"), 0) for x in lsx.kids(n, "RemovedTalents")]
            out = {"stats": vals, "abilities": abilities, "talents": talent_names(talents)}
            if any(removed):
                out["removed_talents"] = talent_names(removed)
            return out

        bar = lsx.kids(t, "SkillBar")
        d.update({
            "archetype": a.get("Archetype"), "trader": a.get("Trader") == "True",
            "custom_display_name": a.get("CustomDisplayName") or None,
            "custom_look": a.get("CustomLookEnabled") == "True",
            "base": block(attrs[0] if attrs else None),
            "bonus": block(attrs[1] if len(attrs) > 1 else None),
            "unspent": {k: _int(v, v) for k, v in lsx.attrs(lsx.kid(t, "UnspentPoints")).items()}
            if lsx.kid(t, "UnspentPoints") is not None else {},
            "items": [{
                "stats_id": lsx.get(i, "StatsID"), "template_id": lsx.get(i, "ID"),
                "amount": _int(lsx.get(i, "Amount")), "slot": _int(lsx.get(i, "InventorySlot")),
                "level": _int(lsx.get(i, "Level")), "name": lsx.get(i, "CustomDisplayName") or None,
                "has_stats_blob": bool(lsx.get(i, "Base")),
            } for i in lsx.kids(t, "Item")],
            "skillbar": [(n, lsx.get(s, "ID")) for n, s in enumerate(bar) if lsx.get(s, "SlotType") != "0"],
            "skillbar_slots": len(bar),
        })
        return d

    def find_template(self, cdir: Path, name: str, kind: str | None = None) -> ET.Element | None:
        _, top = self.eggs(cdir)
        q = name.strip().lower()
        for t in lsx.kids(top):
            if kind and self.template_summary(t)["kind"] != kind:
                continue
            if self.template_name(t).strip().lower() == q:
                return t
        return None

    def template_ref(self, ref: str) -> ET.Element:
        """'Campaign:Template name', or just a name, which is then looked up in all campaigns."""
        if ":" in ref:
            camp, name = ref.split(":", 1)
            t = self.find_template(self.resolve(camp), name)
            if t is None:
                raise GMError(f"Campaign '{camp}' has no template '{name}'")
            return t
        found = [(d, t) for d in self.campaign_dirs() if (t := self.find_template(d, ref)) is not None]
        if len(found) == 1:
            return found[0][1]
        if not found:
            raise GMError(f"Template '{ref}' not found in any campaign. Search with dos2_find_templates.")
        raise GMError(f"Template '{ref}' exists in several campaigns: {', '.join(d.name for d, _ in found)}. "
                      "Use 'Campaign:Name'.")

    def search_templates(self, query: str, campaign: str | None = None, kind: str | None = None,
                         limit: int = 30) -> list[dict]:
        dirs = [self.resolve(campaign)] if campaign else self.campaign_dirs()
        out = []
        for d in dirs:
            try:
                _, top = self.eggs(d)
            except Exception:
                continue
            for t in lsx.kids(top):
                s = self.template_summary(t)
                if kind and s["kind"] != kind:
                    continue
                hay = [s.get("name"), s.get("stat_name"), s.get("stats_id"), s.get("template_id"),
                       *s.get("skills", []), *s.get("items", [])]
                if not query or _match(query, *hay):
                    s["campaign"] = d.name
                    out.append(s)
                    if len(out) >= limit:
                        return out
        return out

    # --- template editing ------------------------------------------------------------

    def put_template(self, top: ET.Element, name: str, base: ET.Element | None, *,
                     template_id=None, stat_name=None, level=None, alignment=None, archetype=None,
                     trader=None, display_name=None, bonus: dict | None = None,
                     ability_bonus: dict | None = None, skills: list[str] | None = None,
                     items: list[dict] | None = None, items_from: ET.Element | None = None,
                     reset_bonus: bool = False, talents: list[str] | None = None,
                     remove_talents: list[str] | None = None) -> tuple[ET.Element, bool]:
        container = lsx.children(top, create=True)
        existing = None
        for t in lsx.kids(top, "GMExportCharacter"):
            if (lsx.get(t, "Name") or "").strip().lower() == name.strip().lower():
                existing = t
                break
        if base is not None and base.get("id") != "GMExportCharacter":
            raise GMError("The base for an NPC must be an NPC template, but this is an item")
        created = existing is None
        if existing is None and base is None:
            raise GMError(f"The campaign has no template '{name}': specify base, the template to copy it from")
        if base is not None:
            t = lsx.copy(base)
            if existing is not None:
                container.insert(list(container).index(existing), t)
                container.remove(existing)
            else:
                container.append(t)
        else:
            t = existing

        lsx.set_attr(t, "Name", T_WSTRING, name)
        if template_id is not None:
            lsx.set_attr(t, "TemplateID", T_FIXED, template_id)
        if stat_name is not None:
            lsx.set_attr(t, "StatName", T_FIXED, stat_name)
        if level is not None:
            lsx.set_attr(t, "Level", T_INT, int(level))
            lsx.set_attr(t, "GMExportCharacterLevel", T_BOOL, True)
        if alignment is not None:
            if alignment not in ALIGNMENTS:
                raise GMError(f"alignment: one of {', '.join(ALIGNMENTS)}")
            lsx.set_attr(t, "Alignment", T_FIXED, alignment)
        if archetype is not None:
            lsx.set_attr(t, "Archetype", T_FIXED, archetype)
        if trader is not None:
            lsx.set_attr(t, "Trader", T_BOOL, bool(trader))
        if display_name is not None:
            lsx.set_attr(t, "CustomDisplayName", T_WSTRING, display_name)

        if bonus or ability_bonus or reset_bonus or talents or remove_talents:
            attrs = lsx.kids(t, "Attribute")
            if len(attrs) < 2:
                raise GMError("The base has no second Attribute node for GM edits")
            gm = attrs[1]
            if reset_bonus:
                for a in list(gm.findall("attribute")):
                    if a.get("id") != "HasReflection":
                        gm.remove(a)
                ab = lsx.kid(gm, "Abilities")
                if ab is not None:
                    for a in list(ab.findall("attribute")):
                        ab.remove(a)
                for nid in ("Talents", "RemovedTalents"):
                    for w in lsx.kids(gm, nid):
                        lsx.set_attr(w, "Object", T_UINT, 0)
            if talents:
                _set_talent_bits(gm, "Talents", talents, True)
                _set_talent_bits(gm, "RemovedTalents", talents, False)
            if remove_talents:
                _set_talent_bits(gm, "RemovedTalents", remove_talents, True)
                _set_talent_bits(gm, "Talents", remove_talents, False)
            for k, v in (bonus or {}).items():
                if int(v) == 0:
                    lsx.del_attr(gm, k)
                else:
                    lsx.set_attr(gm, k, T_INT, int(v))
            if ability_bonus:
                ab = lsx.kid(gm, "Abilities")
                if ab is None:
                    ab = ET.Element("node", id="Abilities")
                    lsx.children(gm, create=True).insert(0, ab)
                for k, v in ability_bonus.items():
                    if int(v) == 0:
                        lsx.del_attr(ab, k)
                    else:
                        lsx.set_attr(ab, k, T_INT, int(v))

        if items is not None or items_from is not None:
            new_items = [lsx.copy(i) for i in lsx.kids(items_from, "Item")] if items_from is not None else []
            lvl = _int(lsx.get(t, "Level"), 1)
            taken = {_int(lsx.get(i, "InventorySlot")) for i in new_items}
            backpack = BACKPACK_SLOT
            for it in items or []:
                while backpack in taken:
                    backpack += 1
                node = self._item_node(it, lvl, backpack)
                taken.add(_int(lsx.get(node, "InventorySlot")))
                new_items.append(node)
            self._replace_kids(t, "Item", new_items, after=("UnspentPoints", "Attribute"))

        if skills is not None:
            self._replace_kids(t, "Skill", [lsx.node("Skill", [("ID", T_FIXED, s), ("Active", T_BOOL, True)])
                                            for s in skills], after=("Item", "UnspentPoints", "Attribute"))
            bar = lsx.kids(t, "SkillBar")
            ch = lsx.children(t, create=True)
            while len(bar) < SKILLBAR_SLOTS:
                slot = lsx.node("SkillBar", [("SlotType", T_INT8, 0)])
                anchor = bar[-1] if bar else (lsx.kids(t, "Skill") or [None])[-1]
                ch.insert(list(ch).index(anchor) + 1 if anchor is not None else len(ch), slot)
                bar.append(slot)
            for n, slot in enumerate(bar):
                for a in list(slot.findall("attribute")):
                    slot.remove(a)
                if n < len(skills):
                    for aid, typ, val in (("SlotType", T_INT8, 1), ("ID", T_FIXED, skills[n]), ("Item", T_FIXED, "")):
                        lsx.set_attr(slot, aid, typ, val)
                else:
                    lsx.set_attr(slot, "SlotType", T_INT8, 0)
        return t, created

    @staticmethod
    def _item_node(it: dict, level: int, default_slot: int) -> ET.Element:
        if not it.get("stats_id") or not it.get("template_id"):
            raise GMError("An item needs stats_id (stats entry) and template_id (root template)")
        slot = it.get("slot", default_slot)
        if isinstance(slot, str):
            if slot not in EQUIP_SLOTS:
                raise GMError(f"slot: a number or one of {', '.join(EQUIP_SLOTS)}")
            slot = EQUIP_SLOTS[slot]
        return lsx.node("Item", [
            ("UUID", T_FIXED, ""), ("ID", T_FIXED, it["template_id"]), ("Amount", T_INT, int(it.get("amount", 1))),
            ("ItemGroup", T_FIXED, "Common"), ("StatsID", T_FIXED, it["stats_id"]), ("Identified", T_BOOL, True),
            ("Level", T_INT, int(it.get("level", level))), ("IsChangedByGameMaster", T_BOOL, False),
            ("CustomBaseStats", T_BOOL, False), ("CustomDisplayName", T_WSTRING, it.get("name", "")),
            ("CustomDescription", T_WSTRING, it.get("description", "")), ("CustomBookContent", T_WSTRING, ""),
            ("InventorySlot", T_USHORT, int(slot)), ("Floating", T_BOOL, False), ("Boost", T_SCRATCH, ""),
            ("Base", T_SCRATCH, ""), ("Parent", T_USHORT, 65535), ("Inventory", T_USHORT, 65535),
            ("IsGenerated", T_BOOL, False),
        ])

    @staticmethod
    def _replace_kids(t: ET.Element, nid: str, new: list[ET.Element], after: tuple[str, ...]) -> None:
        ch = lsx.children(t, create=True)
        old = [n for n in ch if n.get("id") == nid]
        if old:
            pos = list(ch).index(old[0])
        else:
            pos = 0
            for anchor in after:
                prev = [n for n in ch if n.get("id") == anchor]
                if prev:
                    pos = list(ch).index(prev[-1]) + 1
                    break
        for n in old:
            ch.remove(n)
        for k, n in enumerate(new):
            ch.insert(pos + k, n)

    # --- item templates ---------------------------------------------------------------

    @staticmethod
    def item_sample(key: str) -> ET.Element:
        """A GMExportItem sample from data/item_samples.lsx: quest, weapon, weapon_generated, armor."""
        root = ET.parse(Path(__file__).parent / "data" / "item_samples.lsx").getroot()
        for n in root:
            if n.get("sample") == key:
                n = lsx.copy(n)
                n.attrib.pop("sample", None)
                return n
        raise GMError(f"No item sample '{key}': quest, weapon, weapon_generated, armor")

    def put_item_template(self, top: ET.Element, name: str, base: ET.Element, *, template_id=None, stats_id=None,
                          level=None, rarity=None, description=None, book=None, amount=None,
                          boosts: list[str] | None = None) -> tuple[ET.Element, bool]:
        """A saved GM item template: a copy of base with edits. An existing one with the same name is replaced."""
        if base.get("id") != "GMExportItem":
            raise GMError("The base for an item must be an item template")
        container = lsx.children(top, create=True)
        t = lsx.copy(base)
        existing = next((n for n in lsx.kids(top, "GMExportItem")
                         if self.template_name(n).strip().lower() == name.strip().lower()), None)
        if existing is not None:
            container.insert(list(container).index(existing), t)
            container.remove(existing)
        else:
            container.append(t)
        lsx.set_attr(t, "CustomDisplayName", T_WSTRING, name)
        if template_id:
            for aid in ("TemplateID", "ID", "OriginalTemplate"):
                lsx.set_attr(t, aid, T_FIXED, template_id)
        if stats_id:
            lsx.set_attr(t, "StatsID", T_FIXED, stats_id)
        if level is not None:
            lsx.set_attr(t, "Level", T_INT, int(level))
        if rarity:
            lsx.set_attr(t, "ItemGroup", T_FIXED, rarity)
        if description is not None:
            lsx.set_attr(t, "CustomDescription", T_WSTRING, description)
        if book is not None:
            lsx.set_attr(t, "CustomBookContent", T_WSTRING, book)
        if amount is not None:
            lsx.set_attr(t, "Amount", T_INT, int(amount))
        gen = lsx.kid(t, "ItemGeneration")
        if gen is not None:
            if stats_id:
                lsx.set_attr(gen, "Base", T_FIXED, stats_id)
            if rarity:
                lsx.set_attr(gen, "ItemType", T_FIXED, rarity)
            if level is not None:
                lsx.set_attr(gen, "Level", lsx.T_SHORT, int(level))
            if boosts is not None:
                ch = lsx.children(gen, create=True)
                for b in list(ch):
                    if b.get("id") == "Boost":
                        ch.remove(b)
                for b in boosts:
                    ch.append(lsx.node("Boost", [("Object", T_FIXED, b)]))
        elif boosts:
            raise GMError("boosts can only be set on a base that has ItemGeneration, e.g. weapon_generated")
        return t, existing is None

    # --- journal and scenes ----------------------------------------------------------------

    def journal_root(self, cdir: Path, categories: list[dict]) -> ET.Element:
        """The whole GM journal: [{title, shared, chapters: [{title, shared, paragraphs: [{text, shared}]}]}]."""
        root = lsx.empty_save("GMJournal", self._version(cdir))
        top = lsx.region_node(root, "GMJournal")
        lsx.set_attr(top, "GMJournalText", T_WSTRING, "")
        lsx.set_attr(top, "GMJournalShared", T_BOOL, False)

        def level(parent: ET.Element, holder: str, item: str, entries: list[dict], text_key: str, sub=None):
            box = ET.SubElement(lsx.children(parent, create=True), "node", id=holder)
            for e in entries:
                n = ET.SubElement(lsx.children(box, create=True), "node", id=item)
                lsx.set_attr(n, "GMJournalText", T_WSTRING, e.get(text_key, ""))
                lsx.set_attr(n, "GMJournalShared", T_BOOL, bool(e.get("shared", False)))
                if sub:
                    sub(n, e)

        def chapters(n, cat):
            level(n, "GMJournalChapters", "GMJournalChapter", cat.get("chapters", []), "title",
                  lambda c, ch: level(c, "GMJournalParagraphs", "GMJournalParagraph", ch.get("paragraphs", []), "text"))

        level(top, "GMJournalCategories", "GMJournalCategory", categories, "title", chapters)
        return root

    def set_scene(self, cdir: Path, scene: dict, **fields) -> ET.Element:
        root = self.read(cdir, "GMScenes.lsf")
        for s in lsx.kids(lsx.region_node(root, "GameMasterScenes"), "GameMasterScene"):
            if lsx.get(s, "ID") == scene["id"]:
                names = {"name": ("GameMasterSceneName", T_WSTRING), "folder": ("GameMasterScenePath", T_WSTRING),
                         "atmosphere": ("Atmosphere", T_FIXED), "ambient": ("Ambient", T_FIXED), "music": ("Music", T_FIXED)}
                for k, v in fields.items():
                    if v is not None:
                        aid, typ = names[k]
                        lsx.set_attr(s, aid, typ, v)
                return root
        raise GMError(f"No scene {scene['id']} in GMScenes.lsf")

    # --- vignette and sticky note editing ------------------------------------------------

    def vignettes_root(self, cdir: Path) -> tuple[ET.Element, ET.Element]:
        root = self.read(cdir, "Vignettes.lsf")
        if root is None:
            root = lsx.empty_save("GameMasterManager", self._version(cdir))
        return root, lsx.region_node(root, "GameMasterManager")

    @staticmethod
    def vignette_node(uid: str, title: str, text: str, answers: list[str], question: str, folder: str,
                      image: str, size: tuple[int, int] | None, canvas: str, frame: str) -> ET.Element:
        custom = size is not None
        w, h = size or STOCK_SIZE
        zoom = FRAME / max(w, h)
        n = lsx.node("Vignette", [
            ("UUID", T_FIXED, uid), ("VignetteBackground", T_WSTRING, canvas), ("VignetteFrame", T_WSTRING, frame),
            ("VignetteImage", T_WSTRING, image), ("VignetteBackgroundModule", T_FIXED, GM_MODULE),
            ("VignetteFrameModule", T_FIXED, GM_MODULE), ("VignetteImageModule", T_FIXED, "" if custom else GM_MODULE),
            ("VignetteFrameTransformX", T_FLOAT, 165), ("VignetteFrameTransformY", T_FLOAT, 130),
            ("VignetteFrameTransformWidth", T_FLOAT, FRAME), ("VignetteFrameTransformHeight", T_FLOAT, FRAME),
            ("VignetteImageShiftX", T_FLOAT, round((FRAME - w * zoom) / 2, 4)),
            ("VignetteImageShiftY", T_FLOAT, round((FRAME - h * zoom) / 2, 4)),
            ("VignetteImageZoom", T_FLOAT, round(zoom, 7)), ("Flags", lsx.T_BYTE, 1 if custom else 0),
            ("VignetteTitle", T_WSTRING, title), ("VignetteText", T_WSTRING, text.strip()),
            ("VignettePath", T_WSTRING, folder),
        ])
        polls = ET.SubElement(lsx.children(n, create=True), "node", id="VignettePolls")
        lsx.set_attr(polls, "VignetteQuestion", T_WSTRING, question)
        answers_node = lsx.children(polls, create=True)
        for a in answers:
            lsx.set_attr(ET.SubElement(answers_node, "node", id="VignetteAnswers"), "Object", T_WSTRING, a)
        return n

    def notes_root(self, cdir: Path) -> tuple[ET.Element, ET.Element]:
        root = self.read(cdir, "Notes.lsf")
        if root is None:
            root = lsx.empty_save("GameMasterManager", self._version(cdir))
        return root, lsx.region_node(root, "GameMasterManager")

    @staticmethod
    def note_node(uid: str, title: str, text: str, folder: str, *, map_uuid: str = "", x: float = 0, y: float = 0,
                  scene_id: str = "", bound_uuid: str = "") -> ET.Element:
        return lsx.node("Note", [
            ("UUID", T_FIXED, uid), ("Type", T_INT8, 2 if map_uuid else 0), ("NoteTitle", T_WSTRING, title),
            ("NoteText", T_WSTRING, text.strip()), ("NotePath", T_WSTRING, folder), ("UI", T_FIXED, map_uuid),
            ("NoteUIX", T_FLOAT, round(float(x), 4)), ("NoteUIY", T_FLOAT, round(float(y), 4)),
            ("NoteBoundObjectUUID", T_FIXED, bound_uuid), ("NoteScene", T_FIXED, scene_id),
        ])

    @staticmethod
    def upsert(top: ET.Element, nid: str, uid: str, new: ET.Element) -> bool:
        """Puts the node in place of the node with the same UUID; True if the node is new."""
        container = lsx.children(top, create=True)
        for n in lsx.kids(top, nid):
            if lsx.get(n, "UUID") == uid:
                container.insert(list(container).index(n), new)
                container.remove(n)
                return False
        container.append(new)
        return True

    @staticmethod
    def remove_where(top: ET.Element, nid: str, pred) -> list[str]:
        container = lsx.children(top)
        removed = []
        for n in lsx.kids(top, nid):
            if pred(n):
                container.remove(n)
                removed.append(lsx.get(n, "UUID") or lsx.get(n, "Name") or "?")
        return removed

    # --- campaign add-ons ------------------------------------------------------------

    @staticmethod
    def set_addon(meta_root: ET.Element, uuid_: str, name: str, version: int, folder: str, attach: bool) -> bool:
        """Adds or removes an add-on in meta.lsf → Dependencies. True if anything changed."""
        cfg = lsx.region_node(meta_root, "Config")
        deps = lsx.kid(cfg, "Dependencies")
        if deps is None:
            deps = ET.Element("node", id="Dependencies")
            lsx.children(cfg, create=True).insert(0, deps)
        current = [m for m in lsx.kids(deps, "ModuleShortDesc") if lsx.get(m, "UUID") == uuid_]
        if attach and not current:
            lsx.children(deps, create=True).append(lsx.node("ModuleShortDesc", [
                ("UUID", T_FIXED, uuid_), ("Name", T_FIXED, name), ("Version", T_INT, version),
                ("MD5", lsx.T_LSSTRING, ""), ("Folder", T_WSTRING, folder),
            ]))
            return True
        if not attach and current:
            for m in current:
                lsx.children(deps).remove(m)
            return True
        return False

    # --- writing with a backup ------------------------------------------------------------

    def writer(self, cdir: Path, label: str, allow_running: bool = False) -> "Writer":
        if game_running() and not allow_running:
            raise GMError("The game is running. Save the campaign and exit to the main menu, then retry with "
                          "allow_running=true; an open campaign overwrites the files on its next save.")
        return Writer(self, cdir, label)

    def backup_root(self, cdir: Path) -> Path:
        return self.paths.backups / cdir.name

    def backups(self, cdir: Path) -> list[dict]:
        root = self.backup_root(cdir)
        if not root.exists():
            return []
        out = []
        for b in sorted(root.iterdir(), reverse=True):
            m = b / "manifest.json"
            if m.exists():
                data = json.loads(m.read_text("utf-8"))
                out.append({"id": b.name, "label": data.get("label"), "files": data.get("files"),
                            "created_files": data.get("created")})
        return out

    def full_backup(self, cdir: Path, label: str = "full") -> str:
        dest = self.backup_root(cdir) / f"{datetime.now():%Y-%m-%d_%H-%M-%S}-{label}"
        files = [f.relative_to(cdir).as_posix() for f in cdir.rglob("*") if f.is_file()]
        for rel in files:
            (dest / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cdir / rel, dest / "files" / rel)
        (dest / "manifest.json").write_text(json.dumps({"label": label, "files": files, "created": []},
                                                       ensure_ascii=False, indent=1), "utf-8")
        return dest.name

    def restore(self, cdir: Path, backup_id: str, allow_running: bool = False) -> dict:
        src = self.backup_root(cdir) / backup_id
        if not (src / "manifest.json").exists():
            raise GMError(f"No backup {backup_id}. List them with dos2_backups.")
        data = json.loads((src / "manifest.json").read_text("utf-8"))
        w = self.writer(cdir, "before-restore", allow_running)
        for rel in data["files"]:
            w.stage_copy(src / "files" / rel, rel)
        for rel in data.get("created", []):
            w.stage_delete(rel)
        result = w.commit()
        result["restored_from"] = backup_id
        return result


class Writer:
    """The change set of one operation: first a backup of the previous files, then the replacement."""

    def __init__(self, gm: GM, cdir: Path, label: str):
        self.gm, self.cdir, self.label = gm, cdir, label
        self.ops: list[tuple[str, str, Path | None]] = []  # (rel, "put"/"copy"/"delete", source)

    def stage(self, rel: str, root: ET.Element) -> None:
        self.ops.append((rel, "put", self.gm.divine.build(root)))
        # Sanity check: the built file reads back.
        self.gm.divine.read(self.ops[-1][2])

    def stage_copy(self, src: Path, rel: str) -> None:
        self.ops.append((rel, "copy", src))

    def stage_delete(self, rel: str) -> None:
        self.ops.append((rel, "delete", None))

    def commit(self) -> dict:
        if not self.ops:
            return {"changed": []}
        stamp = f"{datetime.now():%Y-%m-%d_%H-%M-%S-%f}"[:-3]
        dest = self.gm.backup_root(self.cdir) / f"{stamp}-{self.label}"
        backed, created = [], []
        for rel, _, _ in self.ops:
            cur = self.cdir / rel
            if cur.exists():
                (dest / "files" / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cur, dest / "files" / rel)
                backed.append(rel)
            else:
                created.append(rel)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "manifest.json").write_text(json.dumps({"label": self.label, "files": backed, "created": created},
                                                       ensure_ascii=False, indent=1), "utf-8")
        for rel, op, src in self.ops:
            target = self.cdir / rel
            if op == "delete":
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            if op == "put":
                src.unlink(missing_ok=True)
        return {"changed": [rel for rel, _, _ in self.ops], "backup": dest.name}
