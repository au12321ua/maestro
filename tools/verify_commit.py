"""Require a successful full verification job for an exact commit in this repository."""

import argparse
import os
import sys
from pathlib import Path

# Also works under python -I, without importing modules from the caller's directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_release import Gitea

REQUIRED_STEPS = {
    "Service and Runner environments",
    "Lint",
    "Markdown links",
    "Versions",
    "Tests",
    "Web client dependencies",
    "Web client",
    "Built pages match their sources",
    "Third-party licenses are allowed",
}


def verified(client, sha):
    page = 1
    while True:
        result = client.call("GET", f"/actions/runs?limit=50&page={page}")
        runs = result.get("workflow_runs", []) if isinstance(result, dict) else result
        if not runs:
            return None
        for run in runs:
            if (
                run.get("head_sha") != sha
                or run.get("event") not in {"push", "workflow_dispatch"}
                or not run.get("path", "").startswith("verify.yml@refs/heads/")
            ):
                continue
            jobs = client.call("GET", f"/actions/runs/{run['id']}/jobs?limit=100")
            jobs = jobs.get("jobs", []) if isinstance(jobs, dict) else jobs
            for job in jobs:
                steps = {s["name"]: s.get("conclusion") for s in job.get("steps", [])}
                if (
                    job.get("name") == "verify"
                    and job.get("head_sha") == sha
                    and job.get("status") == "completed"
                    and job.get("conclusion") == "success"
                    and all(steps.get(name) == "success" for name in REQUIRED_STEPS)
                ):
                    return run
        page += 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    client = Gitea(os.environ["GITEA_API"], os.environ["GITEA_REPO"], os.environ["GITEA_TOKEN"])
    run = verified(client, args.commit)
    if run is None:
        raise SystemExit(
            f"no successful full verify job for {args.commit}; dispatch verify.yml on "
            "the candidate branch, then retry the publication job"
        )
    print(f"verified {args.commit}: {run['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
