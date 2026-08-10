[private]
list:
    @just --list

# Lint
[group('quality')]
lint:
    uv run ruff check src/imbi_cli tests
    uv run ruff format --check src/imbi_cli tests

# Format
[group('quality')]
fmt:
    uv run ruff format src/imbi_cli tests
    uv run ruff check --fix src/imbi_cli tests

# Type-check imbi_cli
[group('quality')]
mypy:
    uv run mypy src/imbi_cli

# Run the tests
[group('quality')]
test:
    uv run pytest

# Run all checks
[group('quality')]
check: lint mypy test

# Install/sync the development environment
[group('dev')]
sync:
    uv sync

# Run the CLI, e.g. `just run projects list-projects`
[group('dev')]
run *args:
    uv run imbi-cli {{ args }}

# Build the client archive, e.g. `just build-pyz` or `just build-pyz imbicli.pyz`
[group('dev')]
build-pyz *archive:
    uv run imbi-cli build-pyz {{ archive }}

# Install imbi-cli as a uv tool
[group('dev')]
install:
    uv tool install --force --reinstall --from . imbi-cli
