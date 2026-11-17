"""Deploy the RoadSense demo to a Hugging Face Docker Space (free CPU hardware).

    pip install huggingface_hub
    hf auth login                      # once, with a token that has write access
    python scripts/deploy_hf.py        # private Space <you>/roadsense, rule engine only
    python scripts/deploy_hf.py --with-llm --public

What it does: stages the code, the keyless demo trip and the two detector weights with deploy/huggingface/, creates
the Space if needed, sets its secrets and variables, and uploads. Hugging Face then builds the image (about 10 minutes).

Secrets (admin password, session, media and API keys) are generated once and kept in ~/.roadsense/ so a redeploy keeps
the same admin password. Nothing secret is printed. The demo runs on sample data with a mock Halifax 311 inside the
container; free Spaces are not in Canada, so do not put real municipal data on one.
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPACE_FILES = ROOT / "deploy" / "huggingface"
# Plain settings worth carrying from .env; anything else stays local
CARRY = ("ROADSENSE_CAMERA_HEIGHT_M", "ROADSENSE_CAMERA_HFOV_DEG", "ROADSENSE_CAMERA_PITCH_DEG",
         "ROADSENSE_DEDUPE_RADIUS_M", "ROADSENSE_EDGE_WEIGHTS", "ROADSENSE_VERIFY_WEIGHTS")
LLM_CARRY = ("ROADSENSE_LLM_MODELS", "ROADSENSE_LLM_RPM")


def read_env(path: Path) -> dict:
    out = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def stage(dest: Path, weights_dir: Path, weights: list[str]) -> None:
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for d in ("roadsense", "scripts", "demo"):
        shutil.copytree(ROOT / d, dest / d, ignore=skip)
    for f in ("requirements.lock", "pyproject.toml"):
        shutil.copy2(ROOT / f, dest / f)
    for f in ("Dockerfile", "start.sh", "README.md"):
        # LF endings whatever the checkout did: `sh` in the Linux container fails on CRLF
        text = (SPACE_FILES / f).read_text(encoding="utf-8").replace("\r\n", "\n")
        (dest / f).write_text(text, encoding="utf-8", newline="\n")
    (dest / "weights").mkdir()
    for w in weights:
        src = weights_dir / w
        if not src.is_file():
            raise SystemExit(f"Missing weights file {src}. Pass --weights-dir or set ROADSENSE_WEIGHTS_DIR in .env.")
        shutil.copy2(src, dest / "weights" / w)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--space", default="roadsense", help="Space name (default: roadsense)")
    p.add_argument("--public", action="store_true", help="make the Space public (default: private)")
    p.add_argument("--with-llm", action="store_true",
                   help="copy OPENROUTER_API_KEY from .env so the AI agents run (uses your OpenRouter quota)")
    p.add_argument("--weights-dir", type=Path, help="folder with the fine-tuned .pt files (default: from .env)")
    p.add_argument("--dry-run", action="store_true", help="stage the files and stop; nothing is uploaded")
    args = p.parse_args()

    env = read_env(ROOT / ".env")
    weights_dir = args.weights_dir or Path(env.get("ROADSENSE_WEIGHTS_DIR", ROOT / "weights"))
    weights = [env.get("ROADSENSE_EDGE_WEIGHTS", "yolov8m_rdd2022.pt"),
               env.get("ROADSENSE_VERIFY_WEIGHTS", "rtdetr_l_rdd2022.pt")]

    with tempfile.TemporaryDirectory(prefix="roadsense-space-") as tmp:
        dest = Path(tmp)
        stage(dest, weights_dir, weights)
        size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        print(f"Staged {sum(1 for f in dest.rglob('*') if f.is_file())} files, {size / 1e6:.0f} MB")
        if args.dry_run:
            print("Dry run: nothing uploaded.")
            return

        try:
            from huggingface_hub import HfApi
        except ImportError:
            raise SystemExit("Install the uploader first: pip install huggingface_hub") from None
        api = HfApi()
        try:
            owner = api.whoami()["name"]
        except Exception:
            raise SystemExit("Not signed in to Hugging Face. Run `hf auth login` with a write token, then retry.") from None
        repo_id = f"{owner}/{args.space}"
        api.create_repo(repo_id, repo_type="space", space_sdk="docker", private=not args.public, exist_ok=True)
        host = f"https://{repo_id.replace('/', '-').replace('_', '-').replace('.', '-').lower()}.hf.space"

        # secrets: generated once, reused on every redeploy
        store = Path.home() / ".roadsense" / f"hf_space_{owner}_{args.space}.json"
        store.parent.mkdir(parents=True, exist_ok=True)
        keep = json.loads(store.read_text(encoding="utf-8")) if store.is_file() else {}
        for k in ("ROADSENSE_ADMIN_PASSWORD", "ROADSENSE_SESSION_SECRET", "ROADSENSE_MEDIA_SECRET",
                  "ROADSENSE_API_TOKEN"):
            keep.setdefault(k, secrets.token_urlsafe(18 if k == "ROADSENSE_ADMIN_PASSWORD" else 48))
        store.write_text(json.dumps(keep, indent=2), encoding="utf-8")
        for k, v in keep.items():
            api.add_space_secret(repo_id, k, v)

        variables = {"ROADSENSE_PUBLIC_URL": host, "ROADSENSE_ADMIN_EMAIL": "admin@roadsense.local",
                     **{k: env[k] for k in CARRY if env.get(k)}}
        if args.with_llm:
            if not env.get("OPENROUTER_API_KEY"):
                raise SystemExit("--with-llm needs OPENROUTER_API_KEY in .env")
            api.add_space_secret(repo_id, "OPENROUTER_API_KEY", env["OPENROUTER_API_KEY"])
            variables.update({"ROADSENSE_USE_LLM": "1", "ROADSENSE_LLM_MODE": "cache",
                              **{k: env[k] for k in LLM_CARRY if env.get(k)}})
        else:
            variables["ROADSENSE_USE_LLM"] = "0"
            try:
                api.delete_space_secret(repo_id, "OPENROUTER_API_KEY")
            except Exception:
                pass
        for k, v in variables.items():
            api.add_space_variable(repo_id, k, v)

        print(f"Uploading to {repo_id} ({'public' if args.public else 'private'})...")
        api.upload_folder(repo_id=repo_id, repo_type="space", folder_path=str(dest),
                          commit_message="Deploy RoadSense demo")

    print("\nDeployed. Hugging Face is building the image now (about 10 minutes).")
    print(f"  Build logs: https://huggingface.co/spaces/{repo_id}")
    print(f"  App:        {host}")
    print(f"  Sign in:    admin@roadsense.local, password in {store} (ROADSENSE_ADMIN_PASSWORD)")


if __name__ == "__main__":
    sys.exit(main())
