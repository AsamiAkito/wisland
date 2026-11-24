"""Build the Windows x64 installer with CPython 3.12 x64, uv and Inno Setup 6.

python packaging/build_installer.py --iscc C:\\tools\\Inno\\ISCC.exe --output dist
"""
import argparse
import hashlib
import mmap
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packaging"
EMBED_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
EMBED_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"


def prepare_runtime(runtime, embed_zip):
    if hashlib.sha256(embed_zip.read_bytes()).hexdigest() != EMBED_SHA256:
        raise ValueError("Unexpected CPython archive hash")
    with zipfile.ZipFile(embed_zip) as archive:
        archive.extractall(runtime)
    site = runtime / "Lib" / "site-packages"
    subprocess.run(["uv", "pip", "install", "--target", str(site), "--python", sys.executable,
                    "--link-mode", "copy", "--no-installer-metadata", "-r", str(ROOT / "requirements.txt"),
                    "-c", str(PACKAGE / "windows-x64-constraints.txt")], check=True)
    # uv's optional console entry points contain the build interpreter's absolute path.
    if (site / "bin").is_dir():
        shutil.rmtree(site / "bin")
    for cache in list(site.rglob("__pycache__")):
        shutil.rmtree(cache)
    for binary in (site / "shiboken6").glob("*140*.dll"):
        shutil.copyfile(binary, runtime / binary.name)
    (runtime / "python312._pth").write_text("python312.zip\n.\nLib/site-packages\n..\n", encoding="ascii")


def copy_application(payload):
    sources = [ROOT / file for file in ("run.pyw", "LICENSE", "README.md", "config.example.json",
                                         "scripts/download_models.py", "wisland/live/LICENSE-YishengTranslator.txt")]
    sources.extend((ROOT / "wisland").rglob("*.py"))
    for source in sources:
        dest = payload / source.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)


def check_payload(payload):
    roots = {str(ROOT), str(Path.home())}
    needles = {variant.encode(encoding) for path in roots for variant in (path, path.replace("\\", "/"))
               for encoding in ("utf-8", "utf-16-le")}
    for file in payload.rglob("*"):
        if not file.is_file() or not file.stat().st_size:
            continue
        with file.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as contents:
            if any(contents.find(needle) >= 0 for needle in needles):
                raise ValueError(f"Local build path in {file.relative_to(payload)}")


def write_cache_cleanup(payload, rules):
    # A user can also run the bundled interpreter directly, without the shortcut's -B.
    # List only cache directories belonging to shipped modules, then remove empty parents.
    python_dirs = {p.parent.relative_to(payload) for p in payload.rglob("*.py")}
    caches = {directory / "__pycache__" for directory in python_dirs}
    caches.add(Path("runtime/Lib/site-packages/comtypes/gen"))
    parents = {parent for path in caches for parent in path.parents if parent != Path(".")}
    lines = [f'Type: filesandordirs; Name: "{{app}}\\{path}"' for path in sorted(caches)]
    lines.extend(f'Type: dirifempty; Name: "{{app}}\\{path}"'
                 for path in sorted(parents, key=lambda p: (-len(p.parts), str(p))))
    rules.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="1.3.0")
    parser.add_argument("--iscc", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--embed-zip", type=Path, help="Use an already-downloaded official archive")
    args = parser.parse_args()
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        parser.error("Build on Windows x64 with a Python 3.12 x64 interpreter")
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        parser.error("Expected a three-part version number")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wisland-build-") as scratch:
        work = Path(scratch)
        payload = work / "payload"
        embed_zip = args.embed_zip
        if not embed_zip:
            embed_zip = work / "python.zip"
            urllib.request.urlretrieve(EMBED_URL, embed_zip)
        prepare_runtime(payload / "runtime", embed_zip)
        copy_application(payload)
        check_payload(payload)
        cleanup = work / "cleanup.iss"
        write_cache_cleanup(payload, cleanup)
        subprocess.run([str(args.iscc.resolve()), f"/DAppVersion={args.version}", f"/DPayload={payload}",
                        f"/DOutputDir={args.output.resolve()}", f"/DCleanupRules={cleanup}",
                        str(PACKAGE / "windows.iss")], check=True)


if __name__ == "__main__":
    main()
