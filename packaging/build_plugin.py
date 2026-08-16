"""Build a .plugin bundle for Claude Desktop.

Two flavours:

  python packaging/build_plugin.py
      Portable. Launches the server with uvx straight from GitHub, so the
      bundle has no machine-specific paths and installs anywhere uv exists.

  python packaging/build_plugin.py --ref v0.1.0
      Same, pinned to a tag instead of tracking the default branch. Use this
      for anything attached to a release, so the artifact stays reproducible.

  python packaging/build_plugin.py --local
      Points at this checkout's virtualenv and source. Only works on this
      machine, but it runs your working tree, so local edits take effect
      without rebuilding.

Standard library only, so it runs with no dependencies installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "packaging" / "plugin"
DIST_DIR = REPO_ROOT / "dist"
GIT_URL = "git+https://github.com/Yuuzulight/Rozetta"


class BuildError(Exception):
    pass


def portable_mcp_config(ref: str | None) -> dict:
    source = f"{GIT_URL}@{ref}" if ref else GIT_URL
    return {
        "mcpServers": {
            "rozetta": {"command": "uvx", "args": ["--from", source, "rozetta"]}
        }
    }


def local_mcp_config() -> dict:
    python = REPO_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    entry = REPO_ROOT / "src" / "server.py"

    for path in (python, entry):
        if not path.exists():
            raise BuildError(
                f"{path} is missing. Create the virtualenv first:\n"
                "  python -m venv .venv\n"
                '  .venv/Scripts/python.exe -m pip install -e ".[dev]"'
            )

    return {
        "mcpServers": {
            "rozetta": {"command": str(python), "args": [str(entry)]}
        }
    }


def validate(manifest: dict, mcp: dict, local: bool) -> list[str]:
    problems = []

    name = manifest.get("name", "")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        problems.append(f"plugin name {name!r} is not kebab-case")
    if not re.fullmatch(r"\d+\.\d+\.\d+", manifest.get("version", "")):
        problems.append(f"version {manifest.get('version')!r} is not semver")

    server = mcp.get("mcpServers", {}).get("rozetta")
    if not server:
        problems.append(".mcp.json does not define a 'rozetta' server")
        return problems

    if not server.get("command"):
        problems.append("server has no command")

    if local:
        for path in [server["command"], *server.get("args", [])]:
            if not Path(path).exists():
                problems.append(f"path does not exist: {path}")
    else:
        # - A portable bundle must not carry anything machine-specific.
        for value in [server["command"], *server.get("args", [])]:
            if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("/"):
                problems.append(f"portable bundle contains an absolute path: {value}")

    return problems


def build(local: bool, ref: str | None) -> Path:
    manifest_path = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        raise BuildError(f"missing {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mcp = local_mcp_config() if local else portable_mcp_config(ref)

    problems = validate(manifest, mcp, local)
    if problems:
        raise BuildError("validation failed:\n  - " + "\n  - ".join(problems))

    DIST_DIR.mkdir(exist_ok=True)
    out = DIST_DIR / (f"{manifest['name']}-local.plugin" if local else f"{manifest['name']}.plugin")
    if out.exists():
        out.unlink()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        # .mcp.json is generated rather than copied, since it differs per flavour.
        bundle.writestr(".mcp.json", json.dumps(mcp, indent=2) + "\n")
        for file in sorted(PLUGIN_DIR.rglob("*")):
            if file.is_file() and file.name not in {".DS_Store", ".mcp.json"}:
                bundle.write(file, file.relative_to(PLUGIN_DIR).as_posix())

    with zipfile.ZipFile(out) as bundle:
        names = bundle.namelist()
        for required in (".claude-plugin/plugin.json", ".mcp.json"):
            if required not in names:
                raise BuildError(f"{required} missing from the archive")
            json.loads(bundle.read(required))

    print(f"built {out.relative_to(REPO_ROOT).as_posix()} ({out.stat().st_size} bytes)")
    print(f"  flavour: {'local' if local else 'portable'}")
    print(f"  command: {mcp['mcpServers']['rozetta']['command']}")
    print(f"  args:    {mcp['mcpServers']['rozetta']['args']}")
    print(f"  files:   {names}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="build a machine-specific bundle")
    parser.add_argument("--ref", help="git tag or commit to pin a portable bundle to")
    args = parser.parse_args()

    if args.local and args.ref:
        parser.error("--ref only applies to portable builds")

    try:
        build(local=args.local, ref=args.ref)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
