# Amalgamate Pages

There is no built-in way to publish a separate GitHub Pages site for each branch
of a project. This action provides a way to simulate it, by rolling up the build
artifacts from different branches into a single site. The default branch is
placed at the root of the site as normal; other branches live in a `/branches/`
directory, with an index.

## Example

[Candy Collective](https://github.com/endlessm/candy-collective) is used in
collaborative game-making programs, where (among other things) participants can
gain experience with contributing to a community project via Git and GitHub.

The `main` branch of the game can be played at
<https://endlessm.github.io/candy-collective/>. Thanks to this action,
work-in-progress branches can be previewed at
<https://endlessm.github.io/candy-collective/branches/>.

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
concurrency:
  group: ${{ github.workflow }}
  cancel-in-progress: true
publish:
    name: Publish all branches to GitHub Pages
    runs-on: ubuntu-latest
    steps:
      - uses: endlessm/amalgamate-pages@v1
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
   action finds the artifacts to amalgamate and publish.

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
