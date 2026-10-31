"""Offline acceptance of the workflow's actual conditions and shell bodies."""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


def condition(expression, values):
    expression = expression.removeprefix("${{").removesuffix("}} ").removesuffix("}}")
    expression = expression.replace("!cancelled()", "True")
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


def test_fidelity_workflow_conditions_and_reporting():
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/fidelity.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = next(
        job["steps"]
        for job in workflow["jobs"].values()
        if any("gen_jira_unserved" in step.get("run", "") for step in job.get("steps", []))
    )
    table = next(step for step in steps if "gen_jira_unserved" in step.get("run", ""))
    report = next(
        step for step in steps if step.get("name") == "Report the divergence on this source's issue"
    )
    failure = next(
        step for step in steps if step.get("name") == "Fail the run so the schedule shows red"
    )
    table_id = table.get("id", "missing_table_id")
    cases = [
        ("jira", "0", "", "1", True, True),
        ("jira", "1", "", "0", True, True),
        ("jira", "1", "", "1", True, True),
        ("jira", "0", "", "0", False, False),
        ("jira", "2", "", "1", False, True),
        ("jira", "1", "", "2", False, True),
        ("jira", "0", "", "2", False, False),
        ("linear", "0", "1", "", True, True),
        ("linear", "1", "2", "", False, True),
        ("linear", "2", "1", "", False, True),
        ("linear", "0", "0", "", False, False),
        ("slack", "1", "", "", True, True),
        ("slack", "0", "", "", False, False),
    ]
    results = []
    for source, diff, fragments, table_status, should_report, should_fail in cases:
        values = {
            "matrix.source": source,
            "steps.diff.outputs.status": diff,
            "steps.fragments.outputs.status": fragments,
            f"steps.{table_id}.outputs.status": table_status,
        }
        actual = (condition(report["if"], values), condition(failure["if"], values))
        assert actual == (should_report, should_fail), (
            source,
            diff,
            fragments,
            table_status,
            actual,
        )
        results.append(
            {
                "source": source,
                "diff": diff,
                "fragments": fragments,
                "table": table_status,
                "report": actual[0],
                "fail": actual[1],
            }
        )

    assert steps.index(table) < steps.index(report)
    runtime = []
    for table_status in ("0", "1", "2"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindir = root / "bin"
            bindir.mkdir()
            fake_python = bindir / "python"
            fake_python.write_text(
                '#!/bin/sh\nprintf "JIRA TABLE DIAGNOSTIC\\n"\nexit "$STUB_STATUS"\n',
                encoding="utf-8",
            )
            fake_python.chmod(0o700)
            output = root / "github-output"
            output.touch()
            env = dict(
                os.environ,
                PATH=f"{bindir}:{os.environ['PATH']}",
                GITHUB_OUTPUT=str(output),
                STUB_STATUS=table_status,
            )
            run = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", table["run"]],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            assert run.returncode == 0, run.stderr
            assert f"status={table_status}" in output.read_text(encoding="utf-8")
            if table_status == "2":
                assert "::warning::" in run.stdout
            runtime.append({"table_status": table_status, "collector_exit": run.returncode})
            if table_status != "1":
                continue
            fake_gh = bindir / "gh"
            fake_gh.write_text(
                f"#!{sys.executable}\nimport json,os,sys\n"
                "with open(os.environ['STUB_GH_LOG'],'a',encoding='utf-8') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o700)
            (root / "report.txt").write_text("COMPARISON FINDING", encoding="utf-8")
            (root / "fragments.txt").write_text("1 fragment\nLINEAR FINDING", encoding="utf-8")
            values = {
                "steps.diff.outputs.status": "1",
                "steps.fragments.outputs.status": "",
                f"steps.{table_id}.outputs.status": "1",
            }
            env.update(
                SOURCE="jira",
                RUN_URL="https://example.invalid/run",
                GITHUB_REPOSITORY="local/fake",
                STUB_GH_LOG=str(root / "gh.jsonl"),
            )
            for key, value in report.get("env", {}).items():
                match = re.fullmatch(r"\$\{\{\s*(steps\.[\w-]+\.outputs\.status)\s*}}", value)
                if match:
                    env[key] = values.get(match[1], "")
            run = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", report["run"]],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            assert run.returncode == 0, run.stderr
            calls = [
                json.loads(line)
                for line in (root / "gh.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            create = next(call for call in calls if call[:2] == ["issue", "create"])
            body = create[create.index("--body") + 1]
            assert "JIRA TABLE DIAGNOSTIC" in body and "COMPARISON FINDING" in body, body
            runtime.append(
                {
                    "combined_report": "contains actual collected Jira diagnostic and comparison finding",
                    "external_calls": 0,
                }
            )

    assert len(results) == 13
    assert len(runtime) == 4
