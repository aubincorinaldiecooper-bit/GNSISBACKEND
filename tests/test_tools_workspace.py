import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis.tools.workspace import (  # noqa: E402
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    RunCommandTool,
    WorkspaceBoundaryError,
    WriteFileTool,
    resolve_in_root,
    workspace_tools,
)


class ResolveInRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        with open(os.path.join(self.root, "a.txt"), "w") as f:
            f.write("hello")
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "sub", "b.txt"), "w") as f:
            f.write("world")

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_path_resolves(self):
        self.assertTrue(resolve_in_root(self.root, "a.txt").endswith("a.txt"))

    def test_nested_relative_path_resolves(self):
        self.assertTrue(resolve_in_root(self.root, "sub/b.txt").endswith("b.txt"))

    def test_parent_traversal_rejected(self):
        with self.assertRaises(WorkspaceBoundaryError):
            resolve_in_root(self.root, "../outside.txt")

    def test_deep_parent_traversal_rejected(self):
        with self.assertRaises(WorkspaceBoundaryError):
            resolve_in_root(self.root, "sub/../../outside.txt")

    def test_absolute_path_outside_root_rejected(self):
        with self.assertRaises(WorkspaceBoundaryError):
            resolve_in_root(self.root, "/etc/passwd")

    def test_absolute_path_inside_root_allowed(self):
        inside = os.path.join(self.root, "a.txt")
        self.assertEqual(resolve_in_root(self.root, inside), os.path.realpath(inside))


class ToolBoundaryTests(unittest.TestCase):
    """Every tool must refuse to touch paths outside its root, not just resolve_in_root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_file_rejects_escape(self):
        result = ReadFileTool(self.root).run(path="../../etc/passwd")
        self.assertTrue(result.is_error)
        self.assertIn("outside", result.content)

    def test_write_file_rejects_escape(self):
        result = WriteFileTool(self.root).run(path="../evil.txt", content="x")
        self.assertTrue(result.is_error)

    def test_edit_file_rejects_escape(self):
        result = EditFileTool(self.root).run(path="../evil.txt", old_string="a", new_string="b")
        self.assertTrue(result.is_error)

    def test_list_files_rejects_escape(self):
        result = ListFilesTool(self.root).run(path="..")
        self.assertTrue(result.is_error)


class ReadWriteEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_then_read_round_trips(self):
        WriteFileTool(self.root).run(path="new.txt", content="hello world")
        result = ReadFileTool(self.root).run(path="new.txt")
        self.assertEqual(result.content, "hello world")

    def test_write_creates_parent_directories(self):
        WriteFileTool(self.root).run(path="a/b/c.txt", content="deep")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a", "b", "c.txt")))

    def test_read_missing_file_errors(self):
        result = ReadFileTool(self.root).run(path="missing.txt")
        self.assertTrue(result.is_error)

    def test_edit_replaces_unique_match(self):
        WriteFileTool(self.root).run(path="f.txt", content="foo bar baz")
        result = EditFileTool(self.root).run(path="f.txt", old_string="bar", new_string="qux")
        self.assertFalse(result.is_error)
        self.assertEqual(ReadFileTool(self.root).run(path="f.txt").content, "foo qux baz")

    def test_edit_rejects_ambiguous_match(self):
        WriteFileTool(self.root).run(path="f.txt", content="foo foo foo")
        result = EditFileTool(self.root).run(path="f.txt", old_string="foo", new_string="bar")
        self.assertTrue(result.is_error)
        self.assertIn("3", result.content)

    def test_edit_rejects_missing_match(self):
        WriteFileTool(self.root).run(path="f.txt", content="foo")
        result = EditFileTool(self.root).run(path="f.txt", old_string="nope", new_string="x")
        self.assertTrue(result.is_error)

    def test_list_files_non_recursive(self):
        WriteFileTool(self.root).run(path="one.txt", content="1")
        WriteFileTool(self.root).run(path="dir/two.txt", content="2")
        result = ListFilesTool(self.root).run(path=".")
        self.assertIn("one.txt", result.content)

    def test_list_files_recursive_finds_nested(self):
        WriteFileTool(self.root).run(path="dir/two.txt", content="2")
        result = ListFilesTool(self.root).run(path=".", recursive=True)
        self.assertIn("two.txt", result.content)


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_successful_command(self):
        result = RunCommandTool(self.root).run(command="echo hi")
        self.assertFalse(result.is_error)
        self.assertIn("hi", result.content)

    def test_failing_command_reports_error(self):
        result = RunCommandTool(self.root).run(command="exit 1")
        self.assertTrue(result.is_error)

    def test_runs_inside_root(self):
        with open(os.path.join(self.root, "marker.txt"), "w") as f:
            f.write("x")
        result = RunCommandTool(self.root).run(command="ls")
        self.assertIn("marker.txt", result.content)


class WorkspaceToolsFactoryTests(unittest.TestCase):
    def test_returns_five_tools(self):
        tools = workspace_tools("/tmp")
        names = {t.name for t in tools}
        self.assertEqual(
            names, {"read_file", "list_files", "write_file", "edit_file", "run_command"}
        )


if __name__ == "__main__":
    unittest.main()
