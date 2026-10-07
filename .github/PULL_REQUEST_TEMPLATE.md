## Summary

<!-- What changes for the lawyer, the firm or the operator, and why. Link the issue. -->

## Tests

<!-- Say what you ran. A pull request should pass all of these; say which you could not run and why. -->

- [ ] `.venv/bin/python -m ruff check src tests`
- [ ] `.venv/bin/python -m pytest -q` (SQLite)
- [ ] `LRA_TEST_BACKEND=d1 .venv/bin/python -m pytest -q` (the D1 adapter)
- [ ] `.venv/bin/python -m evals.run_checks` still meets its target (if checks changed)
- [ ] `npm run check && npm test` in `cloudflare/` or `cloudflare/dms-mcp/` (if a Worker changed)
- [ ] New behaviour has a test; a new check is tested both firing and staying silent

## What this does not do, or was not tested against

<!-- For example: not opened in Word, not run against the live model, not run on Cloudflare. -->

## Checklist

- [ ] No real client document, email, matter or personal data is in this pull request, its tests or its fixtures
- [ ] No new dependency under the GPL or AGPL (`LICENSING.md`)
- [ ] I have signed the CLA (`CLA.md`), or will when the bot asks
