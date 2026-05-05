# Tests

Backend test platform for Jobs AI. Three layers: unit (`tests/unit/`),
integration (`tests/integration/`), shared fixtures (`tests/fixtures/`).
Frontend E2E is intentionally out of scope.

## Run

```powershell
# one-time
pip install -r requirements.txt -r requirements-test.txt

# everything
pytest

# fast feedback loop
pytest tests/unit -m unit -x

# integration only
pytest tests/integration -m integration

# coverage report
pytest --cov=pipeline --cov=app --cov=session_store --cov=auth_utils --cov-report=term-missing
```

## Layout

```
tests/
├── conftest.py        # shared fixtures: tmp_db, fastapi_client, patched_provider, ...
├── fakes.py           # FakeProvider, FakeJobSource, make_raw_job
├── stripe_helpers.py  # hand-rolled Stripe webhook signing for billing tests
├── unit/              # pure-Python tests, no FastAPI, no I/O outside tmp dirs
└── integration/       # fastapi.testclient against an isolated SQLite store
```

## Adding a test

1. Pick the right directory by what your test touches:
   - **unit** if it tests one module's logic in isolation
   - **integration** if it goes through `app.py` routes or the full Flask
     dashboard
2. Use the `unit` or `integration` marker on the test class/function so it
   shows up under the right `-m` filter.
3. Use existing fixtures from `conftest.py` rather than rolling your own
   SQLite/TestClient setup.

## Why these constraints

- **No live LLM calls.** Use `patched_provider` to inject `FakeProvider`,
  or `respx` to mock the underlying HTTP for the real Anthropic/Ollama
  classes when the test needs to exercise SDK behavior.
- **No live job-source HTTP.** Use `fake_source` to clear the registry and
  register a `FakeJobSource` with the rows you want.
- **No live Stripe.** Use `tests/stripe_helpers.py` to hand-roll signed
  webhook payloads.
- **Cross-platform.** Always use `pathlib.Path`, never raw `/` or `\`.
- **Daemon-thread isolation.** `JOBS_AI_DISABLE_INGESTION=1` and
  `JOBS_AI_SKIP_MIGRATION=1` are set automatically by `conftest.py` before
  any project import — don't override them unless your test specifically
  exercises ingestion or migration.

## Test-hostile bits to know about

- `pipeline/config.py` migrates the legacy DB path at import time. The
  `JOBS_AI_SKIP_MIGRATION` env guard keeps that from touching real data.
- `OllamaProvider.OLLAMA_URL` is now per-instance (not class-level) so
  `monkeypatch.setenv("OLLAMA_URL", ...)` works.
- `app.py:_make_provider` checks `_PROVIDER_OVERRIDE` first; the
  `patched_provider` fixture sets it.
- `app.py:_start_ingestion` short-circuits on `JOBS_AI_DISABLE_INGESTION`,
  avoiding the 60s parallel backfill under `TestClient`.
