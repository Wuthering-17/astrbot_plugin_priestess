"""Build a flat AstrBot installation ZIP from versioned and new source files."""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
from zipfile import ZipFile, ZIP_DEFLATED

REPO = Path(__file__).resolve().parents[1]


def build(output: Path) -> Path:
    version = re.search(r"^version:\s*(v\d+\.\d+\.\d+)\s*$", (REPO / "metadata.yaml").read_text("utf-8"), re.M)[1]
    files = subprocess.check_output(["git", "-c", f"safe.directory={REPO.as_posix()}", "ls-files", "-c", "-o", "--exclude-standard"], cwd=REPO, text=True).splitlines()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"astrbot_plugin_priestess_{version}.zip"
    with ZipFile(target, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(set(files)):
            if name.startswith((".github/", ".git/")) or not (REPO / name).is_file():
                continue
            if name.endswith(".py"):
                compile((REPO / name).read_bytes(), name, "exec")
            archive.write(REPO / name, name)
    with ZipFile(target) as archive:
        assert archive.testzip() is None
        assert {"main.py", "metadata.yaml", "LICENSE", "THIRD_PARTY_NOTICES.md"} <= set(archive.namelist())
        assert not any(name.startswith(("data/", ".env", "cache/", "backups/")) or "__pycache__" in name for name in archive.namelist())
    print(f"Built {target}: {target.stat().st_size} bytes")
    print(f"SHA256 {hashlib.sha256(target.read_bytes()).hexdigest()}")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=REPO.parent / "output")
    build(parser.parse_args().output)
