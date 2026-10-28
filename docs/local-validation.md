# Validation without GitHub Actions

GitHub Actions is disabled to prevent runner minutes and Actions artifact storage use.
The previous workflow is preserved under `docs/retired-actions/` as a non-runnable
reference. Do not re-enable it or move it back without the owner's approval.
GitHub Releases are separate from Actions artifacts; existing assets are retained.

Run from a clean checkout with no production .env files, data, or credentials.
Run `bash scripts/validate-local.sh` before proposing a merge/deployment. Record
commit SHA, runtime versions, commands, results, and skipped checks in the review.
These commands do not publish or deploy. Hosting/deployment settings are unchanged.
No automated replacement gate has been configured; a human must review validation
before merging. Do not bypass a required status check: report it for a separate decision.

Use Python 3.11+ (prefer the repository's .python-version), uv and Docker.
The script creates a fresh Postgres 16 container bound only to localhost:55432,
matching smoke_test.py. An occupied port is an error; never reuse an existing DB.
It disables dotenv loading and runs uv sync --locked followed by the smoke test.
