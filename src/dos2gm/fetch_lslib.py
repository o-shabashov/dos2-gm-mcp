"""Downloads the official LSLib release that dos2-gm-mcp drives (Divine.exe) into ./lslib.

LSLib is written by Norbyte and published under the MIT license: https://github.com/Norbyte/lslib
This project does not redistribute it. Run once after cloning:

    uv run dos2-gm-fetch-lslib
"""
import hashlib
import io
import sys
import urllib.request
import zipfile

from .paths import PROJECT

VERSION = "v1.20.4"
URL = f"https://github.com/Norbyte/lslib/releases/download/{VERSION}/ExportTool-{VERSION}.zip"
SHA256 = "5e02368fb8acafda9b45acba37a3f3bf507fc3d65a083a159abbeab06337190e"
TARGET = PROJECT / "lslib"
DIVINE = TARGET / "Packed" / "Tools" / "Divine.exe"


def main() -> int:
    force = "--force" in sys.argv[1:]
    if DIVINE.is_file() and not force:
        print(f"LSLib is already here: {DIVINE}\nUse --force to download it again.")
        return 0
    print(f"Downloading LSLib {VERSION} from {URL}")
    with urllib.request.urlopen(URL, timeout=120) as resp:
        blob = resp.read()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != SHA256:
        print(f"Checksum mismatch: expected {SHA256}, got {digest}. Nothing was unpacked.", file=sys.stderr)
        return 1
    root = TARGET.resolve()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            dest = (root / info.filename).resolve()
            if root != dest and root not in dest.parents:
                print(f"Refusing to unpack outside {root}: {info.filename}", file=sys.stderr)
                return 1
        z.extractall(root)
    if not DIVINE.is_file():
        print(f"Unpacked, but {DIVINE} is missing.", file=sys.stderr)
        return 1
    print(f"Done: {DIVINE}\nDivine needs the .NET 8 runtime or newer (the server sets DOTNET_ROLL_FORWARD=Major).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
