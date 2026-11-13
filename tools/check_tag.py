"""Fail unless a tag is an annotated tag on the server that points at the given commit.

    python3 -I tools/check_tag.py --tag v1.2.3 --commit <sha>           # Gitea: GITEA_API, GITEA_REPO, GITEA_TOKEN
    python3 -I tools/check_tag.py --github --tag v1.2.3 --commit <sha>  # GitHub: GITHUB_REPOSITORY, GH_TOKEN

The check asks the server, not the local clone: actions/checkout fetches the tagged
commit into refs/tags/<name>, which replaces an annotated tag with a plain ref.
"""

import argparse
import os
import sys
from pathlib import Path

# Also works under python -I, without importing modules from the caller's directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_release import Gitea


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--github", action="store_true")
    args = parser.parse_args(argv)
    if args.github:
        api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        client = Gitea(api, os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"], github=True)
    else:
        client = Gitea(os.environ["GITEA_API"], os.environ["GITEA_REPO"], os.environ["GITEA_TOKEN"])
    commit = client.annotated_tag_commit(args.tag)
    if commit is None:
        raise SystemExit(f"{args.tag} is not an annotated tag on the server")
    if commit != args.commit:
        raise SystemExit(f"{args.tag} points at {commit}, not {args.commit}")
    print(f"{args.tag} is an annotated tag of {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
