"""Bundle a private copy of python-roborock into the integration.

The Roborock Mower integration ships its own copy of the parts of
python-roborock it needs, under ``custom_components/roborock_mower/vendor/``.
This keeps it fully independent from the python-roborock version that Home
Assistant installs for the official Roborock integration: neither can force a
version on the other, and both run side by side in one Home Assistant.

Only the modules reachable from the entry points below are copied, and every
``roborock`` import inside them is rewritten to a relative import, so the copy
never touches (or is touched by) the top-level ``roborock`` package.

Usage (needs network access to PyPI):

    python script/vendor_roborock.py 7.12.1
"""

from __future__ import annotations

import ast
import io
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

REPO = Path(__file__).resolve().parent.parent
VENDOR = REPO / "custom_components" / "roborock_mower" / "vendor"
PACKAGE = "roborock"

# Everything the integration imports from python-roborock.
ENTRY_MODULES = [
    "roborock.data",
    "roborock.data.containers",
    "roborock.devices.cache",
    "roborock.devices.rpc.v1_channel",
    "roborock.exceptions",
    "roborock.mqtt.roborock_session",
    "roborock.mqtt.session",
    "roborock.protocol",
    "roborock.roborock_message",
    "roborock.web_api",
]


def _module_path(root: Path, module: str) -> Path | None:
    base = root.joinpath(*module.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    return None


def _package_of(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _imports(root: Path, module: str) -> set[str]:
    """Return every roborock module imported by ``module`` (incl. lazy imports)."""
    path = _module_path(root, module)
    assert path is not None, module
    package = _package_of(module, path)
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base_parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            names = [base] + [f"{base}.{alias.name}" for alias in node.names]
        else:
            continue
        for name in names:
            if (
                name == PACKAGE or name.startswith(PACKAGE + ".")
            ) and _module_path(root, name) is not None:
                found.add(name)
    return found


def closure(root: Path) -> set[str]:
    todo = list(ENTRY_MODULES)
    seen: set[str] = set()
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        # Importing a submodule executes every parent package __init__.
        parts = module.split(".")
        todo.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        todo.extend(_imports(root, module) - seen)
    return seen


_ABS_IMPORT = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kw>from|import)[ \t]+(?P<mod>roborock(?:\.[\w.]+)?)(?P<rest>.*)$",
    re.MULTILINE,
)


def _relative(target: str, package: str) -> str:
    """Relative module reference for ``target`` as seen from ``package``."""
    dots = "." * len(package.split("."))
    rest = target[len(PACKAGE) :].lstrip(".")
    return dots + rest


def rewrite(source: str, module: str, path: Path) -> str:
    package = _package_of(module, path)

    def _sub(match: re.Match[str]) -> str:
        indent, kw, mod, rest = match.group("indent", "kw", "mod", "rest")
        if kw == "import":
            raise SystemExit(f"{module}: unsupported 'import {mod}' form")
        return f"{indent}from {_relative(mod, package)}{rest}"

    return _ABS_IMPORT.sub(_sub, source)


def main(version: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                sys.executable, "-m", "pip", "download", "--quiet", "--no-deps",
                "--only-binary=:all:", f"python-roborock=={version}", "-d", tmp,
            ],
            check=True,
        )
        wheel = next(Path(tmp).glob("python_roborock-*.whl"))
        extract = Path(tmp) / "src"
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(extract)
            license_text = next(
                archive.read(name).decode()
                for name in archive.namelist()
                if name.endswith("LICENSE")
            )

        modules = closure(extract)
        target = VENDOR / PACKAGE
        if target.exists():
            shutil.rmtree(target)
        for module in sorted(modules):
            src = _module_path(extract, module)
            assert src is not None
            dest = VENDOR / src.relative_to(extract)
            dest.parent.mkdir(parents=True, exist_ok=True)
            text = src.read_text(encoding="utf-8")
            dest.write_text(rewrite(text, module, src), encoding="utf-8")

    (VENDOR / "LICENSE-python-roborock").write_text(license_text, encoding="utf-8")
    init = io.StringIO()
    init.write('"""Private copy of python-roborock bundled with this integration.\n\n')
    init.write("Generated by script/vendor_roborock.py -- do not edit by hand.\n")
    init.write("Source: https://github.com/Python-roborock/python-roborock\n")
    init.write("License: Apache-2.0 (see LICENSE-python-roborock). Modification:\n")
    init.write("absolute ``roborock`` imports were rewritten to relative imports and\n")
    init.write("only the modules this integration needs were copied.\n")
    init.write('"""\n\n')
    init.write(f'ROBOROCK_VERSION = "{version}"\n')
    (VENDOR / "__init__.py").write_text(init.getvalue(), encoding="utf-8")
    print(f"Vendored python-roborock {version}: {len(modules)} modules")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "7.12.1")
