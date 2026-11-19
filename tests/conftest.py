"""Point the app at a throwaway data dir before any test imports app.db."""
import os
import tempfile

os.environ["BMS_DATA_DIR"] = tempfile.mkdtemp(prefix="bms-test-")
