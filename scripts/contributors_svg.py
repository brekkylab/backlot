"""Draw the contributors to ``main`` as a wall of round avatars, for the README.

Run by ``.github/workflows/contributors.yml`` on every push to ``main``. A contributor is an account
GitHub links to a commit on ``main``, as its author or as a co-author through a ``Co-authored-by``
trailer, read from the GraphQL ``Commit.authors`` field when the run starts. They are ordered by how
many commits they are on, ties by account id, which is the order of the Contributors list on the
repository's page. The REST contributors API is not used: it counts no co-author, and GitHub
documents that it "may return information that is a few hours old".

An author GitHub links to no account comes back with ``user`` null and is not drawn. Each avatar
is embedded in the file as a data URI: an SVG shown through ``<img>`` loads nothing from outside
itself.

``python scripts/contributors_svg.py <out.svg>`` writes the drawing.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

API = "https://api.github.com/graphql"
COLUMNS = 12
SIZE = 64
GAP = 6

QUERY = """
query($owner: String!, $name: String!, $branch: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $branch) {
      target {
        ... on Commit {
          history(first: 100, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes {
              authors(first: 50) { nodes { user { login databaseId avatarUrl(size: %d) } } }
            }
          }
        }
      }
    }
  }
}
""" % (SIZE * 2)


def tally(commits: list[dict]) -> list[dict]:
    """The accounts on ``commits``, most commits first, ties by account id.

    ``commits`` are ``Commit`` nodes with their ``authors``; an author whose ``user`` is null is
    one GitHub links to no account. An account named twice on one commit counts once for it.
    """
    counts: dict[str, int] = {}
    users: dict[str, dict] = {}
    for commit in commits:
        logins = set()
        for author in commit["authors"]["nodes"]:
            if author["user"]:
                logins.add(author["user"]["login"])
                users[author["user"]["login"]] = author["user"]
        for login in logins:
            counts[login] = counts.get(login, 0) + 1
    order = sorted(counts, key=lambda login: (-counts[login], users[login]["databaseId"]))
    return [users[login] for login in order]


def render(faces: list[tuple[str, str]]) -> str:
    """The SVG for ``(login, avatar data URI)`` pairs, ``COLUMNS`` to a row."""
    columns = min(len(faces), COLUMNS)
    rows = -(-len(faces) // COLUMNS)
    width = columns * (SIZE + GAP) - GAP
    height = rows * (SIZE + GAP) - GAP
    radius = SIZE // 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    ]
    for i, (login, uri) in enumerate(faces):
        x = (i % COLUMNS) * (SIZE + GAP)
        y = (i // COLUMNS) * (SIZE + GAP)
        cx, cy = x + radius, y + radius
        parts.append(
            f"<g><title>{login}</title>"
            f'<clipPath id="c{i}"><circle cx="{cx}" cy="{cy}" r="{radius}"/></clipPath>'
            f'<image href="{uri}" x="{x}" y="{y}" width="{SIZE}" height="{SIZE}" '
            f'clip-path="url(#c{i})"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{radius - 0.5}" fill="none" stroke="#8b949e" '
            f'stroke-opacity="0.5"/></g>'
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _graphql(variables: dict) -> dict:
    request = urllib.request.Request(
        API,
        data=json.dumps({"query": QUERY, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}"},
    )
    with urllib.request.urlopen(request) as response:
        body = json.loads(response.read())
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]


def _commits(repo: str, branch: str) -> list[dict]:
    owner, name = repo.split("/")
    commits, after = [], None
    while True:
        data = _graphql({"owner": owner, "name": name, "branch": branch, "after": after})
        history = data["repository"]["ref"]["target"]["history"]
        commits += history["nodes"]
        if not history["pageInfo"]["hasNextPage"]:
            return commits
        after = history["pageInfo"]["endCursor"]


def _avatar(url: str) -> str:
    # The token is the API's; the avatar host is sent nothing but the URL.
    with urllib.request.urlopen(url) as response:
        image, kind = response.read(), response.headers.get_content_type()
    return f"data:{kind};base64,{base64.b64encode(image).decode()}"


def main(argv: list[str]) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "brekkylab/backlot")
    users = tally(_commits(repo, "refs/heads/main"))
    faces = [(user["login"], _avatar(user["avatarUrl"])) for user in users]
    with open(argv[0], "w", encoding="utf-8") as handle:
        handle.write(render(faces))
    print(f"{len(faces)} contributors drawn to {argv[0]}: {', '.join(u['login'] for u in users)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
