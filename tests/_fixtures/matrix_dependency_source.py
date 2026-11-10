"""Follow-up-only real Matrix dependency source fixture; no install or native donor modules."""
import sys
from pathlib import Path

SOURCE = Path('/Users/aiserver/.hermes/cache/scratch/v0216/out/matrix-dependency-source/site-packages')
sys.path.insert(0, str(SOURCE))
from mautrix.client import SyncStream
import mautrix

assert Path(mautrix.__file__).is_relative_to(SOURCE)
assert SyncStream.INVITED_ROOM.value != SyncStream.JOINED_ROOM.value
