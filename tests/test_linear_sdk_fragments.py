"""`scripts/linear_sdk_fragments.py`, the check CI runs over @linear/sdk's generated fragments."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _script():
    """The script as a module, loaded the way `_check` in `tests/test_prose_check.py` is."""
    spec = importlib.util.spec_from_file_location(
        "scripts.linear_sdk_fragments", REPO / "scripts" / "linear_sdk_fragments.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _script()

_SCHEMA = """
type Query { label: Label }
type Label { id: ID! name: String! color: String! }
type Stub { id: ID! }
"""


def _bundle(*documents: str) -> str:
    """Documents as @linear/sdk 99.0.0's bundle writes them, in template literals as they are."""
    return "\n".join("const D = new TypedDocumentString(`%s`);" % d for d in documents)


# (the schema, `None` for the one Backlot serves; the bundle; the exit code; the lines printed)
@pytest.mark.parametrize(
    "schema,bundle,code,lines",
    [
        pytest.param(
            _SCHEMA,
            _bundle("fragment Label on Label {\n  __typename\n  id\n  name\n}"),
            0,
            ["1 fragments, 1 on a type the schema serves beyond `id`"],
            id="declared fields",
        ),
        pytest.param(
            _SCHEMA,
            _bundle(
                "fragment Label on Label {\n  id\n  name\n  groupType\n  parent {\n    id\n  }\n}"
            ),
            1,
            [
                "1 fragments, 1 on a type the schema serves beyond `id`",
                "fragment Label on Label selects what Label does not declare: groupType, parent",
            ],
            id="undeclared fields",
        ),
        pytest.param(
            _SCHEMA,
            _bundle("fragment Label on Label {\n  id\n  team {\n    id\n    key\n  }\n}"),
            1,
            [
                "1 fragments, 1 on a type the schema serves beyond `id`",
                "fragment Label on Label selects what Label does not declare: team",
            ],
            id="a nested selection is not read",
        ),
        pytest.param(
            _SCHEMA,
            _bundle("fragment Stub on Stub {\n  id\n  name\n}"),
            0,
            ["1 fragments, 0 on a type the schema serves beyond `id`"],
            id="an id-only type",
        ),
        pytest.param(
            _SCHEMA,
            _bundle("fragment Team on Team {\n  id\n  key\n}"),
            0,
            ["1 fragments, 0 on a type the schema serves beyond `id`"],
            id="a type the schema lacks",
        ),
        pytest.param(
            _SCHEMA,
            _bundle('fragment Label on Label {\n  name(format: "x")\n  color\n}'),
            0,
            ["1 fragments, 1 on a type the schema serves beyond `id`"],
            id="an escaped quote",
        ),
        pytest.param(
            _SCHEMA,
            _bundle("fragment Label on Label {\n  id\n  ...LabelParts\n}"),
            0,
            ["1 fragments, 1 on a type the schema serves beyond `id`"],
            id="a spread is no field",
        ),
        pytest.param(
            None,
            _bundle("fragment IssueLabel on IssueLabel {\n  id\n  nosuch\n}"),
            1,
            [
                "1 fragments, 1 on a type the schema serves beyond `id`",
                "fragment IssueLabel on IssueLabel selects what IssueLabel does not declare: nosuch",
            ],
            id="the schema Backlot serves",
        ),
    ],
)
def test_linear_sdk_fragments(tmp_path, capsys, schema, bundle, code, lines):
    path = tmp_path / "index.mjs"
    path.write_text(bundle, encoding="utf-8")
    argv = [str(path)]
    if schema is not None:
        (tmp_path / "linear.graphql").write_text(schema, encoding="utf-8")
        argv += ["--schema", str(tmp_path / "linear.graphql")]
    assert script.main(argv) == code
    assert capsys.readouterr().out.splitlines() == lines


@pytest.mark.parametrize(
    "bundle, line",
    [
        (None, "cannot read {path}: "),
        (
            "export const x = 1;",
            "{path} holds no `fragment … on …`; the bundle's layout may have changed",
        ),
    ],
    ids=["no file", "no fragment"],
)
def test_linear_sdk_fragments_cannot_check_what_it_cannot_read(tmp_path, capsys, bundle, line):
    path = tmp_path / "index.mjs"
    if bundle is not None:
        path.write_text(bundle, encoding="utf-8")
    assert script.main([str(path)]) == 2
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1 and printed[0].startswith(line.format(path=path))


def test_linear_sdk_fragments_runs_as_ci_runs_it(tmp_path):
    """The command line CI runs: the script by its path, its exit code the step's."""
    path = tmp_path / "index.mjs"
    path.write_text(_bundle("fragment IssueLabel on IssueLabel {\n  nosuch\n}"), encoding="utf-8")
    script_path = REPO / "scripts" / "linear_sdk_fragments.py"
    r = subprocess.run([sys.executable, str(script_path), str(path)], capture_output=True)
    assert r.returncode == 1
