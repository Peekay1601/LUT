import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ccx_layout():
    subprocess.run([sys.executable, str(ROOT / "packaging" / "build_ccx.py")], check=True, capture_output=True)
    ccx = ROOT / "dist" / "ConsistentGrade.ccx"
    with zipfile.ZipFile(ccx) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json"))  # must be at the root of the archive
        files = json.loads(z.read("engine/files.json"))
        start = z.read("engine/start.command").decode()
    assert manifest["host"] == {"app": "premierepro", "minVersion": "25.6.0"}  # single host object for a .ccx
    perms = manifest["requiredPermissions"]
    assert perms["network"]["domains"] == ["https://api.anthropic.com"]  # no http:// (blocked on macOS)
    assert perms["localFileSystem"] == "fullAccess"
    assert ".command" in perms["launchProcess"]["extensions"]
    for icon in manifest["entrypoints"][0]["icons"]:
        assert icon["path"] in names and icon["path"].replace(".png", "@2x.png") in names
    assert {"index.html", "main.js", "styles.css"} <= names
    assert all(f"engine/{f}" in names for f in files)
    assert "autograde/worker.py" in files and "autograde/service.py" not in files
    assert start.startswith("#!/bin/bash") and "autograde worker" in start
    # Every file the panel reads from plugin:/ must be in the package.
    src = (ROOT / "premiere-panel" / "main.js").read_text()
    for ref in ("plugin:/engine/files.json", "plugin:/engine/start.command"):
        assert ref in src and ref.replace("plugin:/", "") in names
