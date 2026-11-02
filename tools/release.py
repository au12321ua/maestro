"""Maintainer entry point: prepare, inspect, publish or resume one fixed release.

prepare creates two Git branches and a worktree; publish only runs after review and
successful verification. Git refs, Actions, Releases, registry and Pages are the state.
No deployment is performed. See --help for each command.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_package import Registry
from publish_release import Gitea
from verify_commit import verified
from version import set_version

ROOT = Path(__file__).resolve().parents[1]


def git(*args, root=ROOT):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def ancestor(first, second, root=ROOT):
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", first, second], cwd=root, check=False
        ).returncode
        == 0
    )


def resolve(ref, root=ROOT):
    return git("rev-parse", "--verify", ref + "^{commit}", root=root)


def maybe_ref(ref, root=ROOT):
    result = subprocess.run(
        ["git", "rev-parse", "--verify", ref], cwd=root, text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def branches(version):
    return f"release/v{version}", f"release/v{version}-next"


def notes_path(version):
    return f"docs/releases/v{version}.md"


def prepare(args, root=ROOT):
    if git("status", "--porcelain", root=root):
        raise SystemExit("start prepare from a clean checkout; existing files are preserved")
    git("fetch", args.remote, root=root)
    source = resolve(args.commit, root)
    if not ancestor(source, f"{args.remote}/dev", root):
        raise SystemExit("selected commit is not part of remote dev")
    if not ancestor(f"{args.remote}/master", source, root):
        raise SystemExit(
            "master cannot fast-forward to the selected content; reconcile and reverify"
        )
    candidate, completion = branches(args.version)
    remotes = {args.remote}
    if mirror := getattr(args, "github_remote", None):
        remotes.add(mirror)
    for remote in sorted(remotes):
        if git("ls-remote", "--tags", remote, f"refs/tags/v{args.version}", root=root):
            raise SystemExit(f"version {args.version} is already tagged on {remote}; resume it")
    for ref in (
        f"refs/tags/v{args.version}",
        f"{args.remote}/{candidate}",
        f"{args.remote}/{completion}",
    ):
        if maybe_ref(ref, root):
            raise SystemExit(f"{ref} already exists; inspect/status/publish to resume it")
    next_version = (
        args.next_version
        or f"{args.version.rsplit('.', 1)[0]}.{int(args.version.rsplit('.', 1)[1]) + 1}.dev0"
    )
    if not re.fullmatch(r"\d+\.\d+\.\d+\.dev0", next_version):
        raise SystemExit("next version must be X.Y.Z.dev0")
    formal_tuple = tuple(map(int, args.version.split(".")))
    if tuple(map(int, next_version.removesuffix(".dev0").split("."))) <= formal_tuple:
        raise SystemExit("next development version must follow the formal version")
    worktree = args.worktree.resolve()
    allowed = {
        "pyproject.toml",
        "sdk/pyproject.toml",
        "uv.lock",
        "openapi/maestro-v1.json",
        "sdk/src/maestro_sdk/models.py",
        "sdk/src/maestro_sdk/_contract.py",
        "frontend/src/lib/schema.d.ts",
        notes_path(args.version),
    }
    note_text = args.notes.read_text()
    if not note_text.strip():
        raise SystemExit("release notes must be reviewed and nonempty")
    if maybe_ref(candidate, root):
        if not worktree.is_dir() or git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", root=worktree
        ) != git("rev-parse", "--path-format=absolute", "--git-common-dir", root=root):
            raise SystemExit(
                "prepared branch exists; use its original worktree to resume preparation"
            )
        current_branch = git("symbolic-ref", "--short", "HEAD", root=worktree)
        if current_branch not in (candidate, completion):
            raise SystemExit(
                "preparation worktree is on a different branch; inspect before resuming"
            )
        changed = set(git("diff", "--name-only", "HEAD", root=worktree).splitlines())
        changed.update(
            git("ls-files", "--others", "--exclude-standard", root=worktree).splitlines()
        )
        if changed - allowed:
            raise SystemExit(
                "preparation has unrelated changes; preserve and inspect them before resuming"
            )
    else:
        if maybe_ref(completion, root):
            raise SystemExit("completion exists without its candidate; inspect before resuming")
        git("worktree", "add", "-b", candidate, str(worktree), source, root=root)
    final = resolve(candidate, root)
    if final == source:
        if git("symbolic-ref", "--short", "HEAD", root=worktree) != candidate:
            raise SystemExit("incomplete candidate is not checked out in the preparation worktree")
        # Repeat only derived files after an interrupted dependency/generator operation.
        set_version(args.version, worktree)
        notes = worktree / notes_path(args.version)
        notes.parent.mkdir(parents=True, exist_ok=True)
        if notes.exists() and notes.read_text() != note_text:
            raise SystemExit("prepared notes differ; inspect them instead of overwriting")
        notes.write_text(note_text)
        git("add", "--", *sorted(allowed), root=worktree)
        git(
            "commit",
            "-m",
            f"Prepare release {args.version}\n\nRelease-source: {source}\nNext-development: {next_version}",
            root=worktree,
        )
        final = resolve("HEAD", worktree)
    else:
        message = git("show", "-s", "--format=%B", final, root=root)
        if (
            git("rev-parse", final + "^", root=root) != source
            or f"Release-source: {source}" not in message
            or f"Next-development: {next_version}" not in message
            or tomllib.loads(git("show", f"{final}:pyproject.toml", root=root))["project"][
                "version"
            ]
            != args.version
            or git("show", f"{final}:{notes_path(args.version)}", root=root) != note_text.strip()
        ):
            raise SystemExit(
                "existing candidate differs from the requested preparation; inspect before resuming"
            )
    if not maybe_ref(completion, root):
        git("switch", "-c", completion, root=worktree)
    if resolve(completion, root) == final:
        if git("symbolic-ref", "--short", "HEAD", root=worktree) != completion:
            raise SystemExit("resume next-version preparation from its original worktree")
        set_version(next_version, worktree)
        git("add", "--", *sorted(allowed - {notes_path(args.version)}), root=worktree)
        git(
            "commit",
            "-m",
            f"Resume development at {next_version}\n\nRelease-candidate: {final}",
            root=worktree,
        )
    else:
        complete = resolve(completion, root)
        if (
            git("rev-parse", complete + "^", root=root) != final
            or f"Release-candidate: {final}"
            not in git("show", "-s", "--format=%B", complete, root=root)
            or tomllib.loads(git("show", f"{complete}:pyproject.toml", root=root))["project"][
                "version"
            ]
            != next_version
            or git("status", "--porcelain", root=worktree)
        ):
            raise SystemExit("prepared completion differs or has changes; inspect before resuming")
    print(
        json.dumps(
            {
                "source": source,
                "candidate": final,
                "version": args.version,
                "next_version": next_version,
                "completion": resolve(completion, root),
                "worktree": str(worktree),
                "review": f"git diff {source} {candidate}",
            },
            indent=2,
        )
    )
    print(f"After review: git push {args.remote} {candidate} {completion}")
    return final


def candidate_refs(version, remote, root=ROOT):
    found = []
    for branch in branches(version):
        local, upstream = maybe_ref(branch, root), maybe_ref(f"{remote}/{branch}", root)
        if local and upstream and local != upstream:
            raise SystemExit(f"{branch}: local and remote differ; inspect before resuming")
        if not (local or upstream):
            raise SystemExit(f"missing {branch}; fetch the prepared branches to resume")
        found.append(local or upstream)
    if not ancestor(found[0], found[1], root):
        raise SystemExit("next-development branch does not contain the candidate")
    return found


def clients():
    return (
        Gitea(os.environ["GITEA_API"], os.environ["GITEA_REPO"], os.environ["GITEA_TOKEN"]),
        Gitea(
            "https://api.github.com",
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GH_TOKEN"],
            github=True,
        ),
    )


def release_state(client, tag, sha):
    actual = client.tag_commit(tag)
    if actual and actual != sha:
        raise SystemExit(f"{tag} resolves to {actual}, expected {sha}; never move a shared tag")
    release = client.call("GET", "/releases/tags/" + tag, ok_missing=True)
    return release if release and not release["draft"] else None


def latest_run(client, sha, workflow, tag):
    for page in range(1, 101):
        response = client.call("GET", f"/actions/runs?limit=50&per_page=50&page={page}")
        runs = response.get("workflow_runs", [])
        if not runs:
            return None
        for run in runs:
            if run.get("head_sha") != sha or run.get("event") != "push":
                continue
            if client.github:
                matches = (
                    run.get("path") == f".github/workflows/{workflow}"
                    and run.get("head_branch") == tag
                )
            else:
                matches = run.get("path") == f"{workflow}@refs/tags/{tag}"
            if matches:
                return run
    raise SystemExit("too many Actions runs to locate publication; inspect manually")


def wait_publication(client, sha, workflow, tag, deadline, retry=False):
    retried = False
    while time.monotonic() < deadline:
        run = latest_run(client, sha, workflow, tag)
        if run:
            print(
                f"publication: {run['html_url']} ({run['status']}/{run.get('conclusion')})",
                flush=True,
            )
            if run["status"] == "completed":
                if run.get("conclusion") == "success":
                    return run
                if retry and not retried:
                    client.call("POST", f"/actions/runs/{run['id']}/rerun-failed-jobs")
                    retried = True
                else:
                    raise SystemExit(
                        "publication incomplete; inspect the failed job, then publish --retry"
                    )
        time.sleep(10)
    raise SystemExit("publication still incomplete; rerun publish to continue from current facts")


def compare_channels(gitea, github, first, second):
    left = {a["name"]: a for a in first["assets"]}
    right = {a["name"]: a for a in second["assets"]}
    if not left or left.keys() != right.keys():
        raise SystemExit("release channels have different/missing attachments")
    for name, asset in left.items():
        one = gitea.asset_digest(asset, first["id"])
        two = github.asset_digest(right[name], second["id"])
        if one != two:
            raise SystemExit(f"release channels disagree on {name}; no file will be overwritten")
    (wheel,) = [n for n in left if n.startswith("maestro_sdk-") and n.endswith(".whl")]
    return wheel, gitea.asset_digest(left[wheel], first["id"])


def finish_dev(candidate, completion, args, root=ROOT):
    git("fetch", args.remote, root=root)
    current = resolve(f"{args.remote}/dev", root)
    if ancestor(completion, current, root):
        print(f"dev already includes next development version: {current}")
        return
    if ancestor(current, completion, root):
        final = completion
    else:
        # A new worktree retains any merge conflict for review. Never reset remote dev.
        path = args.finish_worktree.resolve()
        if path.exists():
            if git("status", "--porcelain", root=path):
                raise SystemExit(
                    f"resolve and commit the retained dev integration at {path}, then retry"
                )
            final = resolve("HEAD", path)
            if not ancestor(candidate, final, root) or not ancestor(completion, final, root):
                raise SystemExit("retained dev integration does not include prepared completion")
        else:
            git("worktree", "add", "--detach", str(path), completion, root=root)
        if not ancestor(current, resolve("HEAD", path), root):
            git("merge", "--no-edit", current, root=path)
        final = resolve("HEAD", path)
    # One fast-forward push contains both formal preparation and next-dev version.
    git("push", args.remote, f"{final}:refs/heads/dev", root=root)
    print(f"dev completed: {final}")


def publish(args, root=ROOT):
    git("fetch", args.remote, root=root)
    sha, completion = candidate_refs(args.version, args.remote, root)
    gitea, github = clients()
    tag = f"v{args.version}"
    run = verified(gitea, sha)
    if run is None:
        raise SystemExit(f"candidate {sha} lacks full verification; use verify subcommand first")
    print(f"source verified: {run['html_url']}", flush=True)
    # Preflight both fixed tag identities before advancing either release branch.
    for client in (gitea, github):
        actual = client.tag_commit(tag)
        if actual and actual != sha:
            raise SystemExit(f"{tag} already points at {actual}, expected {sha}")
    following = tomllib.loads(git("show", f"{completion}:pyproject.toml", root=root))["project"][
        "version"
    ]
    if not re.fullmatch(r"\d+\.\d+\.\d+\.dev0", following):
        raise SystemExit("prepared completion must restore a development version")
    source_version = git("show", f"{sha}:pyproject.toml", root=root)
    if tomllib.loads(source_version)["project"]["version"] != args.version:
        raise SystemExit("candidate source version differs from requested tag")
    notes = git("show", f"{sha}:{notes_path(args.version)}", root=root)
    if not notes.strip():
        raise SystemExit("candidate has no release notes")
    if not ancestor(f"{args.remote}/master", sha, root):
        # Already published, master may have moved ahead: allow resuming that release only.
        if not (ancestor(sha, f"{args.remote}/master", root) and gitea.tag_commit(tag) == sha):
            raise SystemExit("master changed; cannot safely fast-forward, prepare and verify again")
    else:
        git("push", args.remote, f"{sha}:refs/heads/master", root=root)
    existing = maybe_ref(f"refs/tags/{tag}", root)
    if existing:
        if (
            resolve(f"refs/tags/{tag}", root) != sha
            or git("cat-file", "-t", existing, root=root) != "tag"
        ):
            raise SystemExit("local formal tag must be annotated and match the candidate")
    else:
        git("tag", "-a", tag, sha, "-m", notes, root=root)
    git("push", args.remote, f"refs/tags/{tag}", root=root)
    deadline = time.monotonic() + args.timeout
    wait_publication(gitea, sha, "verify.yml", tag, deadline, args.retry)
    first = release_state(gitea, tag, sha)
    if not first:
        raise SystemExit("Gitea publication finished without a public release")
    git("fetch", args.github_remote, "master", root=root)
    if not ancestor(f"{args.github_remote}/master", sha, root):
        if not (
            ancestor(sha, f"{args.github_remote}/master", root) and github.tag_commit(tag) == sha
        ):
            raise SystemExit("GitHub master diverged; refusing to overwrite it")
    else:
        git("push", args.github_remote, f"{sha}:refs/heads/master", root=root)
    git("push", args.github_remote, f"refs/tags/{tag}", root=root)
    wait_publication(github, sha, "release.yml", tag, deadline, args.retry)
    second = release_state(github, tag, sha)
    if not second:
        raise SystemExit("GitHub publication finished without a public release")
    wheel, digest = compare_channels(gitea, github, first, second)
    server = os.environ["GITEA_API"].removesuffix("/api/v1")
    registry = Registry(server, os.environ["GITEA_REPO"].split("/")[0], os.environ["GITEA_TOKEN"])
    if registry.published_digest(wheel) != digest:
        raise SystemExit("SDK registry is missing the release wheel or has a different digest")
    repo = os.environ["GITHUB_REPOSITORY"].split("/")
    pages = args.pages_url or f"https://{repo[0]}.github.io/{repo[1]}/simple/maestro-sdk/"
    with urllib.request.urlopen(pages, timeout=60) as response:
        page = response.read().decode()
    if f"{wheel}#sha256={digest}" not in page:
        raise SystemExit("Pages index has not published the verified SDK digest; retry index job")
    finish_dev(sha, completion, args, root)
    print(
        json.dumps(
            {
                "complete": True,
                "commit": sha,
                "version": args.version,
                "gitea": first["html_url"],
                "github": second["html_url"],
                "index": pages,
            },
            indent=2,
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser(
        "prepare", help="prepare reviewable version/contract/notes/next-dev commits"
    )
    prep.add_argument("--commit", required=True)
    prep.add_argument("--version", required=True)
    prep.add_argument("--next-version")
    prep.add_argument("--notes", type=Path, required=True)
    prep.add_argument("--worktree", type=Path, required=True)
    prep.add_argument("--remote", default="origin")
    prep.add_argument("--github-remote", default="github")
    for command in ("status", "verify", "publish"):
        item = sub.add_parser(command)
        item.add_argument("--version", required=True)
        item.add_argument("--remote", default="origin")
        if command == "publish":
            item.add_argument("--github-remote", default="github")
            item.add_argument("--finish-worktree", type=Path, required=True)
            item.add_argument("--timeout", type=int, default=1800)
            item.add_argument(
                "--retry", action="store_true", help="retry failed publication jobs only"
            )
            item.add_argument("--pages-url", help="custom Pages SDK project index URL")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        parser.error("formal version must be X.Y.Z")
    if args.command == "prepare":
        prepare(args)
    elif args.command == "publish":
        publish(args)
    else:
        git("fetch", args.remote)
        sha, completion = candidate_refs(args.version, args.remote)
        client = Gitea(os.environ["GITEA_API"], os.environ["GITEA_REPO"], os.environ["GITEA_TOKEN"])
        if args.command == "verify":
            branch = branches(args.version)[0]
            if resolve(f"{args.remote}/{branch}") != sha:
                raise SystemExit(
                    "push the prepared candidate branch before dispatching verification"
                )
            client.call("POST", "/actions/workflows/verify.yml/dispatches", {"ref": branch})
            print(f"verification requested for {sha} on {branch}")
        else:
            mirror = Gitea(
                "https://api.github.com",
                os.environ["GITHUB_REPOSITORY"],
                os.environ["GH_TOKEN"],
                github=True,
            )
            tag = f"v{args.version}"
            print(
                json.dumps(
                    {
                        "candidate": sha,
                        "completion": completion,
                        "verification": verified(client, sha),
                        "gitea": release_state(client, tag, sha),
                        "github": release_state(mirror, tag, sha),
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
