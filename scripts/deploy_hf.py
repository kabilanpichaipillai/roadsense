"""Deploy the RoadSense demo to a Hugging Face Docker Space.

Note: Docker Spaces on Hugging Face's free CPU hardware need a PRO subscription. For a free option use
scripts/deploy_gcp.py with Google Cloud's trial credit.

    pip install huggingface_hub
    hf auth login                      # once, with a token that has write access
    python scripts/deploy_hf.py        # private Space <you>/roadsense, rule engine only
    python scripts/deploy_hf.py --with-llm --public

What it does: stages the code, the keyless demo trip and the two detector weights with deploy/container/ and the
Space card, creates the Space if needed, sets its secrets and variables, and uploads. Hugging Face then builds the
image (about 10 minutes). Secrets are generated once and kept in ~/.roadsense/; nothing secret is printed. The demo
runs on sample data with a mock Halifax 311 inside the container; Spaces are not in Canada, so no real municipal data.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_common import CARRY, LLM_CARRY, ROOT, copy_lf, deployment_secrets, describe, read_env, stage, weights_for  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--space", default="roadsense", help="Space name (default: roadsense)")
    p.add_argument("--public", action="store_true", help="make the Space public (default: private)")
    p.add_argument("--with-llm", action="store_true",
                   help="copy OPENROUTER_API_KEY from .env so the AI agents run (uses your OpenRouter quota)")
    p.add_argument("--weights-dir", type=Path, help="folder with the fine-tuned .pt files (default: from .env)")
    p.add_argument("--dry-run", action="store_true", help="stage the files and stop; nothing is uploaded")
    args = p.parse_args()

    env = read_env()
    weights_dir, weights = weights_for(env, args.weights_dir)
    with tempfile.TemporaryDirectory(prefix="roadsense-space-") as tmp:
        dest = Path(tmp)
        stage(dest, weights_dir, weights)
        copy_lf(ROOT / "deploy" / "huggingface" / "README.md", dest / "README.md")
        print(f"Staged {describe(dest)}")
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

        keep, store = deployment_secrets(f"hf_space_{owner}_{args.space}")
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
