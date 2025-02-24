#!/usr/bin/env python3

import requests
import os
import tempfile
import zipfile
import shutil
import pathlib
import logging
import urllib.parse
import jinja2


def _paginate(session, url, params=None, item_key=None):
    while True:
        response = session.get(url, params=params)
        response.raise_for_status()
        j = response.json()
        if item_key:
            yield from j[item_key]
        else:
            yield from j
        if not response.links.get("next"):
            break
        url = response.links["next"]["url"]
        params = None


def get_repo_details(session, repo):
    response = session.get(f"https://api.github.com/repos/{repo}")
    response.raise_for_status()
    return response.json()


def find_workflow(session, repo, workflow_name):
    for workflow in _paginate(
        session,
        f"https://api.github.com/repos/{repo}/actions/workflows",
        item_key="workflows",
    ):
        if workflow["name"] == workflow_name:
            return workflow

    raise ValueError(f"Workflow '{workflow_name}' not found")


def find_artifact(session, artifacts_url, artifact_name):
    for artifact in _paginate(session, artifacts_url, item_key="artifacts"):
        if artifact["name"] == artifact_name:
            return artifact


def find_latest_artifacts(session, repo, workflow_id, artifact_name):
    artifacts = {}
    for run in _paginate(
        session,
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_id}/runs",
        params={"status": "success"},
        item_key="workflow_runs",
    ):
        artifact = find_artifact(session, run["artifacts_url"], artifact_name)
        if not artifact or artifact["expired"]:
            continue

        owner_label = run["head_repository"]["owner"]["login"]
        branch = run["head_branch"]
        key = f"{owner_label}/{branch}"

        # Assumes response is sorted, newest to oldest
        if key not in artifacts:
            # TODO: You might hope that you could fetch
            # https://api.github.com/repos/{repo}/actions/runs/{artifact['workflow_run']['id']}
            # and inspect the pull_requests property to find the corresponding PR for each branch.
            # But as discussed at # https://github.com/orgs/community/discussions/25220 that
            # property is always empty for builds from forks.
            artifacts[key] = artifact

    return artifacts


def download_and_extract(session, url, dest_dir):
    with session.get(url, stream=True) as response:
        response.raise_for_status()
        with tempfile.TemporaryFile() as f:
            shutil.copyfileobj(response.raw, f)
            zipfile.ZipFile(f).extractall(dest_dir)


def main():
    logging.basicConfig(level=logging.INFO)

    api_token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    workflow_name = os.environ["WORKFLOW_NAME"]
    artifact_name = os.environ["ARTIFACT_NAME"]

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )

    repo_details = get_repo_details(session, repo)
    default_branch = "/".join((repo_details['owner']['login'], repo_details["default_branch"]))
    workflow = find_workflow(session, repo, workflow_name)
    web_artifacts = find_latest_artifacts(session, repo, workflow["id"], artifact_name)

    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="godoctopus-"))
    logging.info("Assembling site at %s", tmpdir)

    # Place default branch content at root of site
    artifact = web_artifacts.pop(default_branch)
    url = artifact["archive_download_url"]
    logging.info("Fetching %s export from %s", default_branch, url)
    download_and_extract(session, url, tmpdir)

    items = []
    branches_dir = tmpdir / "branches"
    branches_dir.mkdir()

    for branch, artifact in web_artifacts.items():
        url = artifact["archive_download_url"]
        logging.info("Fetching %s export from %s", branch, url)

        branch_quoted = urllib.parse.quote(branch)
        branch_dir = branches_dir / branch_quoted
        branch_dir.mkdir(parents=True)
        download_and_extract(session, url, branch_dir)

        items.append({"dir": branch_quoted, "name": branch})

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
        autoescape=jinja2.select_autoescape(),
    )
    template = env.get_template("branches.html")
    with open(branches_dir / "index.html", "w") as f:
        template.stream(title="Branches", branches=items).dump(f)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"path={tmpdir}\n")


if __name__ == "__main__":
    main()
