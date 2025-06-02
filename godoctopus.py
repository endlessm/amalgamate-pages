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


def pretty_date_from_iso8601(d: str) -> str:
    return datetime.datetime.fromisoformat(d).strftime("%A %-d %B %Y")


class AmalgamatePages:
    def __init__(
        self, api_token: str, default_repo: str, workflow_name: str, artifact_name: str
    ):
        self.default_repo = default_repo
        self.workflow_name = workflow_name
        self.artifact_name = artifact_name

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _paginate(self, url, params=None, item_key=None):
        if not params:
            params = {}
        params.setdefault("per_page", 100)

        while True:
            response = self.session.get(url, params=params)
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

    def get_default_repo_details(self) -> dict[str, Any]:
        response = self.session.get(f"https://api.github.com/repos/{self.default_repo}")
        response.raise_for_status()
        return response.json()

    def find_workflow(self) -> dict[str, Any]:
        for workflow in self._paginate(
            f"https://api.github.com/repos/{self.default_repo}/actions/workflows",
            item_key="workflows",
        ):
            if workflow["name"] == self.workflow_name:
                return workflow

        raise ValueError(f"Workflow '{self.workflow_name}' not found")

    def list_branches(self, repo: str) -> list[dict]:
        try:
            return list(
                self._paginate(
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

    def list_pull_requests(self) -> dict[str, list[dict]]:
        """
        Returns a map from branch label to a list of pull requests for that branch,
        with open PRs before closed ones and more recently-updated ones before older
        ones. "Branch label" here means "user:branch". This is unambiguous because
        any given user/org can have at most one fork of a repo.
        """
        branch_prs: dict[str, list[dict]] = {}

        for pr in self._paginate(
            f"https://api.github.com/repos/{self.default_repo}/pulls",
            params={"state": "all"},
        ):
            branch_prs.setdefault(pr["head"]["label"], []).append(pr)

        for prs in branch_prs.values():
            # Sort open pull requests before closed ones, then more recently-updated
            # ones before older ones.
            prs.sort(
                key=lambda pr: (pr["state"] == "open", pr["updated_at"]), reverse=True
            )

        return branch_prs

    def find_artifact(self, artifacts_url: str) -> dict[str, Any] | None:
        for artifact in self._paginate(artifacts_url, item_key="artifacts"):
            if artifact["name"] == self.artifact_name:
                return artifact
        return None

    def find_latest_artifacts(self, workflow_id: int) -> dict[str, Fork]:
        artifacts: dict[str, Fork] = {}
        for run in self._paginate(
            f"https://api.github.com/repos/{self.default_repo}/actions/workflows/{workflow_id}/runs",
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
                        for branch in self.list_branches(
                            run["head_repository"]["full_name"]
                        )
                    }
                )
                artifacts[owner_label] = fork

            branch_name = run["head_branch"]
            try:
                branch = fork.live_branches[branch_name]
            except KeyError:
                logging.debug(
                    "Ignoring artifact for deleted branch %s/%s",
                    owner_label,
                    branch_name,
                )
                continue

            if not branch.build or branch.build.artifact["expired"]:
                artifact = self.find_artifact(run["artifacts_url"])
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

    def download_and_extract(self, url: str, dest_dir: pathlib.Path) -> None:
        with self.session.get(url, stream=True) as response:
            response.raise_for_status()
            with tempfile.TemporaryFile() as f:
                shutil.copyfileobj(response.raw, f)
                zipfile.ZipFile(f).extractall(dest_dir)

    def run(self) -> None:
        repo_details = self.get_default_repo_details()
        default_org = repo_details["owner"]["login"]
        default_branch_name = repo_details["default_branch"]

        workflow = self.find_workflow()
        web_artifacts = self.find_latest_artifacts(workflow["id"])
        pull_requests = self.list_pull_requests()

        tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="godoctopus-"))
        logging.info("Assembling site at %s", tmpdir)

        # Place default branch content at root of site
        default_branch = web_artifacts[default_org].live_branches.pop(
            default_branch_name
        )
        if not default_branch.build or default_branch.build.artifact["expired"]:
            raise ValueError(
                f"No artifact found for default branch {default_branch_name}"
            )
        url = default_branch.build.artifact["archive_download_url"]
        logging.info(
            "Fetching %s export from %s/%s", default_org, default_branch_name, url
        )
        self.download_and_extract(url, tmpdir)

        items = []
        branches_dir = tmpdir / "branches"
        branches_dir.mkdir()

        for org, fork in web_artifacts.items():
            for branch_name, branch in fork.live_branches.items():
                if not branch.build and org != default_org:
                    logging.debug(
                        "Ignoring never-built third-party branch %s:%s",
                        org,
                        branch_name,
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
                        logging.info(
                            "Fetching %s:%s export from %s", org, branch_name, url
                        )

                        # TODO: Use colon form in directory name, avoiding intermediate
                        # directory with no index?
                        branch_dir = branches_dir / org / branch_name
                        branch_dir.mkdir(parents=True)
                        self.download_and_extract(url, branch_dir)
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


def setup_logging() -> None:
    log_format = "+ %(asctime)s %(levelname)s %(name)s: %(message)s"
    date_format = "%H:%M:%S"

    match os.environ.get("DEBUG", "false").lower():
        case "true":
            level = logging.DEBUG
        case _:
            level = logging.INFO

    logging.basicConfig(level=level, format=log_format, datefmt=date_format)


def main() -> None:
    api_token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    workflow_name = os.environ["WORKFLOW_NAME"]
    artifact_name = os.environ["ARTIFACT_NAME"]

    setup_logging()

    amalgamate_pages = AmalgamatePages(api_token, repo, workflow_name, artifact_name)
    amalgamate_pages.run()


if __name__ == "__main__":
    main()
