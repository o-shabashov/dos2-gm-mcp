"""LSF ↔ LSX through Divine.exe, plus small helpers for the LSX tree."""
import os
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

# LSX attribute type codes that occur in campaign files.
T_BYTE, T_SHORT, T_USHORT, T_INT, T_UINT, T_FLOAT = 1, 2, 3, 4, 5, 6
T_DOUBLE, T_BOOL, T_FIXED, T_LSSTRING, T_INT8, T_SCRATCH = 7, 19, 22, 23, 27, 25
T_TRANSLATED, T_WSTRING = 28, 30


class LsxError(RuntimeError):
    pass


class Divine:
    """LSLib converter. Keeps the files it has read in memory, keyed by (path, mtime, size)."""

    def __init__(self, exe: Path, cache: Path):
        self.exe = exe
        self.cache = cache
        self._mem: dict[tuple, bytes] = {}

    def _run(self, src: Path, dst: Path, action: str = "convert-resource") -> str:
        if not self.exe.exists():
            raise LsxError(f"Divine.exe not found: {self.exe}. Set its path in DOS2GM_DIVINE or config.json.")
        env = dict(os.environ, DOTNET_ROLL_FORWARD="Major")
        args = [str(self.exe), "-g", "dos2de", "-a", action, "-s", str(src)]
        if dst is not None:
            args += ["-d", str(dst)]
        r = subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or "[ERROR]" in r.stdout or (dst is not None and not dst.exists()):
            raise LsxError(f"Divine.exe {action} {src.name}:\n{r.stdout}{r.stderr}")
        return r.stdout

    def package(self, src_dir: Path, pak: Path) -> list[str]:
        """Builds a .pak from a folder (which contains Mods/<mod folder>/…) and returns the list of files in it."""
        tmp = self._tmp(".pak")
        try:
            self._run(src_dir, tmp, "create-package")
            listing = self._run(tmp, None, "list-package")
            pak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tmp, pak)
        finally:
            tmp.unlink(missing_ok=True)
        return [line.split("\t")[0] for line in listing.splitlines() if "\t" in line]

    def _tmp(self, suffix: str) -> Path:
        self.cache.mkdir(parents=True, exist_ok=True)
        return self.cache / f"{uuid.uuid4().hex}{suffix}"

    def read(self, lsf: Path) -> ET.Element:
        """The LSX tree of a .lsf (or .lsx) file. Every call returns its own copy."""
        st = lsf.stat()
        key = (str(lsf).lower(), st.st_mtime_ns, st.st_size)
        data = self._mem.get(key)
        if data is None:
            if lsf.suffix.lower() == ".lsx":
                data = lsf.read_bytes()
            else:
                tmp = self._tmp(".lsx")
                try:
                    self._run(lsf, tmp)
                    data = tmp.read_bytes()
                finally:
                    tmp.unlink(missing_ok=True)
            if len(self._mem) > 64:
                self._mem.clear()
            self._mem[key] = data
        return ET.fromstring(data)

    def build(self, root: ET.Element) -> Path:
        """Builds a .lsf into a temporary file and returns its path."""
        lsx = self._tmp(".lsx")
        lsf = self._tmp(".lsf")
        tree = ET.ElementTree(root)
        ET.indent(tree, space="\t")
        lsx.write_text('<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n",
                       encoding="utf-8-sig")
        try:
            self._run(lsx, lsf)
        finally:
            lsx.unlink(missing_ok=True)
        return lsf


def to_text(el: ET.Element) -> str:
    el = _copy(el)
    ET.indent(el, space="  ")
    return ET.tostring(el, encoding="unicode")


def _copy(el: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(el))


def empty_save(region: str, version: ET.Element | None = None) -> ET.Element:
    """An empty campaign file with one region and a node of the same name."""
    root = ET.Element("save")
    if version is not None:
        root.append(_copy(version))
    else:
        ET.SubElement(root, "version", major="3", minor="6", revision="9", build="0",
                      lslib_meta="v1,bswap_guids,lsf_keys_adjacency")
    reg = ET.SubElement(root, "region", id=region)
    ET.SubElement(reg, "node", id=region)
    return root


# --- nodes and attributes -------------------------------------------------------

def attrs(node: ET.Element) -> dict[str, str]:
    return {a.get("id"): a.get("value", "") for a in node.findall("attribute")}


def get(node: ET.Element, aid: str, default=None):
    a = node.find(f"attribute[@id='{aid}']")
    return a.get("value") if a is not None else default


def set_attr(node: ET.Element, aid: str, type_: int, value) -> None:
    if isinstance(value, bool):
        value = "True" if value else "False"
    a = node.find(f"attribute[@id='{aid}']")
    if a is None:
        a = ET.Element("attribute", id=aid, type=str(type_), value=str(value))
        # attributes go before <children>
        ch = node.find("children")
        if ch is not None:
            node.insert(list(node).index(ch), a)
        else:
            node.append(a)
    else:
        a.set("type", str(type_))
        a.set("value", str(value))


def del_attr(node: ET.Element, aid: str) -> None:
    a = node.find(f"attribute[@id='{aid}']")
    if a is not None:
        node.remove(a)


def children(node: ET.Element, create: bool = False) -> ET.Element | None:
    ch = node.find("children")
    if ch is None and create:
        ch = ET.SubElement(node, "children")
    return ch


def kids(node: ET.Element, nid: str | None = None) -> list[ET.Element]:
    ch = node.find("children")
    if ch is None:
        return []
    return [n for n in ch if nid is None or n.get("id") == nid]


def kid(node: ET.Element, nid: str) -> ET.Element | None:
    ch = node.find("children")
    return ch.find(f"node[@id='{nid}']") if ch is not None else None


def region_node(root: ET.Element, region: str) -> ET.Element | None:
    """The top node of a region (<region id=X><node id=...>)."""
    reg = root.find(f"region[@id='{region}']")
    return reg.find("node") if reg is not None else None


def node(nid: str, attributes: list[tuple[str, int, object]] = ()) -> ET.Element:
    n = ET.Element("node", id=nid)
    for aid, t, v in attributes:
        set_attr(n, aid, t, v)
    return n


def copy(el: ET.Element) -> ET.Element:
    return _copy(el)
