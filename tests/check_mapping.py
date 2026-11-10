"""Scene read check across all campaigns on disk; writes nothing.

uv run python tests/check_mapping.py

1. Every scene character has a combat component (alignment), and every encounter member has the encounter's name.
2. Scene sticky notes are bound to marker items (template f99e4476-…) of that scene.
"""
import sys

from dos2gm import lsx, paths
from dos2gm.gm import GM

STICKY_MARKER = "f99e4476-e744-4a68-8679-78aaa3d03f66"

sys.stdout.reconfigure(encoding="utf-8")
g = GM(paths.load())
st = {"scenes": 0, "chars": 0, "no_alignment": 0, "in_encounter": 0, "encounter_unnamed": 0,
      "notes": 0, "note_on_marker": 0, "note_other": 0}
for d in g.campaign_dirs():
    notes = [n for n in g.notes(d) if n["kind"] == "scene" and n.get("bound_to")]
    for s in g.scenes(d):
        if not s["has_cache"]:
            continue
        det = g.scene_detail(d, s, limit=100_000)
        st["scenes"] += 1
        for c in det["characters"]:
            st["chars"] += 1
            st["no_alignment"] += "alignment" not in c
            if "encounter" in c:
                st["in_encounter"] += 1
        st["encounter_unnamed"] += sum(1 for e in det["encounters"] if not e["name"])
        root = g.read(d, f"LevelCache/{s['id']}.lsf")
        ifac = lsx.kid(lsx.region_node(root, "Items"), "ItemFactory")
        tpl = {lsx.get(c, "UUID"): lsx.get(c, "TemplateID")
               for c in lsx.kids(lsx.kid(ifac, "Creators"), "Creator")} if ifac is not None else {}
        for n in notes:
            if n["scene"] == s["id"]:
                st["notes"] += 1
                st["note_on_marker" if tpl.get(n["bound_to"]) == STICKY_MARKER else "note_other"] += 1
print(st)
ok = st["no_alignment"] == 0 and st["note_other"] == 0
print("OK" if ok else "FAIL")
sys.exit(0 if ok else 1)
