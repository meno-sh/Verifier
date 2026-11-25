#!/usr/bin/env python3
"""Check which OpenCode build install.sh picks for each machine, and that it refuses a bad download.

Runs install.sh with VERIFIER_INSTALL_DRY_RUN=1 and a stand-in platform. Every run has a
temporary HOME and a fake curl and uv first on the PATH: curl writes a file of the wrong
content, and uv fails, so nothing is downloaded or installed even if install.sh ignored
the dry run. The bad-download cases check that install.sh stops before it unpacks
anything, on macOS and on Linux. No network. Returns 1 if any case fails.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
FAILED: list[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


FAKE_CURL = """#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then echo "not opencode" > "$2"; exit 0; fi
  shift
done
exit 1
"""
FAKE_UV = """#!/usr/bin/env bash
echo "fake uv: refused" >&2
exit 97
"""


def install(env: dict[str, str], home: Path | None = None) -> subprocess.CompletedProcess:
    """Run install.sh with these variables, a temporary HOME, and the fake curl and uv."""
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        for name, text in (("curl", FAKE_CURL), ("uv", FAKE_UV)):
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        if home is None:
            home = Path(tmp) / "home"
            home.mkdir()
        return subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True,
                              env={**os.environ, "HOME": str(home),
                                   "PATH": f"{bin_dir}:{os.environ['PATH']}", **env},
                              timeout=60)


def test_builds() -> None:
    """The build for each machine: Linux tar files and macOS zip files, each with its SHA-256."""
    for platform, avx2, want in (
            ("Darwin-arm64", "", "opencode-darwin-arm64.zip sha256 0d5f5119"),
            ("Darwin-x86_64", "yes", "opencode-darwin-x64.zip sha256 515380b0"),
            ("Darwin-x86_64", "no", "opencode-darwin-x64-baseline.zip sha256 e96a33a5"),
            ("Linux-x86_64", "", "opencode-linux-x64.tar.gz sha256 7bb9e61a"),
            ("Linux-aarch64", "", "opencode-linux-arm64.tar.gz sha256 e9841d43")):
        done = install({"VERIFIER_INSTALL_DRY_RUN": "1", "VERIFIER_PLATFORM": platform,
                        "VERIFIER_AVX2": avx2})
        expect(f"{platform}{' AVX2 ' + avx2 if avx2 else ''}: {want.split()[0]}",
               done.returncode == 0 and f"opencode: {want}" in done.stdout,
               done.stdout + done.stderr)
    done = install({"VERIFIER_INSTALL_DRY_RUN": "1", "VERIFIER_PLATFORM": "Windows-x86_64"})
    expect("a machine with no pinned build is refused, with its name",
           done.returncode == 1 and "no pinned OpenCode build for Windows-x86_64" in done.stderr,
           done.stderr)


def test_bad_download() -> None:
    """A download whose SHA-256 is not the pinned one is refused, and nothing is installed."""
    for platform in ("Darwin-arm64", "Linux-x86_64"):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            done = install({"VERIFIER_PLATFORM": platform}, home)
            expect(f"{platform}: install.sh stops on a download with the wrong SHA-256",
                   done.returncode == 1 and "does not have the pinned SHA-256" in done.stderr,
                   done.stdout + done.stderr)
            expect(f"{platform}: and installs no OpenCode",
                   not list(home.glob(".local/share/verifier/opencode-*/opencode")))


def main() -> int:
    """Run every case and report."""
    for test in (test_builds, test_bad_download):
        print(test.__doc__.splitlines()[0])
        test()
    print("all passed" if not FAILED else f"{len(FAILED)} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
