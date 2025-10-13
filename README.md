# Amalgamate Pages

There is no built-in way to publish a separate GitHub Pages site for each branch
of a project. This action provides a way to simulate it, by rolling up the build
artifacts from different branches into a single site.

The action finds the latest build artifact for all live branches in the repo,
plus any builds for pull requests from forks, and places them beneath a
`branches/` directory of the site, with an index in that directory.

The newest-available asset attached to a published release (which may be a
pre-release) is placed at the root of the site. If no such asset is found, the
build artifact for the default branch is placed at the root of the site. If no
such artifact is available, the root of the site redirects to the index of
branches.

## Example

[Threadbare](https://github.com/endlessm/threadbare) is a game by Endless Access.
It is used in game-making programs, where (among other things) participants can
gain experience with contributing to a community project via Git and GitHub.

You can play the latest pre-release of the game at
<https://endlessm.github.io/threadbare/>, and preview work-in-progress branches
at <https://endlessm.github.io/threadbare/branches/>.

## Usage

Suppose you currently have the following workflow:

```yaml
# .github/workflows/export.yml
name: Export Web Build
on:
  pull_request:
  push:
    branches:
      - main
jobs:
  build:
    # ... some project-specific build steps ...

    - name: Upload web build
      uses: actions/upload-pages-artifact@v3
      with:
        name: web
        path: build/web

    - name: Deploy to GitHub Pages
      uses: actions/deploy-pages@v4
      with:
        artifact_name: web
```

Remove the `actions/deploy-pages` step from your export workflow, and change the
`actions/upload-pages-artifact` step to `actions/upload-pages-artifact`:

```yaml
# .github/workflows/export.yml
name: Export Web Build
on:
  pull_request:
  push:
    branches:
      - main
jobs:
  build:
    # ... some project-specific build steps ...
    - name: Upload web build
      uses: actions/upload-artifact@v4
      with:
        name: web
        path: build/web
```

Add a new workflow looking like this:

```yaml
# .github/workflows/publish.yml
name: "Publish to GitHub Pages"
on:
  delete:
  workflow_run:
    workflows:
      # This must match your build workflow's name
      - "Export Web Build"
    types:
      - completed
permissions:
  contents: read
  pages: write
  id-token: write
  # Enable commenting on PRs with a link
  pull-requests: write
  # Enable setting commit build status with a link
  statuses: write
concurrency:
  group: ${{ github.workflow }}
  cancel-in-progress: true
publish:
    name: Publish all branches to GitHub Pages
    runs-on: ubuntu-latest
    steps:
      - uses: endlessm/amalgamate-pages@v2
        with:
          # These must match the workflow and artifact names from your build workflow
          workflow_name: "Export Web Build"
          artifact_name: "web"
```

The important elements are:

1. The `workflow_run` trigger: this causes the publish workflow to run whenever
   any of the input artifacts are updated. (The `delete` trigger causes the
   workflow to run when a branch is deleted.)

2. The `permissions` section: this workflow must be allowed to write to GitHub Pages.

3. The `workflow_name` and `artifact_name` parameters to this action: these are how the
   action finds the artifacts to amalgamate and publish. The `artifact_name` is
   also used when scanning for release assets: for example, if `artifact_name`
   is `web`, then the release asset is expected to have a filename ending with
   `-web.zip`.

4. The `concurrency` rule reduces duplicate runs when the workflow is triggered
   by several events in quick succession; in particular, merging a pull request
   and deleting the source branch will cause the workflow to be triggered once
   by the branch deletion and again by the `main` branch being built after the
   merge.

## Limitations

Any branch for which an artifact is found will be included in the amalgamated
site, unless there is a closed pull request for the branch. In particular this
means that if you have a long-lived branch which is not regularly built, at some
point its build artifact will expire and will not be included in the amalgamated
site. Instead, the branches index will show the date on which it expired.
Manually triggering the build workflow on that branch, or pushing a new commit
to trigger a build, will restore it.

Our primary use-case for this tool is for web builds of Godot game projects. In
principle, it is generic enough to work for any project, and patches are welcome
to improve this so long as they do not undermine our primary use! One current
assumption is that we can place arbitrary files into a `branches` directory at
the root of the website without interfering with the default web build.
