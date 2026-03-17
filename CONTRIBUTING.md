# Testing changes

We have an [amalgamate-pages-test](https://github.com/endlessm/amalgamate-pages-test)
repository which contains a small Godot project that is configured to use
the `test` branch of this action, and a small number of open and closed pull
requests. You can use this repository to test changes locally, or to test
changes on GitHub Actions.

## Local testing

This tool is developed on Linux. Your mileage may vary on other platforms.

This tool is developed using [uv](https://github.com/astral-sh/uv), a Python
project manager. Install this if you haven't already.

Test the amalgamation process like this:

```bash
GITHUB_REPOSITORY=endlessm/amalgamate-pages-test \
WORKFLOW_NAME="Build and Export Game" \
ARTIFACT_NAME="web" \
uv run godoctopus.py amalgamate
```

Adjust `GITHUB_REPOSITORY`, `WORKFLOW_NAME` and `ARTIFACT_NAME` to taste.

> [!TIP]
> If you have the [`gh` CLI tool](https://cli.github.com/) installed and
> configured, a GitHub API token will be fetched from it automatically. Otherwise,
> you must set the `GITHUB_TOKEN` environment variable.

The output will be in the `_build` directory. Serve this using a local web server:

```bash
python3 -m http.server -d _build
```

When run locally, HTTP responses are cached to
`$XDG_CACHE_DIR/godoctopus-cache.sqlite`, which usually means
`~/.cache/godoctopus-cache.sqlite`.

## Testing on GitHub Actions

If you work at Endless Access, you can push work-in-progress changes to `test`,
then trigger builds on https://github.com/endlessm/amalgamate-pages-test.

If you do not work at Endless Access:

1. Fork this repository
2. Fork https://github.com/endlessm/amalgamate-pages-test
3. Enable GitHub Actions and GitHub Pages on your fork
4. Adjust `.github/workflows/export.yml` in your fork to point to your fork of
   `amalgamate-pages`
5. Push changes to a `test` branch in your `amalgamate-pages` fork

## Linting

We use [pre-commit](https://pre-commit.com/) to run various checks, including
formatting the code with `black`.

You may like to install `pre-commit` using `uv`:

```bash
uv tool install pre-commit
```

However you install it, install its hooks with:

```bash
pre-commit install
```
