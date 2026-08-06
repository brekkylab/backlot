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


def test_sheets_get_withholds_grid_data_by_default(base, admin_h):
    """Measured: a plain `spreadsheets.get` returns `sheets[i].properties` and NO `data` — on a real
    workbook that is 4 KB against 5.7 MB with `includeGridData=true`. The mock used to volunteer the
    full grid on every call, so a reader got cells here that it would never get from Google, and the
    document it built had a different layout in the two environments.

    `ranges` alone does not unlock it either — also measured."""
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    for params in ({}, {"ranges": "Sheet1!A1:A2"}):
        sh = httpx.get(f"{base}/sheets/v4/spreadsheets/{fid}", headers=admin_h, params=params).json()
        assert sh["spreadsheetId"] == fid
        assert set(sh["sheets"][0]) == {"properties"}, params
    props = sh["sheets"][0]["properties"]
    # the measured key set of a real sheet's properties
    assert set(props) == {"sheetId", "title", "index", "sheetType", "gridProperties"}
    assert props["gridProperties"] == {"rowCount": 1000, "columnCount": 26}


def test_sheets_get_returns_grid_when_asked(base, admin_h):
    """One row per stored line, one cell per row holding the line verbatim. This used to split on
    commas, which over the real corpus manufactured columns out of prose punctuation — see
    `_sheets_grid`; the corpus has no delimiter-uniform CSV at all."""
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    sh = httpx.get(f"{base}/sheets/v4/spreadsheets/{fid}", headers=admin_h,
                   params={"includeGridData": "true"}).json()
    data = sh["sheets"][0]["data"][0]
    assert "startRow" not in data and "startColumn" not in data, "zeros are omitted, as proto3 does"
    rows = data["rowData"]
    # a cell object per column of the range (26), the empty ones carrying no value — measured shape
    assert {len(r["values"]) for r in rows} == {26}
    assert all(c == {} for r in rows for c in r["values"][1:])
    assert [r["values"][0]["formattedValue"] for r in rows] == \
        ["month,revenue", "Jan,120000", "Feb,135000"]


def test_sheets_get_grid_data_honours_ranges(base, admin_h):
    """Measured: `ranges` + `includeGridData` scopes the returned rowData to the range (a real
    workbook went 5.7 MB -> 11 KB for `A1:B2`)."""
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    sh = httpx.get(f"{base}/sheets/v4/spreadsheets/{fid}", headers=admin_h,
                   params={"includeGridData": "true", "ranges": "Sheet1!A2:A3"}).json()
    data = sh["sheets"][0]["data"][0]
    assert data["startRow"] == 1
    cells = [[c.get("formattedValue") for c in row["values"]] for row in data["rowData"]]
    assert cells == [["Jan,120000"], ["Feb,135000"]]


def test_slides_get_returns_slides(base, admin_h):
    fid, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.presentation")
    pr = httpx.get(f"{base}/slides/v1/presentations/{fid}", headers=admin_h).json()
    assert pr["presentationId"] == fid and len(pr["slides"]) >= 1
    text = "".join(t["textRun"]["content"]
                   for s in pr["slides"] for pe in s["pageElements"]
                   for t in pe["shape"]["text"]["textElements"])
    assert "Slide 1" in text


# The three refusals below were MEASURED against the live Google APIs (docs.googleapis.com,
# sheets.googleapis.com, slides.googleapis.com) with real OAuth credentials, one call per cell:
#
#   target passed to API X                     | result
#   -------------------------------------------|----------------------------------------------
#   a DIFFERENT native Workspace type          | 404 NOT_FOUND  "Requested entity was not found."
#   an Office file of X's own family           | 400 FAILED_PRECONDITION  (Office message)
#   any other non-native (pdf/txt/folder/…)    | 400 INVALID_ARGUMENT  "Request contains an
#                                              |     invalid argument."
#   a nonexistent id                           | 404 NOT_FOUND  (same as row 1)
#
# The first row is the surprise: a Doc id is not a "bad spreadsheet" to the Sheets API, it is
# simply not an entity it knows, and it is indistinguishable from an id that does not exist.
NOT_FOUND = "Requested entity was not found."
INVALID_ARG = "Request contains an invalid argument."
OFFICE_MSG = ("This operation is not supported for this document. "
              "The document must not be an Office file.")


def test_editor_apis_treat_another_native_type_as_not_found(base, admin_h):
    """Measured: 404 "Requested entity was not found." — the SAME answer a nonexistent id gets.
    The mock used to serve these 200, reinterpreting the file: a Doc read through the Sheets API
    came back as a "grid" of prose, plausible enough that a client would trust it."""
    doc, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.document")
    sheet, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    deck, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.presentation")
    for path, label in [
        (f"/sheets/v4/spreadsheets/{doc}", "a Doc through Sheets"),
        (f"/sheets/v4/spreadsheets/{deck}", "a Deck through Sheets"),
        (f"/docs/v1/documents/{sheet}", "a Sheet through Docs"),
        (f"/docs/v1/documents/{deck}", "a Deck through Docs"),
        (f"/slides/v1/presentations/{doc}", "a Doc through Slides"),
        (f"/slides/v1/presentations/{sheet}", "a Sheet through Slides"),
    ]:
        r = httpx.get(f"{base}{path}", headers=admin_h)
        assert r.status_code == 404, f"{label}: {r.status_code}"
        assert r.json()["error"]["message"] == NOT_FOUND, label
    # and it is the same answer as an id that does not exist at all — body and all
    assert httpx.get(f"{base}/sheets/v4/spreadsheets/no-such-id", headers=admin_h).json() == \
        {"error": {"code": 404, "message": NOT_FOUND, "status": "NOT_FOUND"}}
    # each API still serves its OWN type — without this arm a blanket 404 would pass
    assert httpx.get(f"{base}/docs/v1/documents/{doc}", headers=admin_h).status_code == 200
    assert httpx.get(f"{base}/sheets/v4/spreadsheets/{sheet}", headers=admin_h).status_code == 200
    assert httpx.get(f"{base}/slides/v1/presentations/{deck}", headers=admin_h).status_code == 200


def test_sheets_values_treat_another_native_type_as_not_found(base, admin_h):
    """The values routes go through the same guard, so they cannot become the way around it."""
    doc, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.document")
    for r in (_values(base, admin_h, doc, "Sheet1"), _batch(base, admin_h, doc, ["Sheet1"])):
        assert r.status_code == 404
        assert r.json()["error"]["message"] == NOT_FOUND


def test_editor_apis_reject_a_non_native_file(base, admin_h):
    """A PDF is not a Workspace document in any family: measured 400 "Request contains an invalid
    argument." on all three APIs — a different answer from another native type, which 404s."""
    pdf, _ = _drive_by_mime(base, admin_h, "application/pdf")
    for path in (f"/sheets/v4/spreadsheets/{pdf}", f"/docs/v1/documents/{pdf}",
                 f"/slides/v1/presentations/{pdf}"):
        r = httpx.get(f"{base}{path}", headers=admin_h)
        assert r.status_code == 400, path
        assert r.json()["error"]["message"] == INVALID_ARG, path


def test_editor_apis_reject_an_office_file_of_their_own_family(base, admin_h):
    """The one case the third-party bug reports were actually about, and it is narrower than they
    suggest: an Office file gets the Office-specific FAILED_PRECONDITION message ONLY from the API
    that owns its family. Measured both ways round — xlsx to Sheets and docx to Docs give the
    Office message, while xlsx to Docs and docx to Sheets give the plain invalid-argument one."""
    xlsx, _ = _drive_by_mime(
        base, admin_h,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    r = httpx.get(f"{base}/sheets/v4/spreadsheets/{xlsx}", headers=admin_h)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == OFFICE_MSG
    # the same file through the other two APIs is just an invalid argument
    for path in (f"/docs/v1/documents/{xlsx}", f"/slides/v1/presentations/{xlsx}"):
        assert httpx.get(f"{base}{path}", headers=admin_h).json()["error"]["message"] == INVALID_ARG


def test_editor_apis_reject_a_folder(base, admin_h):
    """A folder id is reachable — a client walking Drive holds them — and real Google answers 400
    invalid-argument rather than pretending the folder is a document."""
    folder = httpx.get(f"{base}/drive/v3/files", headers=admin_h,
                       params={"q": "'root' in parents", "pageSize": 1}).json()["files"][0]["id"]
    r = httpx.get(f"{base}/docs/v1/documents/{folder}", headers=admin_h)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == INVALID_ARG


def test_wrong_type_is_refused_before_it_is_read(base, live_server):
    """A caller who cannot see the file still gets 404, not 400: the type of a document you have
    no access to is not something the API should confirm."""
    import yaml
    tokens = {u["email"]: u["token"]
              for u in yaml.safe_load(live_server[1].tokens_path.read_text())["users"]}
    admin_h = {"Authorization": f"Bearer {live_server[1].admin_token}"}
    sheet, _ = _drive_by_mime(base, admin_h, "application/vnd.google-apps.spreadsheet")
    outsider = {"Authorization": f"Bearer {tokens['mia@acme.com']}"}  # cannot see the finance sheet
    assert httpx.get(f"{base}/docs/v1/documents/{sheet}", headers=outsider).status_code == 404


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
# A spreadsheet's stored content is one text blob, and a line break is the only structure it
# actually has — so a row is a line and a row has ONE cell holding that line verbatim. The SAMPLE
# spreadsheet ("Q1 Revenue Model") stores "month,revenue\nJan,120000\nFeb,135000", which is:
#
#          A
#   1  month,revenue
#   2  Jan,120000
#   3  Feb,135000
#
# The commas stay inside the cell. Splitting on them would be a delimiter policy, and the bench
# corpus says the mock has no business guessing one: of its 1,875 `doc_type: sheet` records, NONE
# is delimiter-uniform CSV — 82.6% are prose and 17.4% are prose around a PIPE-delimited table.

GRID = [["month,revenue"], ["Jan,120000"], ["Feb,135000"]]


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
    ("Sheet1!A1:A3", GRID),                             # explicit bounds
    ("A1:A3", GRID),                                    # sheet name omitted
    ("Sheet1!A1:B3", GRID),                             # column B is empty, so it trims away
    ("Sheet1!A1:A2", GRID[:2]),                         # sub-range
    ("Sheet1!A2", [["Jan,120000"]]),                    # single cell keeps its commas
    ("A:A", GRID),                                      # whole column
    ("1:1", [GRID[0]]),                                 # whole row
    ("Sheet1!A2:A", GRID[1:]),                          # unbounded lower edge
    ("'Sheet1'!A1:A1", [GRID[0]]),                      # quoted sheet name
])
def test_sheets_values_get_range_forms(base, admin_h, sheet_id, rng, expected):
    """Every A1 form a client may send has to resolve against the same grid. Without the parser
    each of these is a 404 on a route that does not exist."""
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 200, r.text
    assert r.json()["values"] == expected


def test_sheets_values_get_keeps_a_line_intact(base, admin_h, sheet_id):
    """The cell holds the whole line, commas and all. Splitting on commas is what the mock used to
    do, and over the real corpus it manufactured columns out of sentence punctuation — a prose line
    like "customer dates, ARR exposure, highest-risk deals" became three cells of a table that
    never existed. Which delimiter (if any) applies is the corpus owner's call, not the mock's."""
    j = _values(base, admin_h, sheet_id, "Sheet1!A1").json()
    assert j["values"] == [["month,revenue"]]
    # and there is exactly one column: B is past the end of every row
    assert "values" not in _values(base, admin_h, sheet_id, "Sheet1!B1:B3").json()


def test_sheets_values_round_trips_the_stored_text(base, admin_h, sheet_id):
    """The invariant that makes "serve it as-is" checkable: the cells of the whole sheet, joined
    by newlines, reproduce byte-for-byte what Drive's CSV export serves. If a future splitter
    breaks that, it is inventing or dropping something.

    A blank line comes back as ``[]``, not ``[""]`` — trailing-empty trimming empties the row, which
    is also what real Sheets returns for an interior blank row. So the reconstruction has to read an
    empty row as an empty line: a naive ``cells[0]`` passes on the SAMPLE sheet (it has no blank
    lines) and raises IndexError on a real corpus, which is why a blank line is asserted below."""
    export = httpx.get(f"{base}/drive/v3/files/{sheet_id}/export", headers=admin_h,
                       params={"mimeType": "text/csv"}).text
    rows = _values(base, admin_h, sheet_id, "Sheet1").json()["values"]
    assert "\n".join((cells[0] if cells else "") for cells in rows) == export


def test_sheets_values_serve_a_blank_line_as_an_empty_row(base, admin_h):
    """A blank line is an empty row ``[]``, not a row holding ``""`` — trailing-empty trimming
    empties it, which is what real Sheets returns for an interior blank row (measured: a real
    whole-sheet read came back with row widths {0, 4, 5, 6}).

    ``gd-blankline`` stores "header\\n\\nrow after gap\\n\\n": a gap in the middle and two at the
    end. The trailing ones trim away entirely; the middle one survives as ``[]``."""
    j = _values(base, admin_h, "gd-blankline", "Sheet1").json()
    assert j["values"] == [["header"], [], ["row after gap"]]
    # and the round trip still holds, blank lines and all — trailing gaps included
    export = httpx.get(f"{base}/drive/v3/files/gd-blankline/export", headers=admin_h,
                       params={"mimeType": "text/csv"}).text
    rebuilt = "\n".join((c[0] if c else "") for c in j["values"])
    assert rebuilt == export.rstrip("\n")
    assert export.endswith("\n\n"), "the stored trailing gap is still in the exported text"


def test_sheets_values_get_accepts_an_unencoded_range(base, admin_h, sheet_id):
    """`!` and `:` are legal in a path segment, so a hand-written URL must work as well as the
    percent-encoded one google-api-python-client sends."""
    r = httpx.get(f"{base}/sheets/v4/spreadsheets/{sheet_id}/values/Sheet1!A1:A2", headers=admin_h)
    assert r.status_code == 200
    assert r.json()["values"] == GRID[:2]


def test_sheets_values_get_echoes_the_normalized_range(base, admin_h, sheet_id):
    """A client caches on `range`, so the response names the resolved range in full A1 form —
    sheet included — however the request spelled it."""
    assert _values(base, admin_h, sheet_id, "A1:A2").json()["range"] == "Sheet1!A1:A2"
    # an unbounded edge resolves against the GRID, not the data — measured: a 14-row real sheet
    # answers `values/<title>` with `A1:Z1000`, not `A1:D14`
    assert _values(base, admin_h, sheet_id, "Sheet1").json()["range"] == "Sheet1!A1:Z1000"
    assert _values(base, admin_h, sheet_id, "A:A").json()["range"] == "Sheet1!A1:A1000"
    assert _values(base, admin_h, sheet_id, "1:1").json()["range"] == "Sheet1!A1:Z1"


def test_sheets_values_get_defaults_to_rows(base, admin_h, sheet_id):
    assert _values(base, admin_h, sheet_id, "Sheet1!A1:A3").json()["majorDimension"] == "ROWS"


def test_sheets_values_get_major_dimension_columns_transposes(base, admin_h, sheet_id):
    j = _values(base, admin_h, sheet_id, "Sheet1!A1:A3", majorDimension="COLUMNS").json()
    assert j["majorDimension"] == "COLUMNS"
    # one column, holding every line in order
    assert j["values"] == [["month,revenue", "Jan,120000", "Feb,135000"]]


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


@pytest.mark.parametrize("params, field, enum", [
    ({"majorDimension": "DIAGONAL"}, "major_dimension", "Dimension"),
    ({"valueRenderOption": "NOPE"}, "value_render_option", "ValueRenderOption"),
])
def test_sheets_values_get_rejects_a_bad_enum(base, admin_h, sheet_id, params, field, enum):
    """Measured message shape, not an invented one: Google names the proto field and type."""
    r = _values(base, admin_h, sheet_id, "Sheet1!A1:A2", **params)
    assert r.status_code == 400
    bad = next(iter(params.values()))
    assert r.json()["error"]["message"] == (
        f"Invalid value at \'{field}\' "
        f"(type.googleapis.com/google.apps.sheets.v4.{enum}), \"{bad}\"")


def test_sheets_values_get_render_options_agree_on_this_corpus(base, admin_h, sheet_id):
    """The corpus stores no formulas and no typed numbers — `spreadsheets.get` already declares
    every cell a `stringValue` — so the three render options coincide here. Asserted so that a
    future change which makes them diverge has to say so."""
    out = {opt: _values(base, admin_h, sheet_id, "Sheet1!A1:A3",
                        valueRenderOption=opt).json()["values"]
           for opt in ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")}
    assert out["FORMATTED_VALUE"] == out["UNFORMATTED_VALUE"] == out["FORMULA"] == GRID


def test_sheets_values_get_agrees_with_spreadsheets_get(base, admin_h, sheet_id):
    """Two views of one document: the grid `values.get` serves must be the grid the structured
    read serves, or a client gets a different answer depending on which call it made."""
    sh = httpx.get(f"{base}/sheets/v4/spreadsheets/{sheet_id}", headers=admin_h,
                   params={"includeGridData": "true"}).json()
    # the grid pads each row to the range width with empty cells; `values` trims them. Drop the
    # padding and the two must name the same cells.
    structured = [[c["formattedValue"] for c in row["values"] if c]
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
    # Sheets accepts API keys, so no header at all is 403 PERMISSION_DENIED; a bad bearer is 401.
    # Both measured against the live API.
    assert _values(base, {}, sheet_id, "Sheet1").status_code == 403
    assert _values(base, {"Authorization": "Bearer nope"}, sheet_id,
                   "Sheet1").status_code == 401


def test_sheets_values_get_clamps_a_range_that_overflows_the_grid(base, admin_h, sheet_id):
    """Measured: an END past the grid is CLAMPED, not refused — `A1:AA5` on a 26-column sheet comes
    back as `A1:Z5`, and `A1:B1001` as `A1:B1000`."""
    assert _values(base, admin_h, sheet_id, "A1:AA5").json()["range"] == "Sheet1!A1:Z5"
    assert _values(base, admin_h, sheet_id, "A1:B1001").json()["range"] == "Sheet1!A1:B1000"
    assert _values(base, admin_h, sheet_id, "Z1:AA5").json()["range"] == "Sheet1!Z1:Z5"


@pytest.mark.parametrize("rng", ["AA1:AB5", "ZZ1:ZZ5", "A1001:B1002", "AA1001:AB1002"])
def test_sheets_values_get_rejects_a_start_outside_the_grid(base, admin_h, sheet_id, rng):
    """Measured: the START must sit inside the grid. Overflowing the end clamps; starting outside
    is an error naming the limits."""
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 400
    assert r.json()["error"]["message"].startswith("Range (Sheet1!")
    assert r.json()["error"]["message"].endswith(
        "exceeds grid limits. Max rows: 1000, max columns: 26")


def test_sheets_values_get_empty_inside_the_grid_is_not_an_error(base, admin_h, sheet_id):
    """Measured: a range inside the grid but past the data answers 200 with the range echoed and no
    `values` key — distinct from a range that starts outside the grid, which 400s."""
    for rng in ("A100:B101", "A100", "Z1:Z5"):
        j = _values(base, admin_h, sheet_id, rng).json()
        assert "values" not in j, rng
        assert j["range"].startswith("Sheet1!"), rng


# Every (range -> echoed range) pair below was compared side by side against the live Sheets API on
# a real spreadsheet, normalising only the sheet title. 19 of 21 cases came back byte-identical; the
# other two (`A1`, `'Sheet1'!A1:A1`) differ only because that spreadsheet's A1 is blank while the
# SAMPLE's is not — same status, same echo. Pinned here so the parser cannot drift back.
MEASURED_ECHO = [
    ("Sheet1", "Sheet1!A1:Z1000"),      ("Sheet1!A1:A2", "Sheet1!A1:A2"),
    ("A1:A2", "Sheet1!A1:A2"),          ("A:A", "Sheet1!A1:A1000"),
    ("1:1", "Sheet1!A1:Z1"),            ("Sheet1!A2:A", "Sheet1!A2:A1000"),
    ("Sheet1!A1", "Sheet1!A1"),         ("'Sheet1'!A1:A1", "Sheet1!A1"),
    ("A1:AA5", "Sheet1!A1:Z5"),         ("A1:B1001", "Sheet1!A1:B1000"),
    ("Z1:AA5", "Sheet1!Z1:Z5"),         ("A100:B101", "Sheet1!A100:B101"),
    ("A100", "Sheet1!A100"),            ("Sheet1!A1:D5", "Sheet1!A1:D5"),
]


def test_sheets_values_accept_a_bare_quoted_sheet_name(base, admin_h, sheet_id):
    """`'Sheet1'` with no `!cellpart` means "every cell in that sheet" — measured on a real
    spreadsheet, quoted and unquoted alike, on both `values.get` and `values:batchGet`.

    The mock only un-quoted a title when a `!` followed, so the one form that means "the whole
    sheet without naming bounds" 400d. A client cannot drop the quotes to work around it: quoting is
    what disambiguates a sheet name from a cell reference, measured below."""
    for rng in ("Sheet1", "'Sheet1'"):
        r = _values(base, admin_h, sheet_id, rng)
        assert r.status_code == 200, f"{rng}: {r.text}"
        assert r.json()["range"] == "Sheet1!A1:Z1000", rng
        b = _batch(base, admin_h, sheet_id, [rng])
        assert b.status_code == 200, f"batch {rng}: {b.text}"
        assert b.json()["valueRanges"][0]["range"] == "Sheet1!A1:Z1000", rng


def test_sheets_values_quoting_distinguishes_a_sheet_from_a_cell(base, admin_h, sheet_id):
    """Measured: bare `A1` is the CELL A1 of the first sheet, while `'A1'` is a request for a SHEET
    named A1 and 400s when there is none. So the quotes carry meaning and cannot be stripped —
    without them a client asking for a tab would silently read another tab's cells."""
    assert _values(base, admin_h, sheet_id, "A1").json()["range"] == "Sheet1!A1"
    r = _values(base, admin_h, sheet_id, "'A1'")
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "Unable to parse range: 'A1'"
    # a quoted name that is not this spreadsheet's sheet is refused the same way
    assert _values(base, admin_h, sheet_id, "'Other'").status_code == 400
    assert _batch(base, admin_h, sheet_id, ["'Other'"]).status_code == 400


@pytest.mark.parametrize("rng, echo", MEASURED_ECHO)
def test_sheets_values_range_echo_matches_real_sheets(base, admin_h, sheet_id, rng, echo):
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 200, r.text
    assert r.json()["range"] == echo


@pytest.mark.parametrize("rng, message", [
    ("Other!A1:B2", "Unable to parse range: Other!A1:B2"),
    ("not a range", "Unable to parse range: not a range"),
    ("A1:", "Unable to parse range: A1:"),   # the WHOLE spec, not the offending half
])
def test_sheets_values_parse_error_matches_real_sheets(base, admin_h, sheet_id, rng, message):
    r = _values(base, admin_h, sheet_id, rng)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == message


def test_sheets_batch_get_returns_one_value_range_per_request_range(base, admin_h, sheet_id):
    j = _batch(base, admin_h, sheet_id, ["Sheet1!A1:A1", "Sheet1!A3:A3"]).json()
    assert j["spreadsheetId"] == sheet_id
    # a 1x1 range echoes as a bare cell even when the request spelled out `A1:A1` — measured
    assert [vr["range"] for vr in j["valueRanges"]] == ["Sheet1!A1", "Sheet1!A3"]
    assert [vr["values"] for vr in j["valueRanges"]] == [[GRID[0]], [GRID[2]]]


def test_sheets_batch_get_matches_the_single_get_for_each_range(base, admin_h, sheet_id):
    """batchGet is N single gets through one resolver; if the two disagree, batching changes
    meaning rather than saving round trips."""
    ranges = ["Sheet1", "A1:A2", "Sheet1!A2", "A:A", "Sheet1!D1:E2"]
    batched = _batch(base, admin_h, sheet_id, ranges).json()["valueRanges"]
    singles = [_values(base, admin_h, sheet_id, r).json() for r in ranges]
    assert batched == singles


def test_sheets_batch_get_honors_major_dimension(base, admin_h, sheet_id):
    j = _batch(base, admin_h, sheet_id, ["Sheet1!A1:A3"], majorDimension="COLUMNS").json()
    assert j["valueRanges"][0]["values"] == [["month,revenue", "Jan,120000", "Feb,135000"]]


def test_sheets_batch_get_fails_the_whole_call_on_one_bad_range(base, admin_h, sheet_id):
    """A partial batch would leave the caller unable to tell which range it is missing, so real
    Sheets rejects the request outright."""
    assert _batch(base, admin_h, sheet_id, ["Sheet1!A1:A1", "Other!A1"]).status_code == 400


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
