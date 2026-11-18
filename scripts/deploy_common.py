"""Shared by the deploy scripts: read .env, and stage the demo image's build context (code, demo trip, weights)."""
from __future__ import annotations

import json
import secrets
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = ROOT / "deploy" / "container"
# Plain settings worth carrying from .env to a deployment; anything else stays local
CARRY = ("ROADSENSE_CAMERA_HEIGHT_M", "ROADSENSE_CAMERA_HFOV_DEG", "ROADSENSE_CAMERA_PITCH_DEG",
         "ROADSENSE_DEDUPE_RADIUS_M", "ROADSENSE_EDGE_WEIGHTS", "ROADSENSE_VERIFY_WEIGHTS")
LLM_CARRY = ("ROADSENSE_LLM_MODELS", "ROADSENSE_LLM_RPM")
SECRET_KEYS = ("ROADSENSE_ADMIN_PASSWORD", "ROADSENSE_SESSION_SECRET", "ROADSENSE_MEDIA_SECRET", "ROADSENSE_API_TOKEN")


def read_env(path: Path = ROOT / ".env") -> dict:
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def weights_for(env: dict, weights_dir: Path | None) -> tuple[Path, list[str]]:
    folder = weights_dir or Path(env.get("ROADSENSE_WEIGHTS_DIR", ROOT / "weights"))
    return folder, [env.get("ROADSENSE_EDGE_WEIGHTS", "yolov8m_rdd2022.pt"),
                    env.get("ROADSENSE_VERIFY_WEIGHTS", "rtdetr_l_rdd2022.pt")]


def copy_lf(src: Path, dest: Path) -> None:
    """Copy with LF endings whatever the checkout did: `sh` in the Linux container fails on CRLF."""
    dest.write_text(src.read_text(encoding="utf-8").replace("\r\n", "\n"), encoding="utf-8", newline="\n")


def stage(dest: Path, weights_dir: Path, weights: list[str]) -> None:
    """The demo image's build context: code, keyless demo trip, the two detector weights, Dockerfile, start script."""
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for d in ("roadsense", "scripts", "demo"):
        shutil.copytree(ROOT / d, dest / d, ignore=skip)
    for f in ("requirements.lock", "pyproject.toml"):
        shutil.copy2(ROOT / f, dest / f)
    for f in ("Dockerfile", "start.sh"):
        copy_lf(CONTAINER / f, dest / f)
    (dest / "weights").mkdir()
    for w in weights:
        src = weights_dir / w
        if not src.is_file():
            raise SystemExit(f"Missing weights file {src}. Pass --weights-dir or set ROADSENSE_WEIGHTS_DIR in .env.")
        shutil.copy2(src, dest / "weights" / w)


def describe(dest: Path) -> str:
    files = [f for f in dest.rglob("*") if f.is_file()]
    return f"{len(files)} files, {sum(f.stat().st_size for f in files) / 1e6:.0f} MB"


def deployment_secrets(name: str) -> tuple[dict, Path]:
    """Admin password and keys, generated once per deployment and kept in ~/.roadsense/ (never printed)."""
    store = Path.home() / ".roadsense" / f"{name}.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    keep = json.loads(store.read_text(encoding="utf-8")) if store.is_file() else {}
    for k in SECRET_KEYS:
        keep.setdefault(k, secrets.token_urlsafe(18 if k == "ROADSENSE_ADMIN_PASSWORD" else 48))
    store.write_text(json.dumps(keep, indent=2), encoding="utf-8")
    return keep, store
