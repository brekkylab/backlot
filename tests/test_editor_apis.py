"""New read surfaces added for filesystem-style clients (e.g. mirage):

- Drive root is navigable — ``'root' in parents`` returns folder objects whose ids match what
  files in them report as their parent, and shared-drives enumeration doesn't 404.
- The Workspace editor read APIs (Docs / Sheets / Slides) serve a native doc's content in each
  API's response shape, keyed on the Drive file id and ACL-enforced.
- A Slack channel's ``created`` never postdates its messages.

Driven over the shared SAMPLE corpus via the ``live_server`` subprocess.
"""
from __future__ import annotations

from urllib.parse import quote

import httpx
import pytest


@pytest.fixture(scope="module")
def base(live_server):
    return live_server[0]


@pytest.fixture(scope="module")
def admin_h(live_server):
    return {"Authorization": f"Bearer {live_server[1].admin_token}"}


def _drive_by_mime(base, admin_h, mime):
    """A visible Drive file id + name for the given native mimeType."""
    r = httpx.get(f"{base}/drive/v3/files", headers=admin_h,
                  params={"q": "trashed=false", "pageSize": 1000}).json()
    for f in r["files"]:
        if f["mimeType"] == mime:
            return f["id"], f["name"]
    raise AssertionError(f"no {mime} in corpus")


# --- Drive navigability ---------------------------------------------------------

def test_shared_drives_empty(base, admin_h):
    r = httpx.get(f"{base}/drive/v3/drives", headers=admin_h, params={"fields": "drives(id,name)"})
    assert r.status_code == 200
    assert r.json()["drives"] == []


def test_root_lists_folders_with_matching_ids(base, admin_h):
    r = httpx.get(f"{base}/drive/v3/files", headers=admin_h,
                  params={"q": "'root' in parents and trashed=false", "pageSize": 1000}).json()
    folders = r["files"]
    assert folders, "root should expose folder objects"
    assert all(f["mimeType"] == "application/vnd.google-apps.folder" for f in folders)
    names = {f["name"] for f in folders}
    assert {"marketing", "finance"} <= names

    # a folder's id must equal what its children report as their parent, so a client can descend
    finance = next(f for f in folders if f["name"] == "finance")
    kids = httpx.get(f"{base}/drive/v3/files", headers=admin_h,
                     params={"q": f"'{finance['id']}' in parents and trashed=false"}).json()["files"]
    assert kids and all(finance["id"] in k["parents"] for k in kids)
    # and GET on the folder id resolves to the folder object
    got = httpx.get(f"{base}/drive/v3/files/{finance['id']}", headers=admin_h).json()
    assert got["mimeType"] == "application/vnd.google-apps.folder" and got["name"] == "finance"


# --- Workspace editor read APIs -------------------------------------------------

def test_docs_get_returns_paragraph_text(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.document")
    doc = httpx.get(f"{base}/docs/v1/documents/{fid}", headers=admin_h).json()
    assert doc["documentId"] == fid
    text = "".join(e["textRun"]["content"]
                   for el in doc["body"]["content"] if "paragraph" in el
                   for e in el["paragraph"]["elements"])
    assert "Logo usage" in text  # SAMPLE "Brand guidelines v3"


def test_sheets_get_returns_grid(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    sh = httpx.get(f"{base}/sheets/v4/spreadsheets/{fid}", headers=admin_h).json()
    assert sh["spreadsheetId"] == fid
    rows = sh["sheets"][0]["data"][0]["rowData"]
    cells = [[c.get("formattedValue") for c in row["values"]] for row in rows]
    assert ["month", "revenue"] in cells and ["Jan", "120000"] in cells


def test_slides_get_returns_slides(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.presentation")
    pr = httpx.get(f"{base}/slides/v1/presentations/{fid}", headers=admin_h).json()
    assert pr["presentationId"] == fid and len(pr["slides"]) >= 1
    text = "".join(t["textRun"]["content"]
                   for s in pr["slides"] for pe in s["pageElements"]
                   for t in pe["shape"]["text"]["textElements"])
    assert "Slide 1" in text


def test_editor_apis_enforce_acl(base, live_server):
    """The finance spreadsheet is group-restricted; a non-member gets 404, not the content."""
    import yaml
    tokens = {u["email"]: u["token"]
              for u in yaml.safe_load(live_server[1].tokens_path.read_text())["users"]}
    admin_h = {"Authorization": f"Bearer {live_server[1].admin_token}"}
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    outsider = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}  # marketing, not finance
    assert httpx.get(f"{base}/sheets/v4/spreadsheets/{fid}", headers=outsider).status_code == 404


# --- Sheets values.get / values.batchGet ----------------------------------------
#
# The SAMPLE spreadsheet ("Q1 Revenue Model") is a 3x2 grid, which is what every range form below
# is measured against:
#
#        A        B
#   1  month    revenue
#   2  Jan      120000
#   3  Feb      135000

GRID = [["month", "revenue"], ["Jan", "120000"], ["Feb", "135000"]]


@pytest.fixture(scope="module")
def sheet_id(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    return fid


def _values(base, headers, sheet_id, rng, **params):
    return httpx.get(f"{base}/sheets/v4/spreadsheets/{sheet_id}/values/{quote(rng, safe='')}",
                     headers=headers, params=params)


def _batch(base, headers, sheet_id, ranges, **params):
    return httpx.get(f"{base}/sheets/v4/spreadsheets/{sheet_id}/values:batchGet",
                     headers=headers, params=[("ranges", r) for r in ranges] + list(params.items()))


@pytest.mark.parametrize("rng, expected", [
    ("Sheet1", GRID),                                   # whole sheet
    ("Sheet1!A1:B3", GRID),                             # explicit bounds
    ("A1:B3", GRID),                                    # sheet name omitted
    ("Sheet1!A1:B2", GRID[:2]),                         # sub-range
    ("Sheet1!B2", [["120000"]]),                        # single cell
    ("A:A", [["month"], ["Jan"], ["Feb"]]),             # whole column
    ("1:1", [GRID[0]]),                                 # whole row
    ("Sheet1!A2:B", GRID[1:]),                          # unbounded lower edge
    ("'Sheet1'!A1:B1", [GRID[0]]),                      # quoted sheet name
])
def test_sheets_values_get_range_forms(base, admin_h, sheet_id, rng, expected):
    """Every A1 form a client may send has to resolve against the same grid. Without the parser
    each of these is a 404 on a route that does not exist."""
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 200, r.text
    assert r.json()["values"] == expected


def test_sheets_values_get_accepts_an_unencoded_range(base, admin_h, sheet_id):
    """`!` and `:` are legal in a path segment, so a hand-written URL must work as well as the
    percent-encoded one google-api-python-client sends."""
    r = httpx.get(f"{base}/sheets/v4/spreadsheets/{sheet_id}/values/Sheet1!A1:B2", headers=admin_h)
    assert r.status_code == 200
    assert r.json()["values"] == GRID[:2]


def test_sheets_values_get_echoes_the_normalized_range(base, admin_h, sheet_id):
    """A client caches on `range`, so the response names the resolved range in full A1 form —
    sheet included — however the request spelled it."""
    assert _values(base, admin_h, sheet_id, "A1:B2").json()["range"] == "Sheet1!A1:B2"
    assert _values(base, admin_h, sheet_id, "Sheet1").json()["range"] == "Sheet1!A1:B3"


def test_sheets_values_get_defaults_to_rows(base, admin_h, sheet_id):
    assert _values(base, admin_h, sheet_id, "Sheet1!A1:B3").json()["majorDimension"] == "ROWS"


def test_sheets_values_get_major_dimension_columns_transposes(base, admin_h, sheet_id):
    j = _values(base, admin_h, sheet_id, "Sheet1!A1:B3", majorDimension="COLUMNS").json()
    assert j["majorDimension"] == "COLUMNS"
    assert j["values"] == [["month", "Jan", "Feb"], ["revenue", "120000", "135000"]]


def test_sheets_values_get_trims_trailing_empties(base, admin_h, sheet_id):
    """Real Sheets does not pad a range out to its bounds: a row stops at its last non-empty cell
    and the block stops at its last non-empty row. Padding would make a client read phantom
    columns that the grid does not have."""
    j = _values(base, admin_h, sheet_id, "Sheet1!A1:D5").json()
    assert j["values"] == GRID          # not 5 rows, not 4 columns


def test_sheets_values_get_omits_values_when_the_range_is_empty(base, admin_h, sheet_id):
    """An empty range answers 200 with NO `values` key at all — not `[]`. A client testing
    `"values" in resp` is the documented way to tell empty from present."""
    r = _values(base, admin_h, sheet_id, "Sheet1!D1:E2")
    assert r.status_code == 200
    assert "values" not in r.json()
    assert r.json()["range"] == "Sheet1!D1:E2"


@pytest.mark.parametrize("rng", ["Other!A1:B2", "not a range", "A1:", "!A1", ""])
def test_sheets_values_get_rejects_an_unusable_range(base, admin_h, sheet_id, rng):
    """The mock has exactly one sheet, `Sheet1`; naming another is as unresolvable as a malformed
    reference, and real Sheets 400s on both rather than returning an empty grid."""
    assert _values(base, admin_h, sheet_id, rng).status_code == 400


@pytest.mark.parametrize("params", [{"majorDimension": "DIAGONAL"},
                                    {"valueRenderOption": "NOPE"}])
def test_sheets_values_get_rejects_a_bad_enum(base, admin_h, sheet_id, params):
    assert _values(base, admin_h, sheet_id, "Sheet1!A1:B2", **params).status_code == 400


def test_sheets_values_get_render_options_agree_on_this_corpus(base, admin_h, sheet_id):
    """The corpus stores no formulas and no typed numbers — `spreadsheets.get` already declares
    every cell a `stringValue` — so the three render options coincide here. Asserted so that a
    future change which makes them diverge has to say so."""
    out = {opt: _values(base, admin_h, sheet_id, "Sheet1!A1:B3",
                        valueRenderOption=opt).json()["values"]
           for opt in ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")}
    assert out["FORMATTED_VALUE"] == out["UNFORMATTED_VALUE"] == out["FORMULA"] == GRID


def test_sheets_values_get_agrees_with_spreadsheets_get(base, admin_h, sheet_id):
    """Two views of one document: the grid `values.get` serves must be the grid the structured
    read serves, or a client gets a different answer depending on which call it made."""
    sh = httpx.get(f"{base}/sheets/v4/spreadsheets/{sheet_id}", headers=admin_h).json()
    structured = [[c["formattedValue"] for c in row["values"]]
                  for row in sh["sheets"][0]["data"][0]["rowData"]]
    assert _values(base, admin_h, sheet_id, "Sheet1").json()["values"] == structured


def test_sheets_values_get_enforces_the_acl(base, live_server, sheet_id):
    """The finance spreadsheet is group-restricted; the values route must not be a way around the
    ACL that `spreadsheets.get` enforces."""
    import yaml
    tokens = {u["email"]: u["token"]
              for u in yaml.safe_load(live_server[1].tokens_path.read_text())["users"]}
    outsider = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}  # marketing, not finance
    admin_h = {"Authorization": f"Bearer {live_server[1].admin_token}"}
    # the admin arm is what keeps this honest: without it a missing route 404s and the test passes
    assert _values(base, admin_h, sheet_id, "Sheet1").status_code == 200
    assert _batch(base, admin_h, sheet_id, ["Sheet1"]).status_code == 200
    assert _values(base, outsider, sheet_id, "Sheet1").status_code == 404
    assert _batch(base, outsider, sheet_id, ["Sheet1"]).status_code == 404


def test_sheets_values_get_needs_auth(base, sheet_id):
    assert _values(base, {}, sheet_id, "Sheet1").status_code == 401


def test_sheets_batch_get_returns_one_value_range_per_request_range(base, admin_h, sheet_id):
    j = _batch(base, admin_h, sheet_id, ["Sheet1!A1:B1", "Sheet1!A3:B3"]).json()
    assert j["spreadsheetId"] == sheet_id
    assert [vr["range"] for vr in j["valueRanges"]] == ["Sheet1!A1:B1", "Sheet1!A3:B3"]
    assert [vr["values"] for vr in j["valueRanges"]] == [[GRID[0]], [GRID[2]]]


def test_sheets_batch_get_matches_the_single_get_for_each_range(base, admin_h, sheet_id):
    """batchGet is N single gets through one resolver; if the two disagree, batching changes
    meaning rather than saving round trips."""
    ranges = ["Sheet1", "A1:B2", "Sheet1!B2", "A:A", "Sheet1!D1:E2"]
    batched = _batch(base, admin_h, sheet_id, ranges).json()["valueRanges"]
    singles = [_values(base, admin_h, sheet_id, r).json() for r in ranges]
    assert batched == singles


def test_sheets_batch_get_honors_major_dimension(base, admin_h, sheet_id):
    j = _batch(base, admin_h, sheet_id, ["Sheet1!A1:B3"], majorDimension="COLUMNS").json()
    assert j["valueRanges"][0]["values"] == [["month", "Jan", "Feb"],
                                            ["revenue", "120000", "135000"]]


def test_sheets_batch_get_fails_the_whole_call_on_one_bad_range(base, admin_h, sheet_id):
    """A partial batch would leave the caller unable to tell which range it is missing, so real
    Sheets rejects the request outright."""
    assert _batch(base, admin_h, sheet_id, ["Sheet1!A1:B1", "Other!A1"]).status_code == 400


def test_sheets_batch_get_with_no_ranges_selects_nothing(base, admin_h, sheet_id):
    """`ranges` has no default, so an empty range list selects no data. NOTE: this is the natural
    reading of the API, NOT a behaviour diffed against real Sheets — see the route's comment."""
    r = _batch(base, admin_h, sheet_id, [])
    assert r.status_code == 200
    assert r.json()["spreadsheetId"] == sheet_id
    assert "valueRanges" not in r.json()


# --- Slack timestamp consistency ------------------------------------------------

def test_channel_created_not_after_messages(base, admin_h):
    channels = httpx.get(f"{base}/slack/api/conversations.list", headers=admin_h).json()["channels"]
    assert channels
    for ch in channels:
        hist = httpx.get(f"{base}/slack/api/conversations.history", headers=admin_h,
                         params={"channel": ch["id"], "limit": 1}).json()
        msgs = hist.get("messages", [])
        if msgs:
            assert ch["created"] <= float(msgs[0]["ts"]), f"#{ch['name']} created after its message"


def test_history_honors_oldest_latest(base, admin_h):
    """A time-bounded fetch (as a filesystem client makes per day) is filtered by ts — a tight
    window keeps the message, a window entirely after it drops the message."""
    cid = httpx.get(f"{base}/slack/api/conversations.list", headers=admin_h).json()["channels"][0]["id"]
    ts = float(httpx.get(f"{base}/slack/api/conversations.history", headers=admin_h,
                         params={"channel": cid, "limit": 1}).json()["messages"][0]["ts"])

    tight = httpx.get(f"{base}/slack/api/conversations.history", headers=admin_h,
                      params={"channel": cid, "oldest": ts - 5, "latest": ts + 5,
                              "inclusive": "true", "limit": 1000}).json()["messages"]
    assert any(abs(float(m["ts"]) - ts) < 1e-6 for m in tight)

    after = httpx.get(f"{base}/slack/api/conversations.history", headers=admin_h,
                      params={"channel": cid, "oldest": ts + 1, "latest": ts + 100,
                              "limit": 1000}).json()["messages"]
    assert all(float(m["ts"]) > ts for m in after)  # the sampled message is excluded
