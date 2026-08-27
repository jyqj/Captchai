# Contributing

Thanks for contributing to CaptchAI.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
playwright install --with-deps chromium
```

`requirements.txt` is intentionally runtime-only. Development and documentation
tooling live in `requirements-dev.txt` and `requirements-docs.txt` so production
images do not carry test or MkDocs dependency trees.

## Run the project locally

```bash
python main.py
```

## Validate changes

The full suite includes Redis-backed persistence tests. Start a disposable Redis
instance before running it locally:

```bash
docker run --rm -p 6379:6379 redis:7.4-alpine
pytest -q
```

Run type checks:

```bash
pyright
```

Build docs in an environment with the documentation dependency set installed:

```bash
pip install -r requirements-docs.txt
mkdocs build --strict
```

## Contribution guidelines

- Keep changes aligned with the implemented task types and documented behavior.
- Do not add secret values, personal endpoints, or account-specific configuration to the repository.
- Prefer small, reviewable pull requests.
- Update docs when behavior changes.
- Keep examples copy-pasteable and placeholder-based.
- Avoid overstating compatibility or production guarantees.

## Pull requests

A good pull request usually includes:

- a concise summary of the change
- why the change is needed
- tests or validation notes
- documentation updates if relevant

## Documentation style

This repository aims for documentation that is:

- clear
- practical
- implementation-aware
- safe for public distribution

If you add deployment examples, use placeholders instead of real secrets or private URLs.
