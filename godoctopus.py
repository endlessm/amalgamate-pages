#!/usr/bin/env python3

import argparse
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

API = "https://api.github.com"


class ConfigurationError(Exception):
    pass


@dataclasses.dataclass
class Build:
    workflow_run: dict
    artifact: dict


@dataclasses.dataclass
class Branch:
    info: dict
    build: Build | None

    @property
    def name(self) -> str:
        return self.info["name"]


@dataclasses.dataclass
class Fork:
    live_branches: dict[str, Branch]


@dataclasses.dataclass
class Release:
    data: dict
    asset: dict


PullRequest = dict


class GitHubApi:
    def __init__(self, api_token: str):
        cache_backend = requests_cache.SQLiteCache()
        self.session = requests_cache.CachedSession(
            backend=cache_backend, cache_control=True, expire_after=60
        )
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def paginate(
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


def lead_sorted(seq: collections.abc.KeysView[str], first: str) -> list[str]:
    """Return a list with `first` at the front if present, followed by the rest sorted."""
    if first in seq:
        return [first] + sorted(seq - {first})
    else:
        return sorted(seq)


def pretty_datetime(d: dt.datetime) -> str:
    return d.strftime("%A %-d %B %Y, %-I:%M %p %Z")


def make_jinja2_env() -> jinja2.Environment:
    jinja_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
        autoescape=jinja2.select_autoescape(),
    )
    jinja_env.filters["from_iso8601"] = dt.datetime.fromisoformat
    jinja_env.filters["pretty_datetime"] = pretty_datetime

    return jinja_env


class AmalgamatePages:
    repo_details: dict[str, Any]

    def __init__(
        self,
        api: GitHubApi,
        default_repo: str,
        workflow_name: str,
        artifact_name: str,
    ):
        self.api = api
        self.default_repo = default_repo
        self.workflow_name = workflow_name
        self.artifact_name = artifact_name

        self.jinja_env = make_jinja2_env()

    def get_default_repo_details(self) -> None:
        response = self.api.session.get(f"{API}/repos/{self.default_repo}")
        response.raise_for_status()
        self.repo_details = response.json()

    @property
    def default_org(self) -> str:
        return self.repo_details["owner"]["login"]

    @property
    def default_branch(self) -> str:
        return self.repo_details["default_branch"]

    def find_workflow(self) -> dict[str, Any]:
        for workflow in self.api.paginate(
            f"{API}/repos/{self.default_repo}/actions/workflows",
            item_key="workflows",
        ):
            if workflow["name"] == self.workflow_name:
                return workflow

        raise ConfigurationError(
            f"Workflow '{self.workflow_name}' not found. "
            "Has this project been built at least once?"
        )

    def list_branches(self, repo: str) -> list[dict]:
        try:
            return list(self.api.paginate(f"{API}/repos/{repo}/branches"))
        except requests.HTTPError as error:
            if error.response.status_code != 404:
                raise
            logging.debug(
                "404 when fetching branches for %s; assuming this fork was deleted",
                repo,
            )
            return []

    def list_pull_requests(self) -> dict[str, PullRequest]:
        """
        Returns a map from branch label to the best pull request for that branch,
        preferring open PRs to closed ones and more recently-updated ones to older
        ones. "Branch label" here means "user:branch". This is unambiguous because
        any given user/org can have at most one fork of a repo.
        """
        branch_prs: dict[str, list[PullRequest]] = {}

        for pr in self.api.paginate(
            f"{API}/repos/{self.default_repo}/pulls",
            params={"state": "all"},
        ):
            branch_prs.setdefault(pr["head"]["label"], []).append(pr)

        return {
            label: max(prs, key=lambda pr: (pr["state"] == "open", pr["updated_at"]))
            for label, prs in branch_prs.items()
        }

    def find_artifact(self, artifacts_url: str) -> dict[str, Any] | None:
        for artifact in self.api.paginate(artifacts_url, item_key="artifacts"):
            if artifact["name"] == self.artifact_name:
                return artifact
        return None

    def find_latest_artifacts(self, workflow_id: int) -> dict[str, Fork]:
        artifacts: dict[str, Fork] = {}
        for run in self.api.paginate(
            f"{API}/repos/{self.default_repo}/actions/workflows/{workflow_id}/runs",
            params={"status": "success"},
            item_key="workflow_runs",
        ):
            head_repository = run["head_repository"]
            if head_repository is None:
                logging.debug(
                    "Ignoring workflow run %s from deleted fork",
                    run["html_url"],
                )
                continue

            owner_label = head_repository["owner"]["login"]
            try:
                fork = artifacts[owner_label]
            except KeyError:
                fork = Fork(
                    live_branches={
                        branch["name"]: Branch(info=branch, build=None)
                        for branch in self.list_branches(head_repository["full_name"])
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
        for release in self.api.paginate(f"{API}/repos/{self.default_repo}/releases"):
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
        with self.api.session.get(url, headers=headers, stream=True) as response:
            response.raise_for_status()
            with tempfile.TemporaryFile() as f:
                shutil.copyfileobj(response.raw, f)
                zipfile.ZipFile(f).extractall(dest_dir)

    def download_release(self, release: Release, dest_dir: pathlib.Path) -> None:
        # Downloading a release asset requires setting the Accept header to
        # application/octet-stream, or else you just get the JSON
        # description of the asset back.
        #
        # https://docs.github.com/en/rest/releases/assets?apiVersion=2022-11-28
        #
        # However, setting Accept: application/octet-stream for build
        # artifacts does not work! So we need a different Accept header in
        # the two cases.
        headers = {"Accept": "application/octet-stream"}
        self.download_and_extract(release.asset["url"], dest_dir, headers=headers)

    def render_template(self, name: str, target: pathlib.Path, context: dict) -> None:
        template = self.jinja_env.get_template(name)
        with target.open("w") as f:
            stream = template.stream(context)
            # TemplateStream.dump expects str | IO[bytes]
            # while f is TextIOWrapper[_WrappedBuffer]
            stream.dump(f)  # type: ignore

    def iter_branches(
        self,
        web_artifacts: dict[str, Fork],
        pull_requests: dict[str, PullRequest],
    ) -> Iterator[tuple[str, Branch, PullRequest | None]]:
        for org in lead_sorted(web_artifacts.keys(), self.default_org):
            fork = web_artifacts[org]
            branch_names = fork.live_branches.keys()
            for branch_name in lead_sorted(branch_names, self.default_branch):
                branch = fork.live_branches[branch_name]
                if not branch.build and org != self.default_org:
                    logging.debug(
                        "Ignoring never-built third-party branch %s:%s",
                        org,
                        branch_name,
                    )
                    continue

                pull_request = pull_requests.get(f"{org}:{branch.name}")
                yield org, branch, pull_request

    def run(self) -> None:
        self.get_default_repo_details()

        latest_release = self.get_latest_built_release()

        workflow = self.find_workflow()
        web_artifacts = self.find_latest_artifacts(workflow["id"])
        pull_requests = self.list_pull_requests()

        dest_dir = pathlib.Path(__file__).parent / "_build"
        shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.mkdir(parents=True)

        logging.info("Assembling site at %s", dest_dir)
        have_toplevel_build = False

        if latest_release is not None:
            self.download_release(latest_release, dest_dir)
            have_toplevel_build = True

        items = []
        branches_dir = dest_dir / "branches"
        branches_dir.mkdir()

        for org, branch, pr in self.iter_branches(web_artifacts, pull_requests):
            is_default = branch.name == self.default_branch and org == self.default_org
            item: dict[str, Any] = {
                "org": org,
                "name": branch.name,
                "is_default": is_default,
                "pull_request": pr,
                "build": branch.build,
            }

            if pr and pr["state"] == "closed":
                logging.info(
                    "Ignoring branch %s:%s; newest pull request %s is closed",
                    org,
                    branch.name,
                    pr["url"],
                )
                continue

            if branch.build and not branch.build.artifact["expired"]:
                url = branch.build.artifact["archive_download_url"]
                logging.info("Fetching %s:%s export from %s", org, branch.name, url)

                if is_default and not have_toplevel_build:
                    branch_dir = dest_dir
                    have_toplevel_build = True
                else:
                    # TODO: Use colon form in directory name, avoiding
                    # intermediate directory with no index?
                    branch_dir = branches_dir / org / branch.name
                    branch_dir.mkdir(parents=True)

                self.download_and_extract(url, branch_dir)
                item["relative_path"] = branch_dir.relative_to(
                    branches_dir, walk_up=True
                )

            items.append(item)

        if not have_toplevel_build:
            self.render_template(
                "redirect.html", dest_dir / "index.html", {"target": "branches/"}
            )

        self.render_template(
            "branches.html",
            branches_dir / "index.html",
            {
                "repo_details": self.repo_details,
                "latest_release": latest_release,
                "branches": items,
                "generation_time": dt.datetime.now(tz=dt.timezone.utc),
            },
        )
        shutil.copy("branches.css", branches_dir / "branches.css")

        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"path={dest_dir}\n")

        logging.info("Site assembled at %s", dest_dir)


def check_pages_configuration(session: requests.Session, repo: str) -> None:
    logging.debug("Checking GitHub Pages configuration")

    get_response = session.get(f"{API}/repos/{repo}/pages")
    match get_response.status_code:
        case 404:
            logging.debug("GitHub Pages configuration not found")
        case 200:
            data = get_response.json()
            if data["build_type"] == "workflow":
                logging.debug(
                    "GitHub Pages is configured correctly for this repository"
                )
                return
        case _:
            get_response.raise_for_status()

    raise ConfigurationError(
        "GitHub Pages must be enabled, with the source set to GitHub Actions, in the repository settings.",
        f"Go to https://github.com/{ repo }/settings/pages to fix this.",
    )


def setup_logging() -> None:
    log_format = "+ %(asctime)s %(levelname)s %(name)s: %(message)s"
    date_format = "%H:%M:%S"

    match os.environ.get("DEBUG", "false").lower():
        case "true" | "1":
            level = logging.DEBUG
        case _:
            level = logging.INFO

    logging.basicConfig(level=level, format=log_format, datefmt=date_format)


def amalgamate(
    api: GitHubApi,
    args: argparse.Namespace,
) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    workflow_name = os.environ["WORKFLOW_NAME"]
    artifact_name = os.environ["ARTIFACT_NAME"]
    pages_config = get_pages_config(api.session, repo)

    amalgamate_pages = AmalgamatePages(
        api, repo, pages_config, workflow_name, artifact_name
    )
    amalgamate_pages.run()


def main() -> None:
    api_token = os.environ["GITHUB_TOKEN"]

    setup_logging()

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(title="subcommand", required=True)

    parser_amalgamate = subparsers.add_parser("amalgamate")
    parser_amalgamate.set_defaults(func=amalgamate)

    args = parser.parse_args()
    api = GitHubApi(api_token)

    try:
        args.func(api, args)
    except ConfigurationError as e:
        for message in e.args:
            print(f"::error::{message}")
        raise

    api.session.cache.delete(older_than=dt.timedelta(days=7))


if __name__ == "__main__":
    main()
