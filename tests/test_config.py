import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis import Config  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_attr_and_item_access(self):
        cfg = Config({"provider": "mock", "agent": {"max_steps": 4}})
        self.assertEqual(cfg.provider, "mock")
        self.assertEqual(cfg["agent"]["max_steps"], 4)
        self.assertEqual(cfg.get("missing", "default"), "default")

    def test_merge_overrides(self):
        cfg = Config({"a": 1, "b": 2}).merge({"b": 3, "c": 4})
        self.assertEqual(cfg.to_dict(), {"a": 1, "b": 3, "c": 4})

    def test_fromfile_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"provider": "openrouter", "workdir": "wd"}, handle)
            path = handle.name
        try:
            cfg = Config.fromfile(path)
            self.assertEqual(cfg.provider, "openrouter")
            self.assertEqual(cfg.workdir, "wd")
        finally:
            os.remove(path)

    def test_fromfile_python(self):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write("provider = 'mock'\n_hidden = 1\nagent = {'max_steps': 9}\n")
            path = handle.name
        try:
            cfg = Config.fromfile(path)
            self.assertEqual(cfg.provider, "mock")
            self.assertEqual(cfg.agent["max_steps"], 9)
            self.assertNotIn("_hidden", cfg)  # underscored vars are excluded
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
