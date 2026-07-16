# Releasing

Publishing uses PyPI **trusted publishing** (OIDC) — no API tokens stored
anywhere. One-time setup, then every GitHub release publishes automatically.

## One-time setup (repo owner, ~2 minutes)

1. Log in to https://pypi.org and go to
   **Your account → Publishing → Add a new pending publisher**.
2. Fill in:
   - PyPI project name: `immortalize`
   - Owner: `awtoau`
   - Repository name: `immortalize`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. In the GitHub repo: **Settings → Environments → New environment** named
   `pypi` (no secrets needed; optionally add yourself as a required reviewer
   so releases need a manual click).

## Every release

1. Bump `version` in `pyproject.toml` and in
   `src/immortalize/__init__.py` (`__version__`).
2. Commit, tag, push:
   ```
   git tag v0.1.0
   git push origin main --tags
   ```
3. Create a GitHub release from the tag (**Releases → Draft a new release**).
   Publishing the release triggers `publish.yml`, which builds the sdist +
   wheel and uploads to PyPI.
