"""Hold a pull request from a fork to Pull requests in CONTRIBUTING.md.

Run by ``.github/workflows/pr-gate.yml`` on ``pull_request_target``. Item 7 closes a pull request,
and items 5 and 6 comment on one:

- How many may be open (item 7). Until one of the author's pull requests here is merged, one may
  be open, a draft included; after that, three. A pull request the author opens or reopens past
  that is closed with a comment that names the ones kept, which are the author's oldest.
- Which issue each takes (item 7). A pull request the author opens or reopens that closes an issue
  an older open pull request already closes is closed with a comment that names that pull request.
  Which issues a pull request closes is read from the closing keywords in its description.
- The title and the description (items 5 and 6). What does not follow them is listed in one
  comment, which the next edit updates and a passing edit deletes. The run still passes: a title
  or description off the form does not hold up the review.

A pull request a maintainer reopens is left open by both rules of item 7. The pull request's title
and body are read from the event and never executed, and nothing from the pull request's branch is
checked out.

``python scripts/pr_gate.py --dry-run <number>`` runs every rule against an open pull request as
if it had just been opened, prints what it would write, and writes nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
HEADINGS = ("What changed", "Why this is what the real API does", "Verification")
FORMAT_MARKER = "<!-- pr-gate:format -->"
LIMIT_MARKER = "<!-- pr-gate:limit -->"
TAKEN_MARKER = "<!-- pr-gate:taken -->"
# The roles GitHub gives a pull request's author on this repository that are not a stranger's.
MAINTAINER_ROLES = {"OWNER", "MEMBER", "COLLABORATOR"}

_CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#(\d+)\b", re.I)
_SOURCE = re.compile(r"^([a-z0-9-]+): ")
_CONVENTIONAL = re.compile(
    r"^(?:build|chore|ci|docs|feat|fix|perf|refactor|style|test)(?:\([^)]*\))?!?:", re.I
)
_CHECKBOX = re.compile(r"^- \[[ xX]\] ", re.M)


def closed_issues(body: str) -> list[int]:
    """The issue numbers a closing keyword names in ``body``, in order, each once."""
    return list(dict.fromkeys(int(n) for n in _CLOSING.findall(body)))


def source_prefix(issue_title: str) -> str | None:
    """``gmail`` for ``gmail: maxResults above 500 …``; ``None`` when the title has no prefix."""
    match = _SOURCE.match(issue_title)
    return match.group(1) if match else None


def format_problems(title: str, body: str, prefixes: set[str]) -> list[str]:
    """What the title and description miss of items 5 and 6, one line each.

    ``prefixes`` are the ``<source>`` prefixes of the ``fidelity`` issues the description closes;
    when there are several, the title may open with any of them.
    """
    problems = []
    if prefixes and not any(title.startswith(f"{p}: ") for p in prefixes):
        wanted = " or ".join(f"`{p}: `" for p in sorted(prefixes))
        problems.append(f"Open the title with the closed issue's prefix, {wanted}.")
    if re.search(r"#\d", title):
        problems.append("Move the issue number from the title to the description, as `Closes #N`.")
    if _CONVENTIONAL.match(title):
        problems.append("Drop the `fix:`-style prefix from the title.")
    for heading in HEADINGS:
        if not re.search(rf"^## {re.escape(heading)}[ \t]*\r?$", body, re.M):
            problems.append(f"Add the template's `## {heading}` heading to the description.")
    if len(_CHECKBOX.findall(body)) < 3:
        problems.append("Keep the template's three checkboxes in the description.")
    return problems


def open_limit(merged: int) -> int:
    """How many pull requests an author with ``merged`` merged ones here may have open."""
    return 1 if merged == 0 else 3


def kept_if_over(number: int, open_prs: list[tuple[str, int]], merged: int) -> list[int] | None:
    """The author's pull requests kept open when ``number`` is past the limit, else ``None``.

    ``open_prs`` is ``(created_at, number)`` for each of the author's open pull requests,
    ``number`` included. The oldest are kept, so pull requests opened in the same minute settle
    the same way whichever run finishes first.
    """
    kept = [n for _, n in sorted(open_prs)[: open_limit(merged)]]
    return None if number in kept else kept


def held_elsewhere(number: int, open_prs: list[tuple[str, int, list[int]]]) -> dict[int, int]:
    """Each issue ``number`` closes that an older open pull request closes too, mapped to the
    oldest of those.

    ``open_prs`` is ``(created_at, number, issues)`` for each open pull request here, ``number``
    included, where ``issues`` is what :func:`closed_issues` reads in its description. As in
    :func:`kept_if_over`, the oldest holds, so two opened in the same minute settle the same way
    whichever run finishes first.
    """
    first: dict[int, int] = {}
    for _, n, issues in sorted(open_prs):
        for issue in issues:
            first.setdefault(issue, n)
    mine = next((issues for _, n, issues in open_prs if n == number), [])
    return {issue: first[issue] for issue in mine if first[issue] != number}


def _api(method: str, path: str, payload: dict | None = None):
    request = urllib.request.Request(
        path if path.startswith("https://") else API + path,
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request) as response:
        text = response.read()
    return json.loads(text) if text else None


def _pages(path: str) -> list:
    items, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        batch = _api("GET", f"{path}{sep}per_page=100&page={page}")
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def _fidelity_prefixes(repo: str, body: str) -> set[str]:
    prefixes = set()
    for number in closed_issues(body):
        try:
            issue = _api("GET", f"/repos/{repo}/issues/{number}")
        except urllib.error.HTTPError:
            continue
        labels = {label["name"] for label in issue.get("labels", [])}
        prefix = source_prefix(issue["title"])
        if "fidelity" in labels and prefix and "pull_request" not in issue:
            prefixes.add(prefix)
    return prefixes


def run(repo: str, action: str, pr: dict, sender: str, write: bool) -> int:
    number, login = pr["number"], pr["user"]["login"]
    if pr["state"] != "open" or pr.get("author_association") in MAINTAINER_ROLES:
        print(f"#{number}: not checked ({pr['state']}, {pr.get('author_association')})")
        return 0

    def say(method: str, path: str, payload: dict | None = None):
        print(f"{'' if write else '[dry-run] '}{method} {path}")
        if payload and "body" in payload:
            print("  " + payload["body"].replace("\n", "\n  "))
        return _api(method, path, payload) if write else None

    comments = _pages(f"/repos/{repo}/issues/{number}/comments")

    if action == "opened" or (action == "reopened" and sender == login):
        open_prs = [p for p in _pages(f"/repos/{repo}/pulls?state=open") if p["number"] != number]
        open_prs.append(pr)
        mine = [(p["created_at"], p["number"]) for p in open_prs if p["user"]["login"] == login]
        query = urllib.parse.quote(f"repo:{repo} is:pr is:merged author:{login}")
        merged = _api("GET", f"/search/issues?q={query}")["total_count"]
        kept = kept_if_over(number, mine, merged)
        if kept is not None:
            listed = ", ".join(f"#{n}" for n in kept)
            note = (
                f"{LIMIT_MARKER}\nClosing this one: until one of your pull requests here is "
                "merged, one may be open at a time, a draft included, and three after that "
                f"([Pull requests](https://github.com/{repo}/blob/main/CONTRIBUTING.md"
                f"#pull-requests), item 7). Open now: {listed}. The branch stays in your fork; "
                "open a pull request from it again when that leaves room."
            )
            say("POST", f"/repos/{repo}/issues/{number}/comments", {"body": note})
            say("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})
            return 0
        held = held_elsewhere(
            number,
            [(p["created_at"], p["number"], closed_issues(p["body"] or "")) for p in open_prs],
        )
        if held:
            one = len(set(held.values())) == 1
            holders = " and ".join(f"#{n}" for n in dict.fromkeys(held.values()))
            issues = " and ".join(f"#{issue}" for issue in held)
            note = (
                f"{TAKEN_MARKER}\nClosing this one: {holders}, opened before it, already "
                f"close{'s' if one else ''} {issues}, and an open pull request takes the issue it "
                f"closes ([Pull requests](https://github.com/{repo}/blob/main/CONTRIBUTING.md"
                "#pull-requests), item 7). The branch stays in your fork; open a pull request "
                f"from it again if {holders} {'is' if one else 'are'} closed without being merged."
            )
            say("POST", f"/repos/{repo}/issues/{number}/comments", {"body": note})
            say("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})
            return 0

    problems = format_problems(
        pr["title"], pr["body"] or "", _fidelity_prefixes(repo, pr["body"] or "")
    )
    previous = next((c for c in comments if c["body"].startswith(FORMAT_MARKER)), None)
    if problems:
        note = (
            f"{FORMAT_MARKER}\nA few things in the title and description differ from "
            f"[Pull requests](https://github.com/{repo}/blob/main/CONTRIBUTING.md#pull-requests), "
            "items 5 and 6. This does not hold up the review; editing the pull request "
            "updates this comment.\n\n" + "\n".join(f"- {p}" for p in problems)
        )
        if previous is None:
            say("POST", f"/repos/{repo}/issues/{number}/comments", {"body": note})
        elif previous["body"] != note:
            say("PATCH", f"/repos/{repo}/issues/comments/{previous['id']}", {"body": note})
    elif previous is not None:
        say("DELETE", f"/repos/{repo}/issues/comments/{previous['id']}")
    else:
        print(f"#{number}: title and description follow items 5 and 6")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--dry-run"]:
        repo = os.environ.get("GITHUB_REPOSITORY", "brekkylab/backlot")
        pr = _api("GET", f"/repos/{repo}/pulls/{int(argv[1])}")
        return run(repo, "opened", pr, pr["user"]["login"], write=False)
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    pr, sender = event["pull_request"], event["sender"]["login"]
    return run(os.environ["GITHUB_REPOSITORY"], event["action"], pr, sender, write=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
