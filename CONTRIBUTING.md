# Contributing

Thanks for helping improve `diagram-grill`. The project is in public-alpha
development and favors small, reviewable changes backed by observable tests.

## Development setup

Requirements:

- Python 3.9 or newer
- Graphviz with `dot` available on `PATH`

Create a virtual environment and install the pinned Python dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r skills/diagram-grill/requirements.lock
```

Run the test suite before submitting a change:

```bash
python -m unittest discover -s tests -v
```

## Change discipline

- Keep commits focused and leave the repository passing after each commit.
- Add a regression test for behavioral changes.
- Do not add provider icons, copied upstream code, remote asset downloads, or
  hidden telemetry.
- Do not claim client compatibility without an end-to-end result that records
  the client and runtime versions.
- Update third-party notices when redistributing third-party material.

Use conventional commit subjects such as `feat:`, `fix:`, `test:`, `docs:`,
and `chore:`.
