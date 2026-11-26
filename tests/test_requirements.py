"""The Docker image installs requirements.txt only: every third-party package
the app needs must be listed there (tests run with dev packages installed and
would not notice a missing one)."""
import ast
import importlib.metadata as md
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def listed():
    names = set()
    for line in open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8"):
        line = line.split("#")[0].strip()
        if line:
            names.add(re.split(r"[<>=!~\[ ]", line)[0].lower().replace("_", "-"))
    return names


def imported_top_modules():
    mods = set()
    for dp, _, ns in os.walk(os.path.join(ROOT, "app")):
        for n in ns:
            if n.endswith(".py"):
                tree = ast.parse(open(os.path.join(dp, n), encoding="utf-8").read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        mods.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in sys.stdlib_module_names and m != "app"}


def test_every_imported_package_is_in_requirements():
    dists = md.packages_distributions()
    req = listed()
    missing = []
    for mod in sorted(imported_top_modules()):
        names = {d.lower().replace("_", "-") for d in dists.get(mod, [mod])}
        if not names & req:
            missing.append(f"{mod} ({', '.join(sorted(names))})")
    assert not missing, f"imported but not in requirements.txt: {missing}"


def test_indirect_requirements():
    src = "".join(open(os.path.join(ROOT, "app", f), encoding="utf-8").read()
                  for f in os.listdir(os.path.join(ROOT, "app")) if f.endswith(".py"))
    req = listed()
    if re.search(r"\bForm\(", src):
        assert "python-multipart" in req, "FastAPI Form() needs python-multipart"
    if "ZoneInfo" in src:
        assert "tzdata" in req, "zoneinfo needs tzdata in slim images"
