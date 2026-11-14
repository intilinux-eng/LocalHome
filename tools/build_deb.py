"""Builds LocalHome's Debian package on this PC: in Docker (on Linux, or under
WSL2 on Windows), for the board's processor (arm64, emulated by QEMU), LocalHome's own modules compiled
by Cython - see packaging/deb/. The package lands in dist/.

    python tools/build_deb.py                      # arm64, for a board
    python tools/build_deb.py --arch amd64 --test  # this PC's processor, then
                                                   # the unit tests against the compiled modules

Takes the files git knows, committed or not (a version built from changes not
committed says so: .dirty). Needs Docker: on Linux your user in the `docker`
group, on Windows WSL2 with Docker in it, run as root there. The first arm64
build registers QEMU with the kernel (until a reboot, or until WSL restarts).
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
WORK = DIST / "deb-build"
# Debian's image for each processor: one tag each, never mixed up in the cache.
BASE = {"arm64": "arm64v8/debian:trixie", "amd64": "amd64/debian:trixie"}
WINDOWS = os.name == "nt"


def host_path(path: Path) -> str:
    """The path as Docker sees it: as it is on Linux, under /mnt in WSL."""
    if not WINDOWS:
        return str(path.resolve())
    drive, rest = path.resolve().drive, path.resolve().as_posix()[2:]
    return f"/mnt/{drive[0].lower()}{rest}"


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    base = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
    commit = git("log", "-1", "--format=%h %cd", "--date=format:%Y%m%d%H%M").split()
    # Built from changes not committed: when, too - each such build is newer
    # than the last, so apt takes it as an update.
    dirty = f".dirty{datetime.datetime.now():%Y%m%d%H%M}" if git("status", "--porcelain").strip() else ""
    return f"{base}+git{commit[1]}.{commit[0]}{dirty}"


def source_tar() -> Path:
    """Every file git knows or would add, as it is on disk - never what
    .gitignore keeps out (config/'s real files, builds)."""
    names = [n for n in git("ls-files", "-co", "--exclude-standard", "-z").split("\0") if n]
    WORK.mkdir(parents=True, exist_ok=True)
    tar = WORK / "src.tar"
    with tarfile.open(tar, "w") as out:
        for name in names:
            path = ROOT / name
            if path.is_file() and not name.startswith(("dist/", "desktop/bin/", "desktop/obj/")):
                out.add(path, arcname=name)
    return tar


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--arch", choices=["arm64", "amd64"], default="arm64")
    parser.add_argument("--test", action="store_true", help="the unit tests against the compiled modules, before packaging")
    parser.add_argument("--check", action="store_true",
                        help="then install it on a clean Debian (a container), start it, update it, remove it")
    parser.add_argument("--check-only", action="store_true", help="only that, on the last package built")
    parser.add_argument("--distro", default="Ubuntu", help="the WSL distribution with Docker (Ubuntu)")
    args = parser.parse_args()

    # On Windows every command goes through WSL, as root.
    shell = ["wsl", "-d", args.distro, "-u", "root", "--"] if WINDOWS else []

    def docker(*cmd: str, **kw) -> subprocess.CompletedProcess:
        return subprocess.run([*shell, "docker", *cmd], **kw)

    def check(deb: Path) -> int:
        print(f"checking {deb.name} on a clean Debian")
        return docker("run", "--rm", "--platform", f"linux/{args.arch}", "-v", f"{host_path(DIST)}:/out:ro",
                      "-v", f"{host_path(ROOT / 'packaging' / 'deb')}:/check:ro", BASE[args.arch],
                      "sh", "/check/check_install.sh", f"/out/{deb.name}").returncode

    # arm64 runs through QEMU, registered with the kernel - gone at a reboot,
    # or whenever WSL stops (it does when left idle), so looked for before each use.
    if args.arch == "arm64":
        registered = subprocess.run([*shell, "test", "-e", "/proc/sys/fs/binfmt_misc/qemu-aarch64"]).returncode == 0
        if not registered:
            print("registering QEMU for arm64 (until a reboot)")
            if docker("run", "--privileged", "--rm", "tonistiigi/binfmt", "--install", "arm64",
                      stdout=subprocess.DEVNULL).returncode:
                return 1

    if args.check_only:
        built = sorted(DIST.glob(f"localhome_*_{args.arch}.deb"), key=lambda p: p.stat().st_mtime)
        if not built:
            raise SystemExit(f"no {args.arch} package in {DIST}")
        return check(built[-1])

    ver = version()
    print(f"LocalHome {ver}, {args.arch}")
    tar = source_tar()
    image = f"localhome-deb:{args.arch}"
    if docker("build", "-q", "--platform", f"linux/{args.arch}", "--build-arg", f"BASE={BASE[args.arch]}",
              "-t", image, host_path(ROOT / "packaging" / "deb")).returncode:
        return 1
    started = datetime.datetime.now()
    run = ["run", "--rm", "--platform", f"linux/{args.arch}",
           "-v", f"{host_path(tar.parent)}:/in:ro", "-v", f"{host_path(DIST)}:/out",
           "-v", f"localhome-pip-{args.arch}:/root/.cache/pip", image, ver]
    if args.test:
        run.append("--test")
    code = docker(*run).returncode
    print(f"{'done' if code == 0 else 'failed'} in {(datetime.datetime.now() - started).seconds // 60} min")
    tar.unlink(missing_ok=True)
    if code == 0 and args.check:
        code = check(DIST / f"localhome_{ver}_{args.arch}.deb")
    return code


if __name__ == "__main__":
    sys.exit(main())
