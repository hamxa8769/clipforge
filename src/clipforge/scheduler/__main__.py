"""CLI entry: ``python -m clipforge.scheduler [--live]`` — dry-run by default."""

from clipforge.scheduler import main

if __name__ == "__main__":
    raise SystemExit(main())
