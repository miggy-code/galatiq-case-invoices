# Galatiq invoice prototype

- This is Miguel's fork: https://github.com/miggy-code/galatiq-case-invoices.
- Push only to `origin`, and only feature branches. Never push to the assessment
  repository `galatiq-ai/galatiq-case-invoices`, its main, or the scratch clone.
- Keep upstream main/input fixtures intact. No PRs targeting galatiq-ai.
- Read IMPLEMENTATION.md and evals/POLICY.md before changing business behavior.
- Use Conventional Commits. No secrets, external payment calls, or model calls
  unless separately requested. No emails or messages to the assessors.
- Run `uv run pytest -q` and `uv run python -m invoices.evaluate` after changes.
- Working labels are authored expectations, not an official or reviewed answer key.
