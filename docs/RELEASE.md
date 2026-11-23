# Release process (V-11)

**This document is a runbook, not permission for later gates.** The GitHub repository
`miles990/ras-anything` is **public**. Tag `v0.2.0` and the matching GitHub Release are
the first public release and were authorized for this version. **Do not upload to PyPI or npm** unless Alex explicitly approves that specific step. Anyone can *build* and run
`twine check` locally; registry publishing is a human decision.

Current in-tree version: `0.2.0` (see `pyproject.toml` and `ras_anything.__version__`).

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

## 2. Repository visibility

The repository was made public on 2026-11-23. Do not change visibility without Alex's approval.

Rollback (if ever needed): Settings → Change visibility → Private (know that stargazers/forks may already exist).

## 3. Tag and GitHub Release

`v0.2.0` is the first public tag. For a later version, after tests are green:

```sh
git checkout main
git pull --ff-only origin main
git tag -a vX.Y.Z -m "ras-anything X.Y.Z"
# Push the tag and create the GitHub Release only after Alex approves that version:
# git push origin vX.Y.Z
# gh release create vX.Y.Z --title "ras-anything X.Y.Z" --notes-file notes.md
```

Rollback: `git push origin :refs/tags/vX.Y.Z` and `git tag -d vX.Y.Z` (only if no one has
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

- Confirm `pipx install git+https://github.com/miles990/ras-anything`
  installs `0.2.0` and `ras-anything doctor` runs.
- The README must not say the repo is private. Do not mention a PyPI package name until
  that gate is approved and the upload has succeeded.
- A GitHub Release from tag `v0.2.0` (not a draft) is part of this version. Later releases
  still need Alex's approval for that tag.

## Record of local checks

Local `python -m build` + `twine check` results belong in `wow/proof/` (or a release
checklist comment), not as committed wheels. This file must not be used as a script;
there is no `./release.sh`.
