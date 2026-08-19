"""GOaT model package entry point (also the CI smoke test via `uv run main.py`)."""

import sys

if __name__ == "__main__":
    sys.path.insert(0, "src")
    from goat_model.cli import main

    sys.exit(main())
