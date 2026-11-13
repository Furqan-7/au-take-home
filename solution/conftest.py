# Lets pytest import the `ledger` package when run from the repo root or solution/.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
