"""Deploy the RoadSense demo to one Google Cloud VM (Toronto by default), with HTTPS, using the trial credit.

    # once: install the Google Cloud CLI, then
    gcloud auth login
    gcloud config set project <your-project-id>     # a project linked to your trial billing account
    python scripts/deploy_gcp.py                    # rule engine only
    python scripts/deploy_gcp.py --with-llm         # AI agents on your OpenRouter key (uses its quota)

What it does, each step skipped when already done:
  1. enables the Compute Engine API, reserves a static IP and opens ports 80/443 to VMs tagged roadsense-web
  2. creates an e2-standard-2 Ubuntu VM (2 vCPU, 8 GB, 30 GB disk) that installs Docker on boot
  3. stages the code, the keyless demo trip, the detector weights, deploy/gcp/ and a generated .env, copies them to
     /opt/roadsense and runs `docker compose up -d --build` (the first build takes about 10 minutes)
  4. Caddy fetches a Let's Encrypt certificate for <ip>.sslip.io, so no domain is needed

Re-run it to redeploy: the data volume (reports, photos) is kept. Secrets are generated once and kept in
~/.roadsense/; nothing secret is printed. Cost on list prices: about US$0.08/hour while the VM runs, paid from the
trial credit. To stop paying entirely: python scripts/deploy_gcp.py --delete
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_common import CARRY, LLM_CARRY, ROOT, copy_lf, deployment_secrets, describe, read_env, stage, weights_for  # noqa: E402

GCP_FILES = ROOT / "deploy" / "gcp"
TAG = "roadsense-web"


class GCloud:
    def __init__(self, project: str, zone: str):
        exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        if not exe:
            raise SystemExit("The Google Cloud CLI is not installed: https://cloud.google.com/sdk/docs/install")
        self.exe, self.project, self.zone, self.region = exe, project, zone, zone.rsplit("-", 1)[0]

    def __call__(self, *args: str, check: bool = True) -> str:
        """Run gcloud; return stdout, or "" when it failed and check is off (used for "does this exist?")."""
        r = subprocess.run([self.exe, *args, "--project", self.project, "--quiet"], capture_output=True, text=True)
        if check and r.returncode:
            raise SystemExit(f"gcloud {' '.join(args[:3])} failed:\n{r.stderr.strip()[-1500:]}")
        return r.stdout.strip() if r.returncode == 0 else ""

    def ssh(self, name: str, command: str, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run([self.exe, "compute", "ssh", name, "--zone", self.zone, "--project", self.project, "--quiet",
                            "--strict-host-key-checking=no", "--command", command], capture_output=True, text=True)
        if check and r.returncode:
            raise SystemExit(f"ssh `{command[:60]}` failed:\n{(r.stderr or r.stdout).strip()[-1500:]}")
        return r


def current_project() -> str:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        return ""
    r = subprocess.run([exe, "config", "get-value", "project"], capture_output=True, text=True)
    return r.stdout.strip()


def ensure_infra(g: GCloud, name: str, machine: str) -> str:
    print("Enabling the Compute Engine API (first time takes a minute)...")
    g("services", "enable", "compute.googleapis.com")

    ip = g("compute", "addresses", "describe", f"{name}-ip", "--region", g.region, "--format=value(address)",
           check=False)
    if not ip:
        print("Reserving a static IP...")
        g("compute", "addresses", "create", f"{name}-ip", "--region", g.region)
        ip = g("compute", "addresses", "describe", f"{name}-ip", "--region", g.region, "--format=value(address)")

    if not g("compute", "firewall-rules", "describe", TAG, "--format=value(name)", check=False):
        print("Opening ports 80 and 443...")
        g("compute", "firewall-rules", "create", TAG, "--network", "default", "--allow", "tcp:80,tcp:443",
          "--target-tags", TAG, "--description", "RoadSense web (Caddy)")

    if not g("compute", "instances", "describe", name, "--zone", g.zone, "--format=value(name)", check=False):
        print(f"Creating the VM {name} ({machine}, {g.zone})...")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "startup.sh"
            copy_lf(GCP_FILES / "startup.sh", script)
            g("compute", "instances", "create", name, "--zone", g.zone, "--machine-type", machine,
              "--image-family", "ubuntu-2404-lts-amd64", "--image-project", "ubuntu-os-cloud",
              "--boot-disk-size", "30GB", "--boot-disk-type", "pd-balanced", "--address", ip, "--tags", TAG,
              "--metadata-from-file", f"startup-script={script}")
    return ip


def wait_for_docker(g: GCloud, name: str) -> None:
    print("Waiting for the VM to install Docker...", end="", flush=True)
    for _ in range(60):
        if g.ssh(name, "sudo docker compose version", check=False).returncode == 0:
            print(" ready")
            return
        print(".", end="", flush=True)
        time.sleep(10)
    raise SystemExit("\nDocker did not come up in 10 minutes; check the VM's serial console in the Cloud console.")


def env_file(env: dict, host: str, keep: dict, with_llm: bool) -> str:
    lines = {"SITE_HOST": host, "ROADSENSE_PUBLIC_URL": f"https://{host}", "ROADSENSE_ADMIN_EMAIL": "admin@roadsense.local",
             **keep, **{k: env[k] for k in CARRY if env.get(k)}}
    if with_llm:
        if not env.get("OPENROUTER_API_KEY"):
            raise SystemExit("--with-llm needs OPENROUTER_API_KEY in .env")
        lines.update({"OPENROUTER_API_KEY": env["OPENROUTER_API_KEY"], "ROADSENSE_USE_LLM": "1",
                      "ROADSENSE_LLM_MODE": "cache", **{k: env[k] for k in LLM_CARRY if env.get(k)}})
    else:
        lines["ROADSENSE_USE_LLM"] = "0"
    return "".join(f"{k}={v}\n" for k, v in lines.items())


def wait_for_https(url: str) -> bool:
    print(f"Waiting for {url} (certificate and app start)...", end="", flush=True)
    for _ in range(60):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=10) as r:
                if r.status == 200:
                    print(" up")
                    return True
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(10)
    print()
    return False


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=current_project(), help="Google Cloud project id (default: gcloud config)")
    p.add_argument("--zone", default="northamerica-northeast2-a", help="default: Toronto")
    p.add_argument("--machine", default="e2-standard-2")
    p.add_argument("--name", default="roadsense")
    p.add_argument("--with-llm", action="store_true", help="run the AI agents on OPENROUTER_API_KEY from .env")
    p.add_argument("--weights-dir", type=Path, help="folder with the fine-tuned .pt files (default: from .env)")
    p.add_argument("--dry-run", action="store_true", help="stage the bundle and stop; nothing touches Google Cloud")
    p.add_argument("--delete", action="store_true", help="delete the VM, its disk, the static IP and the firewall rule")
    args = p.parse_args()

    env = read_env()
    weights_dir, weights = weights_for(env, args.weights_dir)
    with tempfile.TemporaryDirectory(prefix="roadsense-gcp-") as tmp:
        bundle = Path(tmp) / "bundle"
        bundle.mkdir()
        stage(bundle, weights_dir, weights)
        for f in ("docker-compose.yml", "Caddyfile"):
            copy_lf(GCP_FILES / f, bundle / f)
        print(f"Staged {describe(bundle)}")
        if args.dry_run:
            print("Dry run: nothing sent to Google Cloud.")
            return
        if not args.project:
            raise SystemExit("No project: run `gcloud config set project <id>` or pass --project.")
        g = GCloud(args.project, args.zone)

        if args.delete:
            print(f"Deleting {args.name} and its IP and firewall rule in {args.project}...")
            g("compute", "instances", "delete", args.name, "--zone", g.zone, "--delete-disks=all", check=False)
            g("compute", "addresses", "delete", f"{args.name}-ip", "--region", g.region, check=False)
            g("compute", "firewall-rules", "delete", TAG, check=False)
            print("Deleted. Nothing is left running in this project from RoadSense.")
            return

        ip = ensure_infra(g, args.name, args.machine)
        host = f"{ip.replace('.', '-')}.sslip.io"
        keep, store = deployment_secrets(f"gcp_{args.project}_{args.name}")
        (bundle / ".env").write_text(env_file(env, host, keep, args.with_llm), encoding="utf-8", newline="\n")

        archive = Path(tmp) / "roadsense.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            for item in bundle.iterdir():
                tar.add(item, arcname=item.name)
        wait_for_docker(g, args.name)

        print("Copying the bundle to the VM...")
        r = subprocess.run([g.exe, "compute", "scp", str(archive), f"{args.name}:roadsense.tgz", "--zone", g.zone,
                            "--project", g.project, "--quiet", "--strict-host-key-checking=no"],
                           capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"Copy failed:\n{r.stderr.strip()[-1500:]}")

    print("Building and starting the containers (first build about 10 minutes)...")
    g.ssh(args.name, "set -e; sudo mkdir -p /opt/roadsense && cd /opt/roadsense"
                     " && sudo rm -rf roadsense scripts demo weights"
                     " && sudo tar -xzf ~/roadsense.tgz -C /opt/roadsense && rm ~/roadsense.tgz"
                     " && sudo chmod 600 .env && sudo docker compose up -d --build 2>&1 | tail -5")

    url = f"https://{host}"
    ok = wait_for_https(url)
    print(f"\n{'Deployed' if ok else 'Deployed, but not answering yet: give it a few minutes'}.")
    print(f"  App:      {url}")
    print(f"  Sign in:  admin@roadsense.local, password in {store} (ROADSENSE_ADMIN_PASSWORD)")
    print(f"  Logs:     gcloud compute ssh {args.name} --zone {g.zone} --command "
          f"\"cd /opt/roadsense && sudo docker compose logs --tail 50\"")
    print(f"  Remove:   python scripts/deploy_gcp.py --delete --project {args.project}")


if __name__ == "__main__":
    sys.exit(main())
