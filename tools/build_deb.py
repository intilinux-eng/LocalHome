"""Builds LocalHome's .deb for the Arduino UNO Q from this PC: the
program compiled by Nuitka, in an arm64 Debian trixie machine that
Docker runs under QEMU (packaging/Dockerfile, packaging/build.sh).

    python tools/build_deb.py              # the current commit -> dist/localhome_<version>_arm64.deb
    python tools/build_deb.py --ref main   # another commit, branch or tag
    python tools/build_deb.py --check      # say what would happen, build nothing

Committed work only, like tools/deploy_board.py: the commit is exported
with git archive, never what's uncommitted here. The version says which
commit and when: 0.1.0+git202610121530.abc1234 - each later commit a
higher version, so apt sees an update.

Docker: `docker` on the PATH (Docker Desktop), or Docker inside WSL2 -
then each command goes through `wsl docker`, the paths translated. The
first build installs QEMU's arm64 emulation if missing and makes the
build machine's image (minutes); Nuitka's and ccache's caches stay in
the localhome-deb-cache volume, so later builds compile only what
changed. Under emulation a first build takes long - an hour or more.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "build" / "deb"
DIST = ROOT / "dist"
IMAGE = "localhome-deb-builder"
CACHE_VOLUME = "localhome-deb-cache"
PLATFORM = "linux/arm64"


def git(*args: str) -> str:
    result = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=ROOT, capture_output=True)
    if result.returncode:
        raise SystemExit(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8", errors="replace").strip() if args[0] != "archive" else result.stdout


def version_of(base: str, committed: str, sha: str) -> str:
    """0.1.0 + the commit's time (YYYYMMDDHHMM) and short hash: later
    commits sort higher for dpkg."""
    return f"{base}+git{committed}.{sha}"


def docker_command(which=shutil.which) -> list[str]:
    """How to call Docker from here: its own command, or through WSL."""
    if which("docker"):
        return ["docker"]
    if which("wsl"):
        return ["wsl", "docker"]
    raise SystemExit("No Docker: install Docker Desktop, or Docker in WSL2 (see docs/deployment.md)")


def docker_path(path: Path, docker: list[str]) -> str:
    """A folder of this PC as Docker sees it: through WSL, /mnt/c/..."""
    if docker[0] != "wsl":
        return str(path)
    out = subprocess.run(["wsl", "wslpath", "-a", str(path).replace("\\", "/")], capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"wslpath {path}: {out.stderr.strip()}")
    return out.stdout.strip()


def run(command: list[str], check: bool = True) -> int:
    print("  $", " ".join(command), flush=True)
    code = subprocess.run(command).returncode
    if check and code:
        raise SystemExit(f"failed ({code}): {' '.join(command)}")
    return code


def export(ref: str, target: Path) -> None:
    """The commit's files, as committed."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with tarfile.open(fileobj=BytesIO(git("archive", "--format=tar", ref))) as tar:
        tar.extractall(target, filter="data")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ref", default="HEAD", help="the commit, branch or tag to build (default: the current commit)")
    parser.add_argument("--check", action="store_true", help="say what would happen, build nothing")
    args = parser.parse_args(argv)

    sha = git("rev-parse", "--short", args.ref)
    committed = git("log", "-1", "--format=%cd", "--date=format-local:%Y%m%d%H%M", args.ref)
    date = git("log", "-1", "--format=%cI", args.ref)
    base = tomllib.loads(git("show", f"{args.ref}:pyproject.toml"))["project"]["version"]
    version = version_of(base, committed, sha)
    if git("status", "--porcelain", "--untracked-files=no") and args.ref == "HEAD":
        print("Note: uncommitted changes here are not in the package - only the commit is.")
    docker = docker_command()
    deb = DIST / f"localhome_{version}_arm64.deb"
    print(f"LocalHome {version} ({args.ref}) -> {deb.relative_to(ROOT)}, with {' '.join(docker)}")
    if args.check:
        return

    start = time.time()
    source = WORK / "src"
    export(args.ref, source)
    DIST.mkdir(exist_ok=True)
    print("== arm64 emulation")
    if run([*docker, "run", "--rm", "--platform", PLATFORM, "debian:trixie", "true"], check=False):
        run([*docker, "run", "--privileged", "--rm", "tonistiigi/binfmt", "--install", "arm64"])
    print("== The build machine (cached once made)")
    src = docker_path(source, docker)
    run([*docker, "build", "--platform", PLATFORM, "-t", IMAGE, "-f", f"{src}/packaging/Dockerfile", src])
    print("== Compile, test, package")
    run([*docker, "run", "--rm", "--platform", PLATFORM,
         "-v", f"{src}:/src:ro", "-v", f"{docker_path(DIST, docker)}:/out", "-v", f"{CACHE_VOLUME}:/cache",
         IMAGE, "sh", "/src/packaging/build.sh", version, sha, date])
    minutes = (time.time() - start) / 60
    print(f"Done in {minutes:.0f} min: {deb.relative_to(ROOT)} ({deb.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    sys.exit(main())
