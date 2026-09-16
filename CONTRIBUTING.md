# Contributing

Contributions are welcome while the project is still early.

Before opening a pull request:

1. Keep raw personal note content, credentials, cookies, and local databases out of commits.
2. Preserve the review boundary: extractors may propose candidates but must not bypass promotion lineage.
3. Add or update tests for trust-boundary and persistence changes.
4. Run `python -m unittest discover -s tests -v`.
5. Keep changes focused; explain any migration or compatibility impact.

For security-sensitive findings, use the process in `SECURITY.md` instead of a public issue.
