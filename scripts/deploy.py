"""Deploy a clean committed checkout; never copy account, state, keys or persona.

Stop the target bridge before running this script. Backups remain in its data dir.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


def deploy(target, entry_name):
    source = Path(__file__).resolve().parent
    if Path(entry_name).name != entry_name or not entry_name.endswith(".py"):
        raise ValueError("entry-name must be a Python filename")
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=source.parent, text=True)
    if dirty.strip():
        raise RuntimeError("Commit and review the checkout before deployment")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source.parent, text=True).strip()
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    data = target / "weixin_bridge"
    backup = data / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True)
    pairs = [(source / "wechat_bridge.py", target / entry_name),
             (source / "codex_ws_helper.mjs", target / "codex_ws_helper.mjs"),
             (source / "claude_helper.py", target / "claude_helper.py"),
             (source / "check_status.ps1", target / "check_status.ps1")]
    metadata = data / "build.json"
    if metadata.exists():
        shutil.copy2(metadata, backup / "build.json")
    for src, dst in pairs:
        if dst.exists():
            shutil.copy2(dst, backup / dst.name)
        shutil.copy2(src, dst)
        if src.read_bytes() != dst.read_bytes():
            raise RuntimeError(f"Deployment hash mismatch: {dst.name}; backup: {backup}")
    digest = hashlib.sha256((target / entry_name).read_bytes()).hexdigest()[:12]
    metadata.write_text(json.dumps({"commit": commit, "source": digest,
                                   "deployed_at": datetime.now().isoformat()}, indent=2), encoding="utf-8")
    print(f"Deployed {commit}; source={digest}; backup={backup}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-scripts", required=True, type=Path)
    parser.add_argument("--entry-name", default="wechat_bridge.py")
    args = parser.parse_args()
    deploy(args.target_scripts, args.entry_name)
