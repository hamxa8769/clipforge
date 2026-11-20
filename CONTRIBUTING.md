# Contributing to ClipForge

## Ground rules

1. **Contract-first.** The tests in `tests/` define the module APIs
   (`ARCHITECTURE.md` §7 lists them). Implement to the contract. If an API
   must change, change the test, the implementation, and the doc together
   in one PR.
2. **Approval-first, always.** No publishing path may bypass the approval
   queue. No "auto-post everything" flag, in code or config.
3. **Official APIs only.** No Selenium / cookie uploaders, no unofficial
   TikTok uploader, no Postiz AGPL code. See the DO-NOT list in
   [README.md](README.md#do-not-list).
4. **`DRY_RUN=true` stays the default.** A PR that flips the default or
   adds a live-publish path without a dry-run equivalent does not merge.
5. **No secrets.** No API keys, tokens, keystores, or `.env` files in any
   commit. Use `.env.example` placeholders only.

## Workflow

```bash
git clone https://github.com/hamxa8769/clipforge.git
cd clipforge
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install   # ruff + secret/large-file guards

# work on a branch, then:
python3 -m pytest tests/ -q
ruff check . && ruff format --check .
python3 scripts/smoke_test.py
```

- Keep PRs small and single-purpose; describe what platform behaviour
  (if any) the change relies on, with a source link.
- New platform limits go in `ARCHITECTURE.md` §6 with a `VERIFY_LIVE`
  note when sources conflict — never hard-code a contested number as fact.
- New tests for new behaviour; contract tests in `tests/unit`, API flow
  tests in `tests/integration`, end-to-end in `scripts/smoke_test.py`.

## Code style

- Python 3.11+, `ruff` (line length 100) for lint + format, `mypy` for
  type checking. CI runs `ruff check`, `ruff format --check`, and pytest.
- Type hints on public functions; docstrings on modules that define a
  contract.
- No new heavy dependency without a note in `pyproject.toml` explaining
  why it is required vs. optional (see the `transcribe` / `vision` /
  `youtube` extras).

## Reporting issues

Use GitHub Issues. For security issues, follow [SECURITY.md](SECURITY.md) —
do not open a public issue for a vulnerability.
