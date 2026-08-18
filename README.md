# imbi-cli

Command-line interface for [Imbi](https://github.com/AWeber-Imbi/imbi).

Client and commands are generated at runtime, in memory, from
`$IMBI_URL/openapi.json` via
[openapi-python-client](https://github.com/openapi-generators/openapi-python-client).
One command group per tag, one command per operation, parameters typed
per the spec.

Each invocation regenerates. Spec source: `--url`/`IMBI_URL` or a
saved file via `--spec-file`/`IMBI_SPEC_FILE`.

The spec is rendered into Python and executed, so point `imbi-cli` only
at an Imbi instance you trust — the same one you trust with your API
token.

## Install
This will install the `imbi-cli` command.

```sh
uv tool install git+https://github.com/AWeber-Imbi/imbi-cli
```

## Setup

```sh
export IMBI_URL=https://imbi.example.com
export IMBI_TOKEN=your-api-key-here
```

| Variable             | Required | Description                                |
|----------------------|----------|--------------------------------------------|
| `IMBI_URL`           | yes      | Base URL                                   |
| `IMBI_TOKEN`         | yes      | API token                                  |
| `IMBI_SPEC_FILE`     | no       | Saved spec path                            |
| `IMBI_ORGANIZATION`  | no       | Default organization slug                  |
| `IMBI_VERBOSE`       | no       | Same as `-v`                               |
| `IMBI_ENV_FILE`      | no       | Dotenv file with the above                 |

`--version` runs standalone; with a spec, names the Imbi version too.

Pin a spec (offline, fixed version):

```sh
curl -o ~/.imbi-openapi.json "$IMBI_URL/openapi.json"
export IMBI_SPEC_FILE=~/.imbi-openapi.json
```

### Dotenv file

```sh
# ~/.imbi.env
IMBI_URL=https://imbi.example.com
IMBI_TOKEN=your-api-key-here
IMBI_ORGANIZATION=aweber
```

```sh
export IMBI_ENV_FILE=~/.imbi.env
imbi-cli projects list-projects
```

Accepts `export` prefixes and quoted values. Environment vars win.

## Usage

```sh
imbi-cli --help
imbi-cli projects --help
imbi-cli projects get-project --org-slug aweber --project-id 7
```

```sh
imbi-cli --version
imbi-cli build-pyz
./imbi-cli_v2.13.1.pyz projects list-projects
```

`build-pyz`: executable `.pyz` with CLI + generated client baked in.
Default name carries the Imbi version (`imbi-cli_v2.13.1.pyz`, else
`imbi-cli_unknown.pyz`). Needs `httpx` and `attrs`. `--version` and
`--help` name the Imbi version it was built from.

Options mirror operation parameters:

- Fixed values shown: `--granularity {day,hour,raw}`
- Booleans: `--flag` / `--no-flag`
- Multi-value repeats: `--status open --status closed`

`-v`: log each request/response (method, URL, status, redacted
headers) to stderr. `--url`: override target for one call.

JSON bodies: `--body '<json>'`, `--body-file <path>`, or `--body-stdin`:

```sh
echo '{"name": "critical", "color": "#ff0000"}' \
    | imbi-cli tags create-tag --org-slug aweber --body-stdin
```

## Development

Tasks live in the [justfile](justfile):

```sh
Available recipes:
    [dev]
    install   # Install imbi-cli as a uv tool
    run *args # Run the CLI, e.g. `just run projects list-projects`
    sync      # Install/sync the development environment

    [quality]
    check     # Run all checks
    fmt       # Format
    lint      # Lint
    mypy      # Type-check imbi_cli
    test      # Run the tests
```

`tests/fixtures/openapi.json`: captured Imbi 2.13.1 spec for tests.
`src/imbi_cli/generator/openapi.py`: pre-generation spec transforms —
shortened operationIds, most-specific tag per operation, non-nullable
path parameters, dropped colliding schema titles.

Client exists at runtime; annotations through it are `typing.Any`.
`mypy` checks the rest.
