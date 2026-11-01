"""The `compare` job of `.github/workflows/fidelity.yml` outside Actions: which steps run for a
source and the statuses its checks answer, the bash of the Jira table check and of the issue step
with stand-ins for `python` and `gh`, and `scripts/gen_jira_unserved.py` run by its path as the
check runs it."""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
STEPS = {
    step.get("id", step.get("name")): step
    for step in yaml.safe_load(
        (REPO / ".github" / "workflows" / "fidelity.yml").read_text(encoding="utf-8")
    )["jobs"]["compare"]["steps"]
}
REPORT = STEPS["Report the divergence on this source's issue"]
FAIL = STEPS["Fail the run so the schedule shows red"]
bash = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash"),
    reason="the job's steps run under bash, on ubuntu-latest",
)


def condition(expression, values):
    expression = expression.removeprefix("${{").removesuffix("}} ").removesuffix("}}")
    expression = (
        re.sub(
            r"steps\.[\w-]+\.outputs\.status|matrix\.source",
            lambda match: repr(values.get(match[0], "")),
            expression,
        )
        .replace("&&", " and ")
        .replace("||", " or ")
    )
    tree = ast.parse(expression.strip(), mode="eval")
    allowed = (
        ast.Expression,
        ast.BoolOp,
        ast.Compare,
        ast.Constant,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
    )
    assert all(isinstance(node, allowed) for node in ast.walk(tree)), expression

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp):
            values = [evaluate(value) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            left = evaluate(node.left)
            for operator, right_node in zip(node.ops, node.comparators, strict=True):
                right = evaluate(right_node)
                if (left == right) != isinstance(operator, ast.Eq):
                    return False
                left = right
            return True
        raise AssertionError(node)

    return bool(evaluate(tree))


def run_step(step, cwd, env):
    """A step's `run` as Actions runs one with no `shell:` on ubuntu-latest: `bash -e {0}`, with no
    `pipefail`."""
    script = cwd / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    return subprocess.run(
        ["bash", "-e", str(script)],
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )


def stand_in(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)


def test_each_source_runs_the_checks_it_takes_and_reports_on_their_statuses():
    """A row is a source and what the comparison and each check would exit with, and then whether
    the table check runs, whether the issue step does and whether the run is failed. A check's
    status reaches the later steps only where its own `if:` lets it run."""
    rows = [
        (("jira", "0", "1", "1"), (True, True, True)),
        (("jira", "1", "", "0"), (True, True, True)),
        (("jira", "1", "", "1"), (True, True, True)),
        (("jira", "0", "", "0"), (True, False, False)),
        (("jira", "2", "", "1"), (True, False, True)),
        (("jira", "1", "", "2"), (True, False, True)),
        (("jira", "0", "", "2"), (True, False, False)),
        (("jira", "1", "", "3"), (True, False, True)),
        (("jira", "0", "", "3"), (True, False, True)),
        (("linear", "0", "1", "1"), (False, True, True)),
        (("linear", "1", "2", ""), (False, False, True)),
        (("linear", "2", "1", ""), (False, False, True)),
        (("linear", "0", "0", "1"), (False, False, False)),
        (("slack", "1", "", ""), (False, True, True)),
        (("slack", "0", "1", "1"), (False, False, False)),
    ]
    got = []
    for (source, diff, fragments, table), _ in rows:
        values = {"matrix.source": source, "steps.diff.outputs.status": diff}
        ran = {}
        for check, status in (("fragments", fragments), ("table", table)):
            ran[check] = condition(STEPS[check]["if"], values)
            values[f"steps.{check}.outputs.status"] = status if ran[check] else ""
        got.append((ran["table"], condition(REPORT["if"], values), condition(FAIL["if"], values)))
    assert got == [expected for _, expected in rows]
    assert [*STEPS].index("table") < [*STEPS].index(REPORT["name"])


@bash
def test_the_table_check_records_its_status_and_output(tmp_path):
    """A row is what the check prints on stdout and on stderr and exits with, and what the step
    leaves: its own exit code, the status it records, `table.txt`, and whether it warns."""
    stale = "backlot/data/jira_unserved.json is stale; run scripts/gen_jira_unserved.py\n"
    dropped = "the baseline records `missing` operations the documents no longer publish: […]\n"
    unreachable = "https://developer.atlassian.com/… unreachable: [Errno 8]\n"
    raised = "Traceback (most recent call last):\n"
    rows = [
        (("", "", 0), (0, "0", "", False)),
        ((stale, "", 1), (0, "1", stale, False)),
        (("", dropped, 1), (0, "1", dropped, False)),
        (("", unreachable, 2), (0, "2", unreachable, True)),
        (("", raised, 3), (0, "3", raised, False)),
    ]
    stand_in(
        tmp_path / "python",
        '#!/bin/sh\nprintf "%s" "$OUT"\nprintf "%s" "$ERR" >&2\nexit "$STATUS"\n',
    )
    got = []
    for (out, err, status), _ in rows:
        (tmp_path / "output").write_text("", encoding="utf-8")
        env = dict(
            os.environ,
            PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            GITHUB_OUTPUT=str(tmp_path / "output"),
            OUT=out,
            ERR=err,
            STATUS=str(status),
        )
        run = run_step(STEPS["table"], tmp_path, env)
        got.append(
            (
                run.returncode,
                (tmp_path / "output").read_text(encoding="utf-8").removeprefix("status=").strip(),
                (tmp_path / "table.txt").read_text(encoding="utf-8"),
                "::warning::" in run.stdout,
            )
        )
    assert got == [expected for _, expected in rows]


@bash
def test_the_issue_step_puts_the_table_check_beside_the_comparison(tmp_path):
    """A row is the comparison's and the table check's status and the open issue's body, and the
    `gh` calls that write: each one's verb, whether the body names the table check before its code
    block, and the block, which holds the table check's output ahead of the comparison's report.
    An issue that already says it is not written."""
    stand_in(
        tmp_path / "gh",
        f"#!{sys.executable}\nimport json, os, sys\n"
        "with open(os.environ['GH_LOG'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'api' and 'state=open' in sys.argv[2]:\n"
        "    print(os.environ['OPEN'])\n"
        "if sys.argv[1:3] == ['issue', 'view']:\n"
        "    print(os.environ['BODY'])\n",
    )
    (tmp_path / "table.txt").write_text("TABLE CHECK OUTPUT\n", encoding="utf-8")
    (tmp_path / "report.txt").write_text("COMPARISON REPORT\n", encoding="utf-8")

    def writes(diff, table, body):
        log = tmp_path / "gh.jsonl"
        log.unlink(missing_ok=True)
        statuses = {"diff": diff, "fragments": "", "table": table}
        env = dict(
            os.environ,
            PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            GH_LOG=str(log),
            GITHUB_REPOSITORY="owner/repo",
            OPEN="" if body is None else "7",
            BODY=body or "",
        )
        for name, value in REPORT["env"].items():
            step = re.fullmatch(r"\$\{\{ steps\.(\w+)\.outputs\.status }}", value)
            env[name] = statuses[step[1]] if step else value
        env.update(SOURCE="jira", RUN_URL="https://example.invalid/run")
        assert run_step(REPORT, tmp_path, env).returncode == 0
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return [
            (call[1], call[call.index("--body") + 1])
            for call in calls
            if call[0] == "issue" and "--body" in call
        ]

    both = "TABLE CHECK OUTPUT\n\nCOMPARISON REPORT\n"
    [(_, written)] = writes("1", "1", None)
    rows = [
        (("1", "1", None), [("create", True, [both])]),
        (("0", "1", None), [("create", True, ["TABLE CHECK OUTPUT\n"])]),
        (("1", "0", None), [("create", False, ["COMPARISON REPORT\n"])]),
        (("1", "1", "an older report"), [("edit", True, [both]), ("comment", False, [])]),
        (("1", "1", written), []),
    ]
    got = [
        [
            (
                verb,
                "`scripts/gen_jira_unserved.py --check`" in body.split("```")[0],
                re.findall(r"```\n(.*)```", body, re.S),
            )
            for verb, body in writes(*row)
        ]
        for row, _ in rows
    ]
    assert got == [expected for _, expected in rows]


def test_the_table_script_run_by_its_path_exits_3_on_an_exception(tmp_path):
    """A row is the documents the script reads, every operation the baseline records as missing
    published with a `security` that refuses it and the last one with the `security` given, and what
    it answers: its exit code, stdout, and the first line of stderr. An object where `security`
    takes an array is a `TypeError`, which Python would exit 1 on, the code the table check reads as
    a stale file."""
    from backlot.fidelity.comparisons import baseline_path

    entries = json.loads(baseline_path("jira").read_text(encoding="utf-8"))["operations"]
    unserved = [entry["operation"] for entry in entries if entry["status"] == "missing"]
    stale = ["backlot/data/jira_unserved.json is stale; run scripts/gen_jira_unserved.py"]
    rows = [
        ([], (1, stale, [])),
        ({}, (3, [], ["Traceback (most recent call last):"])),
    ]
    serve = (
        "import json, runpy, sys\nimport httpx\n"
        "with open(sys.argv[2], encoding='utf-8') as f:\n    document = json.load(f)\n"
        "httpx.get = lambda url, **kw: httpx.Response(\n"
        "    200, json=document, request=httpx.Request('GET', url)\n)\n"
        "sys.argv = [sys.argv[1], '--check']\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    got = []
    for security, _ in rows:
        published: dict = {"paths": {}}
        for operation in unserved:
            method, path = operation.split(" ", 1)
            published["paths"].setdefault(path, {})[method.lower()] = {"security": [{"x": []}]}
        published["paths"][path][method.lower()]["security"] = security
        (tmp_path / "documents.json").write_text(json.dumps(published), encoding="utf-8")
        run = subprocess.run(
            [
                sys.executable,
                "-c",
                serve,
                str(REPO / "scripts" / "gen_jira_unserved.py"),
                str(tmp_path / "documents.json"),
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        got.append((run.returncode, run.stdout.splitlines(), run.stderr.splitlines()[:1]))
    assert got == [expected for _, expected in rows]
