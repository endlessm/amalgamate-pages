#!/usr/bin/env python3

import collections.abc
import dataclasses
import datetime as dt
import logging
import os
import pathlib
import shutil
import tempfile
import zipfile
from typing import Any, Iterator

import jinja2
import requests
import requests_cache


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


@dataclasses.dataclass
class Release:
    data: dict
    asset: dict


def lead_sorted(seq: collections.abc.KeysView[str], first: str) -> list[str]:
    """Return a list with `first` at the front if present, followed by the rest sorted."""
    if first in seq:
        return [first] + sorted(seq - {first})
    else:
        return sorted(seq)


def pretty_datetime(d: dt.datetime) -> str:
    return d.strftime("%A %-d %B %Y, %-I:%M %p %Z")


class AmalgamatePages:
    def __init__(
        self, api_token: str, default_repo: str, workflow_name: str, artifact_name: str
    ):
        self.default_repo = default_repo
        self.workflow_name = workflow_name
        self.artifact_name = artifact_name

        backend = requests_cache.SQLiteCache()
        self.session = requests_cache.CachedSession(
            backend=backend, cache_control=True, expire_after=60
        )
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

        self.jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
            autoescape=jinja2.select_autoescape(),
        )
        self.jinja_env.filters["from_iso8601"] = dt.datetime.fromisoformat
        self.jinja_env.filters["pretty_datetime"] = pretty_datetime

    def _paginate(
        self, url, params: dict | None = None, item_key: str | None = None
    ) -> Iterator[dict]:
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

    def get_latest_built_release(self) -> Release | None:
        """Fetches data on the latest release that has an asset that looks like a web build."""
        name_suffix = f"-{self.artifact_name}.zip"
        content_type = "application/zip"

        logging.info(
            "Finding latest release with an asset whose name ends with '%s'",
            name_suffix,
        )

        # https://docs.github.com/en/rest/releases/releases?apiVersion=2022-11-28#get-the-latest-release
        # does not allow you to fetch the latest prerelease, and currently all
        # releases in Threadbare are prereleases.
        for release in self._paginate(
            f"https://api.github.com/repos/{self.default_repo}/releases"
        ):
            if release["draft"]:
                continue

            # TODO: Add a parameter to control whether pre-releases are used?

            for asset in release["assets"]:
                if (
                    asset["name"].endswith(name_suffix)
                    and asset["content_type"] == content_type
                ):
                    logging.info(
                        "Found suitable asset %s in release %s",
                        asset["name"],
                        release["name"],
                    )
                    return Release(release, asset)

        logging.info("No suitable release/asset found")
        return None

    def download_and_extract(
        self, url: str, dest_dir: pathlib.Path, headers: dict[str, str] | None = None
    ) -> None:
        with self.session.get(url, headers=headers, stream=True) as response:
            response.raise_for_status()
            with tempfile.TemporaryFile() as f:
                shutil.copyfileobj(response.raw, f)
                zipfile.ZipFile(f).extractall(dest_dir)

    def render_template(self, name: str, target: pathlib.Path, context: dict) -> None:
        template = self.jinja_env.get_template(name)
        with target.open("w") as f:
            stream = template.stream(context)
            # TemplateStream.dump expects str | IO[bytes]
            # while f is TextIOWrapper[_WrappedBuffer]
            stream.dump(f)  # type: ignore

    def run(self) -> None:
        repo_details = self.get_default_repo_details()
        default_org = repo_details["owner"]["login"]
        default_branch = repo_details["default_branch"]

        latest_release = self.get_latest_built_release()

        workflow = self.find_workflow()
        web_artifacts = self.find_latest_artifacts(workflow["id"])
        pull_requests = self.list_pull_requests()

        tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="godoctopus-"))
        logging.info("Assembling site at %s", tmpdir)
        have_toplevel_build = False

        if latest_release is not None:
            # Downloading a release asset requires setting the Accept header to
            # application/octet-stream, or else you just get the JSON
            # description of the asset back.
            #
            # https://docs.github.com/en/rest/releases/assets?apiVersion=2022-11-28
            #
            # However, setting Accept: application/octet-stream for build
            # artifacts does not work! So we need a different Accept header in
            # the two cases.
            self.download_and_extract(
                latest_release.asset["url"],
                tmpdir,
                headers={"Accept": "application/octet-stream"},
            )
            have_toplevel_build = True

        items = []
        branches_dir = tmpdir / "branches"
        branches_dir.mkdir()

        for org in lead_sorted(web_artifacts.keys(), default_org):
            fork = web_artifacts[org]
            for branch_name in lead_sorted(fork.live_branches.keys(), default_branch):
                branch = fork.live_branches[branch_name]
                if not branch.build and org != default_org:
                    logging.debug(
                        "Ignoring never-built third-party branch %s:%s",
                        org,
                        branch_name,
                    )
                    continue

                item: dict[str, Any] = {}
                item["name"] = (
                    branch_name if org == default_org else f"{org}/{branch_name}"
                )

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

                        if (
                            org == default_org
                            and branch_name == default_branch
                            and not have_toplevel_build
                        ):
                            branch_dir = tmpdir
                            have_toplevel_build = True
                        else:
                            # TODO: Use colon form in directory name, avoiding
                            # intermediate directory with no index?
                            branch_dir = branches_dir / org / branch_name
                            branch_dir.mkdir(parents=True)

                        self.download_and_extract(url, branch_dir)
                        item["relative_path"] = branch_dir.relative_to(
                            branches_dir, walk_up=True
                        )

                items.append(item)

        if not have_toplevel_build:
            self.render_template(
                "redirect.html", tmpdir / "index.html", {"target": "branches/"}
            )

        self.render_template(
            "branches.html",
            branches_dir / "index.html",
            {
                "title": "Branches",
                "latest_release": latest_release,
                "branches": items,
            },
        )

        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"path={tmpdir}\n")

        logging.info("Site assembled at %s", tmpdir)

        self.session.cache.delete(older_than=dt.timedelta(days=7))


def setup_logging() -> None:
    log_format = "+ %(asctime)s %(levelname)s %(name)s: %(message)s"
    date_format = "%H:%M:%S"

    match os.environ.get("DEBUG", "false").lower():
        case "true" | "1":
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
