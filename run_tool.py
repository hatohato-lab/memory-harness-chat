"""Command-line entry without installation: python run_tool.py --help"""
from memory_harness.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
