# Contributing

Contributions are welcome as pull requests on GitHub.

## Before you start

1. Read `README.md` for how to run the service from source.
2. Open an issue first for anything larger than a small fix, so we can agree on the shape.

## Making a change

- Set up with `uv sync --extra dev`, then run `uv run pytest -q` before you open a pull request. It must pass.
- One logical change per commit, with a plain-sentence message that says what changed and why.
- Match the surrounding code: its naming, comment density and idiom.
- Sign off every commit (`git commit -s`). That certifies the
  [DCO 1.1](https://developercertificate.org/): you wrote the change or have the right to submit it
  under the project's MIT licence.
- Never commit `.env`, databases (`data/`, `*.db`), tokens or machine-specific paths.
