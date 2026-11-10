"""Where the GM campaigns, LSLib, the cache and the backups live.

Lookup order: environment variable → config.json in the project root → default value.
  DOS2GM_CAMPAIGNS  the GMCampaigns folder
  DOS2GM_DIVINE     path to Divine.exe from LSLib
  DOS2GM_BACKUPS    the backups folder
  DOS2GM_GAME       the game folder (by default it is looked up in the Steam libraries)
  DOS2GM_OSIRIS_DATA, DOS2GM_MODS  the "Osiris Data" and "Mods" folders in the game's documents folder
"""
import ctypes
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
GAME_DOCS = Path("Larian Studios") / "Divinity Original Sin 2 Definitive Edition"


def documents_dir() -> Path:
    """The Documents folder, honouring relocation (it may be moved to another drive or a cloud folder)."""
    if sys.platform != "win32":
        return Path.home() / "Documents"
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL
    return Path(buf.value) if buf.value else Path.home() / "Documents"


@dataclass(frozen=True)
class Paths:
    campaigns: Path
    divine: Path
    backups: Path
    cache: Path
    osiris_data: Path
    mods: Path
    game: Path | None


def steam_game_dir() -> Path | None:
    """The DOS2 folder in the Steam libraries (from the registry and libraryfolders.vdf)."""
    import re
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        return None
    libs = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        libs += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text("utf-8", "replace"))]
    for lib in libs:
        d = lib / "steamapps" / "common" / "Divinity Original Sin 2"
        if (d / "DefEd").exists():
            return d
    return None


def load() -> Paths:
    cfg = {}
    cfg_file = PROJECT / "config.json"
    if cfg_file.exists():
        cfg = json.loads(cfg_file.read_text("utf-8"))

    def pick(env: str, key: str, default: Path) -> Path:
        return Path(os.environ.get(env) or cfg.get(key) or default)

    docs = documents_dir() / GAME_DOCS
    game = os.environ.get("DOS2GM_GAME") or cfg.get("game")
    return Paths(
        campaigns=pick("DOS2GM_CAMPAIGNS", "campaigns", docs / "GMCampaigns"),
        divine=pick("DOS2GM_DIVINE", "divine", PROJECT / "lslib" / "Packed" / "Tools" / "Divine.exe"),
        backups=pick("DOS2GM_BACKUPS", "backups", PROJECT / "backups"),
        cache=PROJECT / ".cache",
        osiris_data=pick("DOS2GM_OSIRIS_DATA", "osiris_data", docs / "Osiris Data"),
        mods=pick("DOS2GM_MODS", "mods", docs / "Mods"),
        game=Path(game) if game else steam_game_dir(),
    )
