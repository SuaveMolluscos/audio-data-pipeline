# Contributing

Bug reports, documentation improvements, and focused code contributions are welcome.

## Before opening a pull request

1. Open an issue first for substantial behavioral or architectural changes.
2. Keep changes focused and avoid committing credentials, database files, logs, or third-party audio.
3. Add or update tests for behavior changes.
4. Run the project checks:

   ```bash
   python -m unittest discover -s tests -v
   python -m compileall -q analyzer.py crawler.py db.py main.py monitor.py seeds.py
   python -m pip install -e ".[dev]"
   ruff check .
   ```

5. Describe the motivation, behavior change, and validation in the pull request.

Please follow the terms and rate limits of the external data providers when changing API integrations.
