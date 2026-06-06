"""Ensure the repo root is importable so `tests._fakes` and `goalclaw` resolve."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
