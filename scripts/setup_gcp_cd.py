"""One-time setup so GitHub Actions can deploy Dev to the RoadSense VM, with no stored keys.

    python scripts/setup_gcp_cd.py                   # project from `gcloud config`, repo rhaularj/roadsense
    python scripts/setup_gcp_cd.py --github-vars     # also writes the repository variables with `gh`

Each step is skipped when already done:
  1. enables the IAM, IAM Credentials, Security Token Service and OS Login APIs
  2. creates a deploy-only service account (roadsense-deployer) that can do three things: read Compute Engine
     resources, log in to the one VM with sudo through OS Login, and act as that VM's service account (needed for an
     OS Login session). It cannot create, delete or change VMs, firewalls or IAM.
  3. turns on OS Login for the VM, so SSH access follows IAM instead of keys in metadata
  4. creates a Workload Identity pool and GitHub provider that accept only tokens from this repository's Dev branch,
     and lets those tokens act as the deploy account

Then .github/workflows/cd.yml can sign in with google-github-actions/auth. Undo with --delete.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

POOL, PROVIDER, SA_NAME = "github", "github-oidc", "roadsense-deployer"


class G:
    def __init__(self, project: str):
        self.exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        if not self.exe:
            raise SystemExit("The Google Cloud CLI is not installed.")
        self.project = project

    def __call__(self, *args: str, check: bool = True) -> str:
        r = subprocess.run([self.exe, *args, "--project", self.project, "--quiet"], capture_output=True, text=True)
        if check and r.returncode:
            raise SystemExit(f"gcloud {' '.join(args[:4])} failed:\n{r.stderr.strip()[-1200:]}")
        return r.stdout.strip() if r.returncode == 0 else ""


def default_project() -> str:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    return subprocess.run([exe, "config", "get-value", "project"], capture_output=True, text=True).stdout.strip() if exe else ""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=default_project())
    p.add_argument("--repo", default="rhaularj/roadsense")
    p.add_argument("--branch", default="Dev")
    p.add_argument("--vm", default="roadsense")
    p.add_argument("--zone", default="northamerica-northeast2-a")
    p.add_argument("--url", default="", help="the app's https address (default: from the VM's IP)")
    p.add_argument("--github-vars", action="store_true", help="write the repository variables with `gh`")
    p.add_argument("--delete", action="store_true", help="remove the provider, pool, service account and OS Login")
    args = p.parse_args()
    if not args.project:
        raise SystemExit("No project: run `gcloud config set project <id>` or pass --project.")
    g = G(args.project)
    number = g("projects", "describe", args.project, "--format=value(projectNumber)")
    sa = f"{SA_NAME}@{args.project}.iam.gserviceaccount.com"
    pool_id = f"projects/{number}/locations/global/workloadIdentityPools/{POOL}"

    if args.delete:
        g("iam", "workload-identity-pools", "providers", "delete", PROVIDER, "--location=global",
          f"--workload-identity-pool={POOL}", check=False)
        g("iam", "workload-identity-pools", "delete", POOL, "--location=global", check=False)
        g("iam", "service-accounts", "delete", sa, check=False)
        g("compute", "instances", "remove-metadata", args.vm, "--zone", args.zone, "--keys=enable-oslogin", check=False)
        print("Removed the deploy account, the GitHub trust and OS Login on the VM.")
        return

    print("Enabling IAM, IAM Credentials, STS and OS Login...")
    g("services", "enable", "iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com",
      "oslogin.googleapis.com")

    if not g("iam", "service-accounts", "describe", sa, "--format=value(email)", check=False):
        print(f"Creating the deploy account {sa}...")
        g("iam", "service-accounts", "create", SA_NAME, "--display-name=RoadSense CD (GitHub Actions)",
          "--description=Deploys Dev to the RoadSense VM over OS Login; no other permissions")

    print("Granting it only what a deploy needs...")
    g("projects", "add-iam-policy-binding", args.project, f"--member=serviceAccount:{sa}",
      "--role=roles/compute.viewer", "--condition=None")
    g("compute", "instances", "add-iam-policy-binding", args.vm, "--zone", args.zone,
      f"--member=serviceAccount:{sa}", "--role=roles/compute.osAdminLogin")
    vm_sa = g("compute", "instances", "describe", args.vm, "--zone", args.zone,
              "--format=value(serviceAccounts[0].email)")
    if vm_sa:
        g("iam", "service-accounts", "add-iam-policy-binding", vm_sa, f"--member=serviceAccount:{sa}",
          "--role=roles/iam.serviceAccountUser")

    print("Turning on OS Login for the VM...")
    g("compute", "instances", "add-metadata", args.vm, "--zone", args.zone, "--metadata=enable-oslogin=TRUE")

    if not g("iam", "workload-identity-pools", "describe", POOL, "--location=global", "--format=value(name)",
             check=False):
        print("Creating the Workload Identity pool...")
        g("iam", "workload-identity-pools", "create", POOL, "--location=global", "--display-name=GitHub Actions")
    condition = f"assertion.repository=='{args.repo}' && assertion.ref=='refs/heads/{args.branch}'"
    if not g("iam", "workload-identity-pools", "providers", "describe", PROVIDER, "--location=global",
             f"--workload-identity-pool={POOL}", "--format=value(name)", check=False):
        print(f"Trusting GitHub tokens from {args.repo} on {args.branch} only...")
        g("iam", "workload-identity-pools", "providers", "create-oidc", PROVIDER, "--location=global",
          f"--workload-identity-pool={POOL}", "--display-name=GitHub OIDC",
          "--issuer-uri=https://token.actions.githubusercontent.com",
          "--attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository,"
          "attribute.ref=assertion.ref", f"--attribute-condition={condition}")
    g("iam", "service-accounts", "add-iam-policy-binding", sa, "--role=roles/iam.workloadIdentityUser",
      f"--member=principalSet://iam.googleapis.com/{pool_id}/attribute.repository/{args.repo}")

    ip = g("compute", "instances", "describe", args.vm, "--zone", args.zone,
           "--format=value(networkInterfaces[0].accessConfigs[0].natIP)")
    variables = {"GCP_PROJECT": args.project, "GCP_ZONE": args.zone, "GCP_VM": args.vm, "GCP_DEPLOY_SA": sa,
                 "GCP_WIF_PROVIDER": f"{pool_id}/providers/{PROVIDER}",
                 "ROADSENSE_URL": args.url or f"https://{ip.replace('.', '-')}.sslip.io"}
    print("\nDone. Repository variables for .github/workflows/cd.yml (none of them is secret):")
    for k, v in variables.items():
        print(f"  {k}={v}")
    if args.github_vars:
        gh = shutil.which("gh")
        if not gh:
            raise SystemExit("`gh` is not installed: set the variables in the repository's Settings > Variables.")
        for k, v in variables.items():
            r = subprocess.run([gh, "variable", "set", k, "--repo", args.repo, "--body", v], capture_output=True,
                               text=True)
            print(f"  {'set' if r.returncode == 0 else 'FAILED'} {k}" + ("" if r.returncode == 0 else
                                                                       f": {r.stderr.strip()[:200]}"))


if __name__ == "__main__":
    sys.exit(main())
