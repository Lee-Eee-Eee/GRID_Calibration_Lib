"""Import the repository-as-package layout when pytest runs from either root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
