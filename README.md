# Reviewed Memory

A local-first, human-reviewed memory layer for AI agents.

Reviewed Memory converts personal notes into structured long-term memory while preserving provenance and preventing unreviewed model inference from silently becoming trusted user facts.

## Core model

1. Read authorized source evidence.
2. Let an extractor propose atomic candidates.
3. Validate candidates against trusted evidence metadata.
4. Review explicitly before promotion.
5. Preserve promotion lineage and corrections.

The current source adapter is read-only flomo. The memory engine is designed to remain source-agnostic.

## Status

Early OSS release candidate. The repository includes the SQLite memory engine, candidate staging, trust gates, review actions, provenance, profile derivation, lightweight retrieval, and tests.

Run tests with:

```bash
python -m unittest discover -s tests -v
```

## flomo adapter

Copy `.env.example` to `.env` and provide your own credentials locally. Never commit tokens, cookies, raw memo exports, or derived databases containing personal data.

The flomo client only implements read operations. The adapter may need maintenance if flomo changes its web protocol.

## Trust boundary

Extractors may propose candidates, but they cannot directly create active memory. Active fragments must pass the review/promotion path and remain traceable through lineage.

See `SECURITY.md` for reporting and data-handling guidance.

## License

MIT.
