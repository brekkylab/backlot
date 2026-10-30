"""`scripts/linear_sdk_fragments.py`, the check CI runs over @linear/sdk's generated fragments."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import script_module

REPO = Path(__file__).resolve().parent.parent


script = script_module("linear_sdk_fragments")

_SCHEMA = """
type Query { label: Label }
type Label { id: ID! name: String! color: String! }
type Stub { id: ID! }
interface Named { id: ID! name: String! }
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
            ["fragments: 1, checked: 1"],
            id="declared fields",
        ),
        pytest.param(
            _SCHEMA,
            _bundle(
                "fragment Label on Label {\n  id\n  name\n  groupType\n  parent {\n    id\n  }\n}"
            ),
            1,
            [
                "fragments: 1, checked: 1",
                "fragment Label on Label selects what Label does not declare: groupType, parent",
            ],
            id="undeclared fields",
        ),
        pytest.param(
            _SCHEMA,
            _bundle("fragment Label on Label {\n  id\n  team {\n    id\n    key\n  }\n}"),
            1,
            [
                "fragments: 1, checked: 1",
                "fragment Label on Label selects what Label does not declare: team",
            ],
            id="a nested selection is not read",
        ),
        pytest.param(
            _SCHEMA,
            _bundle("fragment Named on Named {\n  id\n  name\n  color\n}"),
            1,
            [
                "fragments: 1, checked: 1",
                "fragment Named on Named selects what Named does not declare: color",
            ],
            id="an interface",
        ),
        pytest.param(
            _SCHEMA,
            _bundle("fragment Label on Label\n{\n  id\n  name\n}"),
            0,
            ["fragments: 1, checked: 1"],
            id="a newline before the selection",
        ),
        pytest.param(
            _SCHEMA,
            _bundle("fragment Stub on Stub {\n  id\n  name\n}"),
            0,
            ["fragments: 1, checked: 0"],
            id="an id-only type",
        ),
        pytest.param(
            _SCHEMA,
            _bundle("fragment Team on Team {\n  id\n  key\n}"),
            0,
            ["fragments: 1, checked: 0"],
            id="a type the schema lacks",
        ),
        pytest.param(
            _SCHEMA,
            _bundle('fragment Label on Label {\n  name(format: "x")\n  color\n}'),
            0,
            ["fragments: 1, checked: 1"],
            id="a quoted argument",
        ),
        pytest.param(
            _SCHEMA,
            _bundle("fragment Label on Label {\n  id\n  ...LabelParts\n}"),
            0,
            ["fragments: 1, checked: 1"],
            id="a spread is no field",
        ),
        pytest.param(
            None,
            _bundle("fragment IssueLabel on IssueLabel {\n  id\n  nosuch\n}"),
            1,
            [
                "fragments: 1, checked: 1",
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
    "bundle, schema, line",
    [
        (None, None, "cannot read {path}: "),
        (b"\xff\xfe", None, "cannot read {path}: "),
        (
            _bundle("fragment Label on Label {\n  id\n}"),
            "none.graphql",
            "cannot read {tmp}/none.graphql: ",
        ),
        (
            "export const x = 1;",
            None,
            "{path} holds no `fragment … on …`; the bundle's layout may have changed",
        ),
        (
            'const D = new TypedDocumentString("fragment Label on Label {\\n  id\\n}");',
            None,
            "{path}: fragment Label does not parse: Syntax Error",
        ),
        (
            "const D = new TypedDocumentString(`fragment Label on Label {\n  id\n`);",
            None,
            "{path}: fragment Label's braces never close; the bundle's layout may have changed",
        ),
    ],
    ids=[
        "no file",
        "not UTF-8",
        "no schema",
        "no fragment",
        "a fragment in a string with escaped newlines",
        "braces that never close",
    ],
)
def test_linear_sdk_fragments_cannot_check_what_it_cannot_read(
    tmp_path, capsys, bundle, schema, line
):
    path = tmp_path / "index.mjs"
    if isinstance(bundle, bytes):
        path.write_bytes(bundle)
    elif bundle is not None:
        path.write_text(bundle, encoding="utf-8")
    argv = [str(path)] if schema is None else [str(path), "--schema", str(tmp_path / schema)]
    assert script.main(argv) == 2
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1 and printed[0].startswith(line.format(path=path, tmp=tmp_path))


# (the schema, as in the first table; the exit code; the lines on stdout; the first line on stderr)
@pytest.mark.parametrize(
    "schema,code,out,err",
    [
        pytest.param(
            None,
            1,
            [
                "fragments: 1, checked: 1",
                "fragment IssueLabel on IssueLabel selects what IssueLabel does not declare: nosuch",
            ],
            [],
            id="a fragment named",
        ),
        pytest.param(
            "type Query {",
            2,
            [],
            ["Traceback (most recent call last):"],
            id="a schema that does not build",
        ),
    ],
)
def test_linear_sdk_fragments_runs_as_ci_runs_it(tmp_path, schema, code, out, err):
    """The command line CI runs: the script by its path, its exit code the step's."""
    path = tmp_path / "index.mjs"
    path.write_text(_bundle("fragment IssueLabel on IssueLabel {\n  nosuch\n}"), encoding="utf-8")
    argv = [sys.executable, str(REPO / "scripts" / "linear_sdk_fragments.py"), str(path)]
    if schema is not None:
        (tmp_path / "linear.graphql").write_text(schema, encoding="utf-8")
        argv += ["--schema", str(tmp_path / "linear.graphql")]
    r = subprocess.run(argv, capture_output=True, encoding="utf-8")
    assert r.returncode == code
    assert r.stdout.splitlines() == out
    assert r.stderr.splitlines()[:1] == err
