#!/usr/bin/env python3

import dataclasses
import datetime
import logging
import os
import pathlib
import shutil
import tempfile
import zipfile
from typing import Any

import jinja2
import requests


@dataclasses.dataclass
class Build:
    workflow_run: dict
    artifact: dict


@dataclasses.dataclass
class Branch:
    info: dict
    build: Build | None


@dataclasses.dataclass
class Fork:
    live_branches: dict[str, Branch]


def _paginate(session, url, params=None, item_key=None):
    if not params:
        params = {}
    params.setdefault("per_page", 100)

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


def list_branches(session, repo) -> list[dict]:
    try:
        return list(
            _paginate(
                session,
                f"https://api.github.com/repos/{repo}/branches",
            )
        )
    except requests.HTTPError as error:
        if error.response.status_code != 404:
            raise
        logging.debug(
            "404 when fetching branches for %s; assuming this fork was deleted",
            repo,
        )
        return []


def list_pull_requests(session, repo) -> dict[str, list[dict]]:
    """
    Returns a map from branch label to a list of pull requests for that branch,
    with open PRs before closed ones and more recently-updated ones before older
    ones. "Branch label" here means "user:branch". This is unambiguous because
    any given user/org can have at most one fork of a repo.
    """
    branch_prs: dict[str, list[dict]] = {}

    for pr in _paginate(
        session,
        f"https://api.github.com/repos/{repo}/pulls",
        params={"state": "all"},
    ):
        branch_prs.setdefault(pr["head"]["label"], []).append(pr)

    for prs in branch_prs.values():
        # Sort open pull requests before closed ones, then more recently-updated
        # ones before older ones.
        prs.sort(key=lambda pr: (pr["state"] == "open", pr["updated_at"]), reverse=True)

    return branch_prs


def find_artifact(session, artifacts_url, artifact_name):
    for artifact in _paginate(session, artifacts_url, item_key="artifacts"):
        if artifact["name"] == artifact_name:
            return artifact


def find_latest_artifacts(session, repo, workflow_id, artifact_name) -> dict[str, Fork]:
    artifacts: dict[str, Fork] = {}
    for run in _paginate(
        session,
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_id}/runs",
        params={"status": "success"},
        item_key="workflow_runs",
    ):
        owner_label = run["head_repository"]["owner"]["login"]
        try:
            fork = artifacts[owner_label]
        except KeyError:
            fork = Fork(
                live_branches={
                    branch["name"]: Branch(info=branch, build=None)
                    for branch in list_branches(
                        session, run["head_repository"]["full_name"]
                    )
                }
            )
            artifacts[owner_label] = fork

        branch_name = run["head_branch"]
        try:
            branch = fork.live_branches[branch_name]
        except KeyError:
            logging.debug(
                "Ignoring artifact for deleted branch %s/%s", owner_label, branch_name
            )
            continue

        if not branch.build or branch.build.artifact["expired"]:
            artifact = find_artifact(session, run["artifacts_url"], artifact_name)
            if not artifact:
                continue

            if not branch.build or (
                branch.build.artifact["expired"] and not artifact["expired"]
            ):
                branch.build = Build(workflow_run=run, artifact=artifact)
            # TODO: You might hope that you could fetch
            # https://api.github.com/repos/{repo}/actions/runs/{artifact['workflow_run']['id']}
            # and inspect the pull_requests property to find the corresponding PR for each branch.
            # But as discussed at https://github.com/orgs/community/discussions/25220 that
            # property is always empty for builds from forks.

    return artifacts


def download_and_extract(session, url, dest_dir):
    with session.get(url, stream=True) as response:
        response.raise_for_status()
        with tempfile.TemporaryFile() as f:
            shutil.copyfileobj(response.raw, f)
            zipfile.ZipFile(f).extractall(dest_dir)


def pretty_date_from_iso8601(d: str) -> str:
    return datetime.datetime.fromisoformat(d).strftime("%A %-d %B %Y")


def main() -> None:
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    api_token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    workflow_name = os.environ["WORKFLOW_NAME"]
    artifact_name = os.environ["ARTIFACT_NAME"]

    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO)

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )

    repo_details = get_repo_details(session, repo)
    default_org = repo_details["owner"]["login"]
    default_branch_name = repo_details["default_branch"]

    workflow = find_workflow(session, repo, workflow_name)
    web_artifacts = find_latest_artifacts(session, repo, workflow["id"], artifact_name)
    pull_requests = list_pull_requests(session, repo)

    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="godoctopus-"))
    logging.info("Assembling site at %s", tmpdir)

    # Place default branch content at root of site
    default_branch = web_artifacts[default_org].live_branches.pop(default_branch_name)
    if not default_branch.build or default_branch.build.artifact["expired"]:
        raise ValueError(f"No artifact found for default branch {default_branch_name}")
    url = default_branch.build.artifact["archive_download_url"]
    logging.info("Fetching %s export from %s/%s", default_org, default_branch_name, url)
    download_and_extract(session, url, tmpdir)

    items = []
    branches_dir = tmpdir / "branches"
    branches_dir.mkdir()

    for org, fork in web_artifacts.items():
        for branch_name, branch in fork.live_branches.items():
            if not branch.build and org != default_org:
                logging.debug(
                    "Ignoring never-built third-party branch %s:%s", org, branch_name
                )
                continue

            item: dict[str, Any] = {"name": f"{org}/{branch_name}"}

            try:
                pull_request = pull_requests[f"{org}:{branch_name}"][0]
            except (KeyError, IndexError):
                pass
            else:
                if pull_request["state"] == "closed":
                    logging.info(
                        "Ignoring branch %s; newest pull request %s is closed",
                        item["name"],
                        pull_request["url"],
                    )
                    continue
                item["pull_request"] = pull_request

            if branch.build:
                item["build"] = branch.build

                if not branch.build.artifact["expired"]:
                    url = branch.build.artifact["archive_download_url"]
                    logging.info("Fetching %s:%s export from %s", org, branch_name, url)

                    # TODO: Use colon form in directory name, avoiding intermediate
                    # directory with no index?
                    branch_dir = branches_dir / org / branch_name
                    branch_dir.mkdir(parents=True)
                    download_and_extract(session, url, branch_dir)
                    item["relative_path"] = f"{org}/{branch_name}"

            items.append(item)

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
        autoescape=jinja2.select_autoescape(),
    )
    env.filters["pretty_date_from_iso8601"] = pretty_date_from_iso8601
    template = env.get_template("branches.html")
    with (branches_dir / "index.html").open("w") as f:
        stream = template.stream(title="Branches", branches=items)
        # TemplateStream.dump expects str | IO[bytes]
        # while f is TextIOWrapper[_WrappedBuffer]
        stream.dump(f)  # type: ignore

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"path={tmpdir}\n")

    logging.info("Site assembled at %s", tmpdir)


if __name__ == "__main__":
    main()
