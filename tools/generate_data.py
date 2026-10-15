#!/usr/bin/env python3
"""Regenerate seeds/*.csv (and seeds/SEED) for your assigned dataset seed.

Thin wrapper: the generator itself lives in grader/generate_data.py so the
grader can regenerate your data and check the committed CSVs are unedited.

  python3 tools/generate_data.py --seed 1234      # or: make data SEED=1234
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "grader"))

from generate_data import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
