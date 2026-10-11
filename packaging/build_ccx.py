"""Build dist/ConsistentGrade.ccx: the UXP panel plus the bundled engine.

A .ccx is a ZIP with manifest.json at its root; double-clicking it installs the
plugin through the Creative Cloud desktop app.
Usage: python packaging/build_ccx.py
"""

import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "premiere-panel"
BUILD = ROOT / "build" / "ConsistentGrade"
DIST = ROOT / "dist"

ENGINE_REQUIREMENTS = "numpy>=1.26\nopencv-python-headless>=4.9\npillow>=10\n"


def main() -> Path:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    shutil.copytree(PANEL, BUILD, ignore=shutil.ignore_patterns(".DS_Store"))
    engine = BUILD / "engine"
    (engine / "autograde").mkdir(parents=True)
    files = []
    for src in sorted((ROOT / "autograde").glob("*.py")):
        if src.name == "service.py":  # HTTP service is not used by the panel
            continue
        shutil.copy2(src, engine / "autograde" / src.name)
        files.append(f"autograde/{src.name}")
    (engine / "requirements.txt").write_text(ENGINE_REQUIREMENTS)
    files.append("requirements.txt")
    (engine / "files.json").write_text(json.dumps(files, indent=1))
    shutil.copy2(ROOT / "packaging" / "start.command", engine / "start.command")

    version = json.loads((BUILD / "manifest.json").read_text())["version"]
    DIST.mkdir(exist_ok=True)
    out = DIST / "ConsistentGrade.ccx"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(BUILD.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(BUILD).as_posix())
    print(f"built {out} (v{version}, {out.stat().st_size // 1024} KB)")
    return out


if __name__ == "__main__":
    main()
