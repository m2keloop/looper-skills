"""The portable package uses the inexpensive official summary by default."""
import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_hot_contests.py"
SPEC = importlib.util.spec_from_file_location("portable_sync_modes", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class PortableModeTests(unittest.TestCase):
    def invoke(self, flags):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = [str(SCRIPT), "--report-root", str(root / "reports"),
                    "--snapshots", str(root / "snapshots"), *flags]
            with patch.object(sys, "argv", argv), patch.object(sync, "sync", return_value=0) as run:
                with contextlib.redirect_stderr(io.StringIO()):
                    result = sync.main()
                return result, run

    def test_default_collects_hot_lists_only(self):
        result, run = self.invoke([])
        self.assertEqual(result, 0)
        self.assertTrue(run.call_args.args[0].hot_only)

    def test_four_tiers_require_explicit_mode(self):
        result, run = self.invoke(["--capital-groups"])
        self.assertEqual(result, 0)
        self.assertFalse(run.call_args.args[0].hot_only)

    def test_conflicting_modes_do_not_start_collection(self):
        result, run = self.invoke(["--hot-only", "--capital-groups"])
        self.assertEqual(result, 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
