# Ensures the project root is importable when running pytest from anywhere.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
