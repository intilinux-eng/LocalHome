"""Builds a release's files into one folder, dist/release/v<version>/: the
board's package (arm64), the Linux desktop app's package and, when it is
there, the Windows app's installer - with SHA256SUMS to check them.

    python tools/release.py            # on the tagged commit: git tag vX.Y.Z first

The board's package is built and tried on a clean Debian (build_deb.py
--check). The Windows installer is built on Windows (dotnet build -c Release
in desktop/, it lands in dist/LocalHome-Setup.exe); copied when present.
"""
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "([^"]+)"', text, re.M).group(1)


def main() -> int:
    v = version()
    tags = subprocess.run(["git", "-C", str(ROOT), "tag", "--points-at", "HEAD"], capture_output=True, text=True).stdout.split()
    if f"v{v}" not in tags:
        sys.exit(f"HEAD has no tag v{v}: tag the release first (git tag v{v})")
    folder = DIST / "release" / f"v{v}"
    folder.mkdir(parents=True, exist_ok=True)

    if subprocess.run([sys.executable, str(ROOT / "tools" / "build_deb.py"), "--check"]).returncode:
        return 1
    board = DIST / f"localhome_{v}_arm64.deb"
    if not board.exists():
        sys.exit(f"{board.name} missing: something left uncommitted gives a .dirty version")
    out = subprocess.run([sys.executable, str(ROOT / "desktop" / "linux" / "build_deb.py")],
                         capture_output=True, text=True, check=True).stdout
    desktop = Path(out.split(" (")[0].strip())

    files = [board, desktop]
    windows = DIST / "LocalHome-Setup.exe"
    if windows.exists():
        files.append(windows)
    else:
        print("no LocalHome-Setup.exe in dist/: build it on Windows, then run this again")
    for file in files:
        shutil.copy2(file, folder / file.name)
    sums = [f"{hashlib.sha256((folder / f.name).read_bytes()).hexdigest()}  {f.name}" for f in files]
    (folder / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    print(f"\n{folder}:")
    for f in sorted(folder.iterdir()):
        print(f"  {f.name}  ({f.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
