"""Entry point: `python -m imbi_cli` and the `imbi-cli` console script."""

from imbi_cli import app


def main() -> None:
    app.main()


if __name__ == "__main__":
    main()
