# Release process (V-11)

**This document is a runbook, not permission.** The GitHub repository `miles990/ras-anything`
is **private**. Do not make it public, do not tag a release, and do not upload to PyPI or npm
unless Alex explicitly approves that specific step (the "Alex gate"). Anyone can *build* and
run `twine check` locally; publishing is a human decision.

Current in-tree version: `0.1.0` (see `pyproject.toml` and `ras_anything/__version__`).
The first public tag contemplated by this runbook is **v0.2.0**.

The Node agent ships **inside the Python wheel** as package data (`ras_anything/lang/node_agent/`,
plus the Go agent under `ras_anything/lang/go_agent/` and the Java agent under `ras_anything/lang/java_agent/`). A separate npm package is **not required**
and must not be published unless a later, explicit decision says otherwise.

## 0. Preconditions (run these; they do not publish anything)

- [ ] Working tree is clean and `git status` matches the commit you intend to release.
- [ ] `bash wow/work/s/ptest.sh` is green (record the test count).
- [ ] `./install.sh` (or `make install`) has been re-run in a **fresh temporary `$HOME`** and
      `ras-anything --version`, `ras-anything doctor`, both skill paths, and `ras-anything mcp`
      initialize all succeed. Then `./install.sh --uninstall` cleans up.
- [ ] No secrets, tokens, private keys, or builder absolute paths (`/workspace/`, `/home/…`)
      in committed `examples/**/*.json`, `.ras/out/`, or docs.
- [ ] `README.md` Install / CLI / agent sections, `docs/RELEASE.md`, and `LICENSE` are current.
- [ ] Version bump is agreed: set `version` in `pyproject.toml`, `ras_anything/__init__.py`,
      `.claude-plugin/plugin.json` (and changelog, if you add one) to the same `X.Y.Z`.
      For the first public release that is `0.2.0`.

## 1. Local package check (safe — no upload)

From a clean checkout, in a throwaway venv (do **not** commit `dist/` or `build/`):

```sh
python3 -m venv /tmp/ras-rel-venv
/tmp/ras-rel-venv/bin/python -m pip install --upgrade build twine
# build sdist + wheel into ./dist
/tmp/ras-rel-venv/bin/python -m build
/tmp/ras-rel-venv/bin/twine check dist/*
```

`twine check` must report `PASSED` for every artifact. Confirm the wheel contains
`ras_anything/lang/node_agent/*.cjs`, `ras_anything/lang/go_agent/*/*.go` and `ras_anything/lang/java_agent/*/*.java`
(`unzip -l dist/ras_anything-*.whl | grep -E 'node_agent|go_agent|java_agent'`).

You may delete `dist/` and `build/` afterwards. **Stop here** unless Alex has approved
the next section.

## 2. Make the repository public (Alex gate — do not run)

Only after an explicit "make `miles990/ras-anything` public" from Alex:

1. Recheck step 0 (history, secrets, license).
2. On GitHub: Settings → General → Danger Zone → Change repository visibility → Public.
   (Equivalent CLI, also gated: `gh repo edit miles990/ras-anything --visibility public`.)
3. Confirm the default branch is `main`, that Issues/Discussions settings are intended,
   and that no private fork-only workflows were relying on privacy.
4. Record the time and the acting user in the release notes.

Rollback: Settings → Change visibility → Private (know that stargazers/forks may already exist).

## 3. Tag v0.2.0 (Alex gate — do not run without approval)

```sh
git checkout main
git pull --ff-only origin main
git tag -a v0.2.0 -m "ras-anything 0.2.0"
# Push the tag only after Alex approves:
# git push origin v0.2.0
```

Rollback: `git push origin :refs/tags/v0.2.0` and `git tag -d v0.2.0` (only if no one has
already consumed the tag). Never `--force` a published tag.

## 4. Publish to PyPI (Alex gate — do not run)

Preferred: GitHub → PyPI **trusted publishing** (OIDC) on tag `v*`, so no API token lives
in the repo. Alternative: a project-scoped PyPI token in a local environment variable,
never committed.

```sh
# AFTER Alex approval, and after twine check has already PASSED:
# Trusted publishing: let the configured GitHub Action publish the tag.
# Or, manually (token stays in the environment, never in git):
#   twine upload dist/*
```

Checks immediately before upload:

- The version on PyPI is not already taken (`0.2.0` must be new).
- `twine check dist/*` still passes on the exact artifacts you will upload.
- You are uploading to `pypi.org`, not a typo-squatted index.

Rollback: PyPI cannot delete a release; you may **yank** a bad file
(`https://pypi.org/manage/project/ras-anything/`) and publish a patch (`0.2.1`).
Yank does not remove already-downloaded wheels.

## 5. npm (not in this release)

Do **not** publish `@…/ras-anything` or a Node agent package to npm. The Node (and Go)
agents travel with the Python sdist/wheel. Revisit only with a written decision.

## 6. After a public release (also gated)

- Confirm `pipx install ras-anything` (or `pipx install git+https://github.com/miles990/ras-anything`)
  installs `0.2.0` and `ras-anything doctor` runs.
- The README Install snippet can then mention the PyPI name; until then it must keep saying
  the GitHub repo is private and must not be published.
- Optionally draft a GitHub Release from tag `v0.2.0` (notes + checksums of `dist/*`).
  Creating the GitHub Release is also an Alex-gated step.

## Record of local checks

Local `python -m build` + `twine check` results belong in `wow/proof/` (or a release
checklist comment), not as committed wheels. This file must not be used as a script;
there is no `./release.sh`.
