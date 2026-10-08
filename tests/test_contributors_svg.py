"""`scripts/contributors_svg.py`'s choice of who is drawn and how, without the network."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _wall():
    """`scripts/contributors_svg.py`, imported by path since `scripts/` is no package."""
    spec = importlib.util.spec_from_file_location(
        "contributors_svg", REPO / "scripts" / "contributors_svg.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wall = _wall()


# Account ids in an order that is neither the logins' nor the commits'.
IDS = {"a": 3, "b": 1, "c": 2}


def _user(login: str) -> dict:
    return {"login": login, "databaseId": IDS[login], "avatarUrl": f"https://avatars/{login}"}


def _commit(*logins: str | None) -> dict:
    """A ``Commit`` node with these authors; ``None`` is one GitHub links to no account."""
    return {"authors": {"nodes": [{"user": login and _user(login)} for login in logins]}}


@pytest.mark.parametrize(
    ("commits", "logins"),
    [
        ([_commit("b"), _commit("a"), _commit("a")], ["a", "b"]),
        ([_commit("a"), _commit("b"), _commit("c")], ["b", "c", "a"]),
        ([_commit("a", "b")], ["b", "a"]),
        ([_commit("a", "a"), _commit("c")], ["c", "a"]),
        ([_commit("a", None)], ["a"]),
        ([_commit(None)], []),
        ([], []),
    ],
)
def test_tally(commits, logins):
    assert wall.tally(commits) == [_user(login) for login in logins]


@pytest.mark.parametrize(
    ("count", "width", "height", "last"),
    [
        (1, 40, 40, ("0", "0")),
        (18, 788, 40, ("748", "0")),
        (19, 788, 84, ("0", "44")),
        (37, 788, 128, ("0", "88")),
    ],
)
def test_render_lays_the_faces_out_eighteen_to_a_row(count, width, height, last):
    faces = [(f"user{i}", f"data:image/png;base64,{i}") for i in range(count)]
    svg = wall.render(faces)
    assert re.search(rf'width="{width}" height="{height}" viewBox="0 0 {width} {height}"', svg)
    assert re.findall(r"<title>([^<]*)</title>", svg) == [login for login, _ in faces]
    assert re.findall(r'<image href="([^"]*)"', svg) == [uri for _, uri in faces]
    assert svg.count("clip-path=") == count
    assert re.findall(r'<image [^>]* x="(\d+)" y="(\d+)"', svg)[-1] == last
