"""Read check on real campaigns; writes nothing.

uv run python tests/smoke_read.py "My Campaign" [another campaign] > read.json

The first argument is the name (or folder, UUID, or a part of one) of a campaign that exists on disk.
The optional second one is another campaign whose sticky notes are read as well.
"""
import json
import sys

from dos2gm import server as s

if len(sys.argv) < 2:
    sys.exit('usage: python tests/smoke_read.py "<campaign>" [another campaign] > read.json\n'
             "No campaign given. Pass the name of a campaign that exists on disk; list them with:\n"
             '  python -c "from dos2gm import server as s; print(s.dos2_status())"')
camp = sys.argv[1]
other = sys.argv[2] if len(sys.argv) > 2 else None
out = {}
st = s.dos2_status()
out["status"] = {k: v for k, v in st.items() if k != "campaigns"}
out["campaigns"] = [{k: c.get(k) for k in ("name", "folder", "scenes", "addons", "modified", "error")}
                    for c in st.get("campaigns", [])]
c = s.dos2_campaign(camp)
out["campaign"] = c
for sc in c["scenes"]:
    out.setdefault("scenes", []).append(s.dos2_scene(camp, sc["name"]))
for t in s.dos2_templates(camp):
    out.setdefault("templates", []).append(s.dos2_template(camp, t["name"]))
out["find_demon"] = s.dos2_find_templates("demon", limit=5)
out["find_bow"] = s.dos2_find_templates("WPN_Bow", kind="npc", limit=3)
out["vignettes"] = s.dos2_vignettes(camp)
out["notes"] = s.dos2_notes(camp)
if other:
    out["notes_other"] = s.dos2_notes(other)[:3]
out["raw_journal"] = s.dos2_raw(camp, "Journal.lsf")
out["raw_xpath"] = s.dos2_raw(camp, "OverviewMaps.lsf", ".//node[@id='OverviewMapPins']")
out["backups"] = s.dos2_backups(camp)
json.dump(out, sys.stdout.buffer and open(1, "w", encoding="utf-8", closefd=False), ensure_ascii=False, indent=1)
