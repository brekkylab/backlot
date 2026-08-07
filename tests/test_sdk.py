"""Read-only coverage: drive each official SDK against the mock.

Uses the ``live_server`` fixture (a real ``uvicorn`` on the conftest SAMPLE corpus, which
carries the +α surface) — the official SDKs make real HTTP calls, so they need a listening
port rather than the in-process ``TestClient``. Exercises every service's SDK read methods — Slack (slack_sdk),
Gmail+Drive+Sheets (google-api-python-client), GitHub (PyGithub), Jira+Confluence
(atlassian-python-api) — asserting all return shape-correct data. Skipped unless the optional
SDKs (``.[examples]``) are installed.
"""

from __future__ import annotations

import pytest

for _mod in ("slack_sdk", "googleapiclient", "github", "atlassian"):
    pytest.importorskip(_mod)

BASE = ADMIN = None  # set by the test from the live_server fixture
_results: list[tuple[str, str, bool, str]] = []


def check(service: str, name: str):
    def run(fn):
        try:
            note = fn() or ""
            _results.append((service, name, True, str(note)[:44]))
        except Exception as e:  # noqa: BLE001
            _results.append((service, name, False, f"{type(e).__name__}: {e}"[:44]))

    return run


# ------------------------------------------------------------------ Slack
def slack():
    from slack_sdk import WebClient

    c = WebClient(token=ADMIN, base_url=f"{BASE}/slack/api/")
    check("Slack", "auth.test")(lambda: c.auth_test()["ok"] and "ok")
    chans = c.conversations_list(limit=200)["channels"]
    inc = next(x["id"] for x in chans if x["name"] == "incidents")
    check("Slack", "conversations.list")(lambda: f"{len(chans)} channels")
    check("Slack", "conversations.info")(
        lambda: c.conversations_info(channel=inc)["channel"]["name"]
    )
    hist = c.conversations_history(channel=inc, limit=50)["messages"]
    check("Slack", "conversations.history")(lambda: f"{len(hist)} top-level")
    root = next(m for m in hist if m.get("reply_count"))
    check("Slack", "message reactions")(
        lambda: root["reactions"][0]["name"] if root.get("reactions") else 1 / 0
    )
    check("Slack", "conversations.replies")(
        lambda: f"{len(c.conversations_replies(channel=inc, ts=root['ts'])['messages'])} in thread"
    )
    check("Slack", "conversations.members")(
        lambda: f"{len(c.conversations_members(channel=inc)['members'])} members"
    )
    check("Slack", "users.list")(lambda: f"{len(c.users_list()['members'])} users")
    check("Slack", "search.messages")(
        lambda: (
            f"{len(m)} matches"
            if (m := c.search_messages(query="gateway")["messages"]["matches"])
            else 1 / 0
        )
    )


# ------------------------------------------------------------------ Gmail
def _gmail_svc():
    from google.oauth2.credentials import Credentials
    from google.api_core.client_options import ClientOptions
    from googleapiclient.discovery import build

    return build(
        "gmail",
        "v1",
        credentials=Credentials(token=ADMIN),
        client_options=ClientOptions(api_endpoint=BASE),
        static_discovery=True,
    )


def gmail():
    svc = _gmail_svc()
    check("Gmail", "getProfile")(
        lambda: svc.users().getProfile(userId="me").execute()["emailAddress"]
    )
    check("Gmail", "labels.list")(
        lambda: f"{len(svc.users().labels().list(userId='me').execute()['labels'])} labels"
    )
    msgs = svc.users().messages().list(userId="me", maxResults=50).execute().get("messages", [])
    check("Gmail", "messages.list")(lambda: f"{len(msgs)} messages")
    # find the message that has an attachment
    full = None
    for stub in msgs:
        mm = svc.users().messages().get(userId="me", id=stub["id"], format="full").execute()
        parts = mm["payload"].get("parts", [])
        if any(p.get("filename") for p in parts):
            full = mm
            break
    full = (
        full or svc.users().messages().get(userId="me", id=msgs[0]["id"], format="full").execute()
    )
    check("Gmail", "messages.get multipart")(lambda: full["payload"]["mimeType"])
    att = next((p for p in full["payload"].get("parts", []) if p.get("filename")), None)
    check("Gmail", "messages.attachments.get")(
        lambda: (
            len(
                svc.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=full["id"], id=att["body"]["attachmentId"])
                .execute()["data"]
            )
            if att
            else 1 / 0
        )
    )
    qres = svc.users().messages().list(userId="me", q="board").execute().get("messages", [])
    check("Gmail", "messages.list q (free text)")(lambda: f"{len(qres)} match" if qres else 1 / 0)
    fres = svc.users().messages().list(userId="me", q="from:ceo").execute().get("messages", [])
    check("Gmail", "messages.list q (from:)")(lambda: f"{len(fres)} match" if fres else 1 / 0)
    check("Gmail", "threads.list")(
        lambda: (
            f"{len(svc.users().threads().list(userId='me').execute().get('threads', []))} threads"
        )
    )
    check("Gmail", "threads.get")(
        lambda: (
            f"{len(svc.users().threads().get(userId='me', id=msgs[0]['id']).execute()['messages'])} msgs"
        )
    )
    # Served ids must look like Gmail's own (#39): 16 lowercase hex under 2**63. A dsid would be
    # refused by the real API as an invalid id value, so a client written against the mock would
    # only discover that in production.
    check("Gmail", "ids are Gmail-shaped")(
        lambda: (
            f"{len(msgs)} hex ids"
            if all(
                len(m["id"]) == 16 and int(m["id"], 16) < 2**63 and m["id"] == m["id"].lower()
                for m in msgs
            )
            else 1 / 0
        )
    )

    def _unparsable_id():
        """The 400/404 split, through the SDK: a non-hex id is an invalid argument, not a 404."""
        from googleapiclient.errors import HttpError

        try:
            svc.users().messages().get(userId="me", id="not-a-hex-id").execute()
        except HttpError as e:
            return f"{e.resp.status} on a non-hex id" if e.resp.status == 400 else 1 / 0
        raise AssertionError("a non-hex id was accepted")

    check("Gmail", "non-hex id is 400")(_unparsable_id)


# ------------------------------------------------------------------ Drive
def drive():
    from google.oauth2.credentials import Credentials
    from google.api_core.client_options import ClientOptions
    from googleapiclient.discovery import build

    svc = build(
        "drive",
        "v3",
        credentials=Credentials(token=ADMIN),
        client_options=ClientOptions(api_endpoint=f"{BASE}/drive/v3"),
        static_discovery=True,
    )
    files = (
        svc.files()
        .list(pageSize=100, fields="files(id,name,mimeType,capabilities,size)")
        .execute()["files"]
    )
    check("Drive", "files.list")(lambda: f"{len(files)} files")
    by_mime = {f["mimeType"].rsplit(".", 1)[-1]: f for f in files}
    check("Drive", "doc/sheet/slide types")(lambda: ",".join(sorted(by_mime)[:3]))
    sheet = next(f for f in files if f["mimeType"].endswith("spreadsheet"))
    check("Drive", "export Sheet=csv")(
        lambda: svc.files().export(fileId=sheet["id"], mimeType="text/csv").execute()[:12]
    )
    doc = next(f for f in files if f["mimeType"].endswith("document"))
    check("Drive", "export Doc=plain")(
        lambda: svc.files().export(fileId=doc["id"], mimeType="text/plain").execute()[:12]
    )
    pdf = next((f for f in files if f["mimeType"] == "application/pdf"), None)
    check("Drive", "get alt=media (binary)")(
        lambda: len(svc.files().get_media(fileId=pdf["id"]).execute()) if pdf else 1 / 0
    )
    check("Drive", "permissions.list")(
        lambda: (
            f"{len(svc.permissions().list(fileId=files[0]['id']).execute()['permissions'])} perms"
        )
    )
    ftxt = (
        svc.files()
        .list(q="fullText contains 'palette'", fields="files(id,name)")
        .execute()["files"]
    )
    check("Drive", "files.list fullText contains")(lambda: f"{len(ftxt)} match" if ftxt else 1 / 0)
    # about.get is usually a client's first call; the SDK builds it from the discovery doc, so this
    # proves the real request shape (fields + alt=json) reaches the route.
    about = svc.about().get(fields="user,storageQuota").execute()
    check("Drive", "about.get")(
        lambda: (
            f"{about['user']['emailAddress']} {about['storageQuota']['usage']}B"
            if about["user"]["me"]
            else 1 / 0
        )
    )


# ------------------------------------------------------------------ Sheets
def sheets():
    """The Sheets read surface through its own SDK. Worth its own check because the client
    percent-encodes the A1 range into the path (`Sheet1%21A1%3AB2`) and builds the URL from the
    discovery document — neither of which an httpx test exercises."""
    from google.oauth2.credentials import Credentials
    from google.api_core.client_options import ClientOptions
    from googleapiclient.discovery import build

    drive = build(
        "drive",
        "v3",
        credentials=Credentials(token=ADMIN),
        client_options=ClientOptions(api_endpoint=f"{BASE}/drive/v3"),
        static_discovery=True,
    )
    fid = next(
        f["id"]
        for f in drive.files().list(pageSize=100, fields="files(id,mimeType)").execute()["files"]
        if f["mimeType"].endswith("spreadsheet")
    )
    # the service path lives under /sheets here, so the discovery-built URL is /sheets/v4/...
    svc = build(
        "sheets",
        "v4",
        credentials=Credentials(token=ADMIN),
        client_options=ClientOptions(api_endpoint=f"{BASE}/sheets"),
        static_discovery=True,
    )
    vals = svc.spreadsheets().values()
    # a row is a stored line and holds it in ONE cell, commas included (see `_sheets_grid`)
    got = vals.get(spreadsheetId=fid, range="Sheet1!A1:A2").execute()
    check("Sheets", "values.get")(
        lambda: (
            f"{len(got['values'])}x{len(got['values'][0])} {got['range']}"
            if got["values"] == [["month,revenue"], ["Jan,120000"]]
            else 1 / 0
        )
    )
    batch = vals.batchGet(spreadsheetId=fid, ranges=["Sheet1!A1:A1", "A:A"]).execute()
    want = [[["month,revenue"]], [["month,revenue"], ["Jan,120000"], ["Feb,135000"]]]
    check("Sheets", "values.batchGet")(
        lambda: (
            f"{len(batch['valueRanges'])} ranges"
            if [vr["values"] for vr in batch["valueRanges"]] == want
            else 1 / 0
        )
    )
    cols = vals.get(spreadsheetId=fid, range="Sheet1!A1:A3", majorDimension="COLUMNS").execute()[
        "values"
    ]
    check("Sheets", "values.get majorDimension")(
        lambda: "transposed" if cols == [["month,revenue", "Jan,120000", "Feb,135000"]] else 1 / 0
    )
    check("Sheets", "spreadsheets.get")(
        lambda: svc.spreadsheets().get(spreadsheetId=fid).execute()["properties"]["title"]
    )
    # A Doc id is not an entity the Sheets API knows, so it 404s exactly like a nonexistent id —
    # measured against sheets.googleapis.com, not assumed. The SDK surfaces it as HttpError 404.
    from googleapiclient.errors import HttpError

    doc = next(
        f["id"]
        for f in drive.files().list(pageSize=100, fields="files(id,mimeType)").execute()["files"]
        if f["mimeType"].endswith("apps.document")
    )

    def _wrong_type():
        try:
            svc.spreadsheets().get(spreadsheetId=doc).execute()
        except HttpError as e:
            return f"{e.resp.status} on a Doc id" if e.resp.status == 404 else 1 / 0
        raise AssertionError("a Doc id was served as a spreadsheet")

    check("Sheets", "wrong doc type is not found")(_wrong_type)

    # The envelope exists for THIS: googleapiclient parses `error.message` out of the body to build
    # HttpError's reason. Against `{"detail": …}` it fell back to dumping raw bytes, so a client
    # could not read the message it branches on. `error_details` carries the parsed `errors[]`.
    def _error_message_is_readable():
        try:
            svc.spreadsheets().values().get(spreadsheetId=fid, range="Nope!A1").execute()
        except HttpError as e:
            if e.reason != "Unable to parse range: Nope!A1":
                raise AssertionError(f"reason not parsed: {e.reason!r}")
            return f"reason={e.reason[:28]!r}"
        raise AssertionError("a bad range was accepted")

    check("Sheets", "HttpError.reason from the body")(_error_message_is_readable)

    def _drive_error_details():
        """Drive's legacy `errors[]` reaches the SDK as `error_details`, which is where a client
        finds the `reason` it branches on."""
        try:
            drive.files().list(fields="bogusField").execute()
        except HttpError as e:
            got = (e.error_details or [{}])[0]
            if got.get("reason") != "invalidParameter" or got.get("location") != "fields":
                raise AssertionError(f"error_details not parsed: {e.error_details!r}")
            return f"reason={got['reason']}"
        raise AssertionError("a bogus fields mask was accepted")

    check("Drive", "HttpError.error_details reason")(_drive_error_details)


# ------------------------------------------------------------------ GitHub
def github():
    from github import Auth, Github

    gh = Github(auth=Auth.Token(ADMIN), base_url=f"{BASE}/github")
    repo = gh.get_repo(
        "acme/gateway"
    )  # the SAMPLE corpus is @acme.com; owner is echoed by the mock
    check("GitHub", "get_repo")(lambda: repo.full_name)
    issues = list(repo.get_issues(state="all"))
    check("GitHub", "get_issues (issues+PRs)")(lambda: f"{len(issues)} items")
    check("GitHub", "PR marker on /issues")(
        lambda: "yes" if any(i.pull_request for i in issues) else 1 / 0
    )
    an_issue = next(i for i in issues if not i.pull_request)
    check("GitHub", "issue.get_comments")(lambda: f"{len(list(an_issue.get_comments()))} comments")
    prs = list(repo.get_pulls(state="all"))
    check("GitHub", "get_pulls")(lambda: f"{len(prs)} PRs")
    check("GitHub", "pull.get_reviews")(lambda: f"{len(list(prs[0].get_reviews()))} reviews")
    check("GitHub", "get_readme")(lambda: repo.get_readme().name)
    sr = gh.search_issues(query="refill")
    check("GitHub", "search_issues")(lambda: f"{sr.totalCount} hits" if sr.totalCount else 1 / 0)


# ---------------------------------------------- Google OAuth client config
def google_oauth():
    """Drive Gmail via a Google *client config* (not a raw token): the authorized-user
    refresh flow and a service account impersonating a user — both refreshing against the
    mock's /oauth2/token. Proves the config→token-endpoint→usr-token→ACL chain end to end."""
    import json
    import urllib.request
    from google.api_core.client_options import ClientOptions
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials as UserCreds
    from googleapiclient.discovery import build

    with urllib.request.urlopen(f"{BASE}/_mock/credentials") as r:
        creds = json.load(r)
    with urllib.request.urlopen(f"{BASE}/_mock/users") as r:
        who = json.load(r)["users"][0]
    oc, uri = creds["oauth_client"], creds["token_uri"]
    email = who["email"]

    # authorized_user credential = the shared oauth_client + a user's token (from /_mock/users,
    # used as the refresh_token) + the mock's token_uri
    uc = UserCreds(
        None,
        refresh_token=who["token"],
        token_uri=uri,
        client_id=oc["client_id"],
        client_secret=oc["client_secret"],
    )
    g = build(
        "gmail",
        "v1",
        credentials=uc,
        static_discovery=True,
        client_options=ClientOptions(api_endpoint=BASE),
    )
    check("OAuth", "authorized_user refresh")(
        lambda: g.users().getProfile(userId="me").execute()["emailAddress"] == email or 1 / 0
    )

    sa = creds["service_account"]
    sac = service_account.Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/gmail.readonly"], subject=email
    )
    g2 = build(
        "gmail",
        "v1",
        credentials=sac,
        static_discovery=True,
        client_options=ClientOptions(api_endpoint=BASE),
    )
    check("OAuth", "service_account impersonation")(
        lambda: g2.users().getProfile(userId="me").execute()["emailAddress"] == email or 1 / 0
    )


# ------------------------------------------------------------------ Jira
def jira():
    from atlassian import Jira

    j = Jira(url=f"{BASE}/atlassian", username="svc@x", password=ADMIN)
    res = j.get("rest/api/3/search/jql", params={"maxResults": 50})
    check("Jira", "search/jql")(lambda: f"{len(res['issues'])} issues")
    tres = j.get("rest/api/3/search/jql", params={"jql": 'text ~ "latency"'})
    check("Jira", "search/jql text~")(
        lambda: f"{len(tres['issues'])} match" if tres["issues"] else 1 / 0
    )
    key = next(i["key"] for i in res["issues"] if i["fields"]["summary"].startswith("SEV2"))
    iss = j.get(f"rest/api/3/issue/{key}")
    f = iss["fields"]
    check("Jira", "issue comments")(lambda: f"{f['comment']['total']} comments")
    check("Jira", "issue links")(lambda: f"{len(f['issuelinks'])} links")
    check("Jira", "subtasks")(lambda: f"{len(f['subtasks'])} subtasks")
    check("Jira", "issue/{key}/comment")(
        lambda: f"{j.get(f'rest/api/3/issue/{key}/comment')['total']} comments"
    )
    check("Jira", "issueLinkType")(
        lambda: f"{len(j.get('rest/api/3/issueLinkType')['issueLinkTypes'])} types"
    )


# ------------------------------------------------------------------ Confluence
def confluence():
    from atlassian import Confluence

    cf = Confluence(url=f"{BASE}/atlassian/wiki", username="svc@x", password=ADMIN)
    res = cf.get("rest/api/content", params={"limit": 50, "expand": "body.storage"})
    check("Confluence", "content.list")(lambda: f"{len(res['results'])} pages")
    handbook = next(p for p in res["results"] if "Handbook" in p["title"])
    kids = cf.get(f"rest/api/content/{handbook['id']}/child/page")
    check("Confluence", "child/page")(lambda: f"{kids['size']} children")
    child = kids["results"][0]["id"]
    check("Confluence", "child/comment")(
        lambda: f"{cf.get(f'rest/api/content/{child}/child/comment')['size']} comments"
    )
    check("Confluence", "content/{id}/label")(
        lambda: ",".join(x["name"] for x in cf.get(f"rest/api/content/{child}/label")["results"])
    )
    check("Confluence", "ancestors expand")(
        lambda: (
            f"{len(cf.get(f'rest/api/content/{child}', params={'expand': 'ancestors'})['ancestors'])} ancestors"
        )
    )


# ------------------------------------------------------------------ Notion
def notion():
    from notion_client import Client
    from backlot import synth

    c = Client(auth=ADMIN, base_url=f"{BASE}/notion")
    check("Notion", "search")(
        lambda: f"{len(m)} hits" if (m := c.search(query="on-call")["results"]) else 1 / 0
    )
    pid = synth.notion_id("nt-runbook")
    check("Notion", "pages.retrieve")(lambda: c.pages.retrieve(pid)["object"])
    blocks = c.blocks.children.list(pid)["results"]
    check("Notion", "blocks.children.list")(lambda: f"{len(blocks)} blocks" if blocks else 1 / 0)
    did = synth.notion_id("nt-tasks-db")
    db = c.databases.retrieve(did)
    check("Notion", "databases.retrieve")(lambda: db["object"])
    dsid = db["data_sources"][0]["id"]
    check("Notion", "data_sources.retrieve")(lambda: c.data_sources.retrieve(dsid)["object"])
    check("Notion", "data_sources.query")(
        lambda: (
            f"{len(r)} rows"
            if (r := c.data_sources.query(data_source_id=dsid)["results"])
            else 1 / 0
        )
    )
    check("Notion", "users.list")(lambda: f"{len(c.users.list()['results'])} users")
    check("Notion", "users.me")(lambda: c.users.me()["type"])
    check("Notion", "comments.list")(lambda: c.comments.list(block_id=pid)["results"][0]["object"])


def test_sdk_read_coverage(live_server):
    global BASE, ADMIN
    base, settings = live_server
    BASE, ADMIN = base, settings.admin_token
    fns = [slack, gmail, drive, sheets, github, jira, confluence, google_oauth]
    import importlib.util

    if importlib.util.find_spec("notion_client"):  # optional; only when .[examples] is installed
        fns.append(notion)
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - a setup failure shouldn't abort the matrix
            _results.append((fn.__name__.title(), "setup", False, f"{type(e).__name__}: {e}"[:44]))
    failures = [f"{svc}.{name}: {note}" for svc, name, ok, note in _results if not ok]
    assert not failures, f"{len(failures)} SDK check(s) failed:\n" + "\n".join(failures)


# ------------------------------------------------------------------ S3


def _s3_client(base_url, token):
    boto3 = pytest.importorskip("boto3")
    from botocore.config import Config
    from backlot import synth

    return boto3.client(
        "s3",
        endpoint_url=f"{base_url}/s3",
        aws_access_key_id=synth.s3_access_key_id(token),
        aws_secret_access_key=synth.s3_secret_access_key(token),
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )


def test_s3_sdk_read_matrix(live_server):
    base_url, settings = live_server
    s3 = _s3_client(base_url, settings.admin_token)

    names = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert {"eng-artifacts", "people-vault"} <= names

    # us-east-1 is represented by an empty LocationConstraint on real S3; boto3 surfaces that
    # as a falsy value (None), not the literal string "us-east-1".
    assert not s3.get_bucket_location(Bucket="eng-artifacts").get("LocationConstraint")

    listed = s3.list_objects_v2(Bucket="eng-artifacts")
    keys = {o["Key"] for o in listed["Contents"]}
    assert {"runbooks/oncall.md", "design/architecture.md"} <= keys

    pref = s3.list_objects_v2(Bucket="eng-artifacts", Prefix="runbooks/")
    assert {o["Key"] for o in pref["Contents"]} == {"runbooks/oncall.md"}

    obj = s3.get_object(Bucket="eng-artifacts", Key="runbooks/oncall.md")
    body = obj["Body"].read().decode()
    assert body == "Check dashboards, roll back, page on-call."
    assert obj["ContentType"] == "text/markdown"

    head = s3.head_object(Bucket="eng-artifacts", Key="runbooks/oncall.md")
    assert head["ContentLength"] == len(body)

    part = s3.get_object(Bucket="eng-artifacts", Key="runbooks/oncall.md", Range="bytes=0-4")
    assert part["Body"].read().decode() == body[:5]  # inclusive range
    assert part["ContentRange"].endswith(f"/{len(body)}")


def test_hubspot_sdk_read_matrix(live_server):
    """The official client points at the mock through the plain `host` kwarg — no shim. On 8.x that
    kwarg is silently ignored and the client talks to api.hubapi.com, so the host assertion below
    is the guard that a "mock" run is not really hitting production."""
    pytest.importorskip("hubspot")
    from hubspot import HubSpot
    from hubspot.crm.companies import PublicObjectSearchRequest

    base_url, settings = live_server
    api = HubSpot(access_token=settings.admin_token, host=f"{base_url}/hubspot")
    assert base_url in api.crm.companies.basic_api.api_client.configuration.host

    # get_all pages until a response omits paging.next; returning at all proves that contract
    companies = {c.properties.get("name"): c for c in api.crm.companies.get_all()}
    assert {"Acme Health", "Stealth Health Co"} <= set(companies)
    assert companies["Acme Health"].properties["domain"] == "acme-health.com"
    assert companies["Acme Health"].id.isdigit()  # HubSpot ids are numeric strings

    contacts = api.crm.contacts.get_all()
    assert [c.properties.get("email") for c in contacts] == ["ava@acme-health.com"]

    req = PublicObjectSearchRequest(
        filter_groups=[
            {"filters": [{"propertyName": "industry", "operator": "EQ", "value": "healthcare"}]}
        ]
    )
    found = api.crm.companies.search_api.do_search(public_object_search_request=req)
    assert found.total == 2
    assert {r.properties["name"] for r in found.results} == {"Acme Health", "Borealis Clinics"}

    assoc = api.crm.associations.v4.basic_api.get_page(
        object_type="companies", object_id=companies["Acme Health"].id, to_object_type="contacts"
    )
    assert [a.to_object_id for a in assoc.results] == [contacts[0].id]
    assert assoc.results[0].association_types[0].label == "Primary"


def test_hubspot_sdk_acl_scopes_to_user(live_server, tokens):
    """`hs-co-secret` is readable only by hana; ava's listing must not contain it."""
    pytest.importorskip("hubspot")
    from hubspot import HubSpot

    base_url, _ = live_server

    def names(token):
        api = HubSpot(access_token=token, host=f"{base_url}/hubspot")
        return {c.properties.get("name") for c in api.crm.companies.get_all()}

    assert "Stealth Health Co" not in names(tokens["ava@acme.com"])
    assert "Stealth Health Co" in names(tokens["hana@acme.com"])


def test_s3_sdk_acl_scopes_to_user(live_server, tokens):
    base_url, settings = live_server
    s3 = _s3_client(base_url, tokens["ava@acme.com"])  # engineering, not people
    names = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert "eng-artifacts" in names and "people-vault" not in names
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket="people-vault", Key="comp/bands.csv")
    assert e.value.response["Error"]["Code"] in ("NoSuchKey", "NoSuchBucket", "AccessDenied")
