import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.dual_ai.web_batch import HumanRequired
from tools.dual_ai.web_environment import backend_python


class BackendEnvironmentTests(unittest.TestCase):
    def test_prefers_existing_project_venv_with_fastapi(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / ".venv/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            result = SimpleNamespace(returncode=0, timed_out=False,
                                     stdout=json.dumps({"python": str(python), "fastapi": "existing"}))
            with patch("tools.dual_ai.web_environment.run_command", return_value=result) as run:
                environment = backend_python(root)
            self.assertEqual(environment["executable"], str(python))
            self.assertEqual(run.call_args.args[0][0], str(python))
            self.assertIn("import json,sys,fastapi", run.call_args.args[0][-1])
            self.assertNotIn("pip", run.call_args.args[0])

    def test_missing_dependency_stops_without_install_or_model_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            result = SimpleNamespace(returncode=1, timed_out=False, stdout="")
            with patch("tools.dual_ai.web_environment.run_command", return_value=result) as run:
                with self.assertRaises(HumanRequired):
                    backend_python(Path(folder))
            self.assertTrue(run.called)
            self.assertTrue(all(args.args[0][1] == "-c" for args in run.call_args_list))
            self.assertTrue(all("pip" not in args.args[0] for args in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
