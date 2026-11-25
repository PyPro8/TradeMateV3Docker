"""The project must be internally consistent: every internal import resolves to a real module that really
exposes what is imported, nothing imports a module that is missing, and the package says what version it is.

This exists because an archive once shipped with only some of the package's files. Run against an EXTRACTED copy
of a release (see TESTING.md) it proves the archive is self-contained.
"""

import ast
import compileall
import importlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"venv", ".venv", "__pycache__", "node_modules", ".git"}


def project_files():
    for path in sorted(ROOT.rglob("*.py")):
        if not SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            yield path


def internal_imports():
    """(file, module, [names]) for every `import trademate...` / `from trademate... import ...`."""
    found = []
    for path in project_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in ("trademate", "app", "tests"):
                found.append((path, node.module, [a.name for a in node.names]))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in ("trademate", "app", "tests"):
                        found.append((path, a.name, []))
    return found


class ProjectIntegrityTest(unittest.TestCase):
    def test_every_python_file_compiles(self):
        self.assertTrue(compileall.compile_dir(str(ROOT), quiet=2, force=True, rx=__import__("re").compile(r"venv|node_modules")))

    def test_package_exposes_its_version(self):
        trademate = importlib.import_module("trademate")
        self.assertRegex(trademate.__version__, r"^\d+\.\d+\.\d+")

    def test_every_internal_import_resolves_and_exposes_what_is_imported(self):
        imports = internal_imports()
        self.assertGreater(len(imports), 30)          # the scan itself must be finding things
        problems = []
        for path, module, names in imports:
            rel = path.relative_to(ROOT)
            try:
                mod = importlib.import_module(module)
            except Exception as exc:                   # missing file, syntax error, circular import ...
                problems.append(f"{rel}: cannot import {module}: {exc!r}")
                continue
            for name in names:
                if name != "*" and not hasattr(mod, name):
                    try:
                        importlib.import_module(f"{module}.{name}")      # `from package import submodule`
                    except Exception:
                        problems.append(f"{rel}: {module} has no '{name}'")
        self.assertEqual(problems, [])

    def test_every_module_in_the_package_imports_cleanly_on_its_own(self):
        for path in (ROOT / "trademate").rglob("*.py"):
            if path.name == "__main__.py":
                continue                               # entry points run code on import by design
            module = ".".join(path.relative_to(ROOT).with_suffix("").parts).replace(".__init__", "")
            with self.subTest(module=module):
                importlib.import_module(module)

    def test_required_modules_exist(self):
        for name in ("__init__", "config", "errors", "logging_setup", "journal", "app",
                     "broker/__init__", "broker/base", "broker/mt5_adapter", "broker/mt5_commands", "broker/validation"):
            self.assertTrue((ROOT / "trademate" / f"{name}.py").exists(), f"trademate/{name}.py is missing")
        for path in ("app.py", "mt5/TradeMateBridge.mq5", "templates/index.html", "static/js/app.js",
                     "static/js/pl.js", "static/css/style.css", "config/settings.toml", "requirements.txt"):
            self.assertTrue((ROOT / path).exists(), f"{path} is missing")

    def test_every_script_the_page_loads_exists(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        import re
        for name in re.findall(r"filename='([^']+)'", html):
            self.assertTrue((ROOT / "static" / name).exists(), f"static/{name} is referenced by index.html but missing")


if __name__ == "__main__":
    unittest.main()
