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
   any of the input artifacts are updated.
2. The `permissions` section: this workflow must be allowed to write to GitHub Pages.
3. The `workflow_name` and `artifact_name` parameters to this action: these are how the
   action finds the artifacts to amalgamate and publish.

You may also want to specify the following to reduce redundant builds:

```yaml
# Cancel any ongoing previous run if the job is re-triggered
concurrency:
  group: ${{ github.workflow }}
  cancel-in-progress: true
```

## Limitations

Currently, any branch for which an artifact is found will be included in the
amalgamated site. This means:

- Branches that have been merged into `main` will be included until their build
  artifacts expire and something else triggers a rebuild of the site;
- Unmerged branches whose artifacts have expired will not be included once the
  site is rebuilt.
