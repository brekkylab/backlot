"""The walks of the Google types comparison: for each discovery document a source measures the
response types of, which reads to make of a running Backlot, over which corpus.

Which method answers which message is the discovery document's to say (a method's `response`), so
unlike Graph's walk these name methods, not types. What each has to know is how to make its reads
carry everything they can. A field none of them carries is reported missing, which is what the
baseline records.

`sheets` reads a grid holding each kind of cell a corpus states, a range that starts past the
first row and column, a second sheet for a grid range to name, and a prose spreadsheet beside it,
then writes to the grid, copies a sheet and creates a workbook, as its owner.

`docs` reads a document stating a title, a heading and every run mark a corpus states, and a prose
document beside it.
"""

from __future__ import annotations

from typing import Any

import httpx

from backlot.fidelity.errors import FidelityError

SHEETS_CORPUS = [
    {
        "source_type": "google_drive",
        "doc_id": "walk-grid",
        "subtype": "spreadsheet",
        "title": "Walk grid",
        "folder": "walk",
        "author_email": "walker@acme.com",
        "created": "2026-01-10T08:00:00Z",
        "updated": "2026-01-11T08:00:00Z",
        "sheets": [
            {"title": "Data", "grid": [["Region", "Deals", "Won"], ["EMEA", 12, True]]},
            {"title": "More", "grid": [["a", None, "c"], [None, 2.5, False]]},
        ],
    },
    {
        "source_type": "google_drive",
        "doc_id": "walk-prose",
        "subtype": "spreadsheet",
        "title": "Walk prose",
        "folder": "walk",
        "author_email": "walker@acme.com",
        "created": "2026-01-10T08:00:00Z",
        "updated": "2026-01-10T08:00:00Z",
        "content": "month,revenue\nJan,1",
    },
]


DOCS_CORPUS = [
    {
        "source_type": "google_drive",
        "doc_id": "walk-doc",
        "title": "Walk doc",
        "folder": "walk",
        "author_email": "walker@acme.com",
        "created": "2026-01-10T08:00:00Z",
        "updated": "2026-01-11T08:00:00Z",
        "paragraphs": [
            {"style": "TITLE", "text": "Walk doc"},
            {"style": "HEADING_1", "text": "Section"},
            {
                "runs": [
                    {"text": "plain "},
                    {"text": "bold", "bold": True, "italic": True},
                    {"text": " "},
                    {"text": "struck", "underline": True, "strikethrough": True},
                    {"text": " "},
                    {"text": "link", "link": "https://example.com/"},
                ]
            },
        ],
    },
    {
        "source_type": "google_drive",
        "doc_id": "walk-doc-prose",
        "title": "Walk prose doc",
        "folder": "walk",
        "author_email": "walker@acme.com",
        "created": "2026-01-10T08:00:00Z",
        "updated": "2026-01-10T08:00:00Z",
        "content": "first line\nsecond line",
    },
]


def docs(timeout: float) -> list[tuple[str, Any]]:
    """Each document read through `documents.get`, as the service account."""
    import backlot

    with backlot.serve(DOCS_CORPUS, telemetry=False) as server:
        headers = {"Authorization": f"Bearer {server.token}"}
        with httpx.Client(base_url=server.base_url, headers=headers, timeout=timeout) as client:
            files = _ok(
                client.get(
                    "/drive/v3/files",
                    params={"q": "mimeType='application/vnd.google-apps.document'"},
                )
            )["files"]
            return [
                ("docs.documents.get", _ok(client.get(f"/docs/v1/documents/{f['id']}")))
                for f in files
            ]


def sheets(timeout: float) -> list[tuple[str, Any]]:
    """Each Sheets read as the discovery method it is, with what Backlot answered, as the service
    account so every spreadsheet answers; then each write, as the spreadsheet's owner, who alone may
    write it. The writes come last so every read sees the corpus as stated."""
    import backlot
    from backlot.server import meta_users

    with backlot.serve(SHEETS_CORPUS, telemetry=False) as server:
        headers = {"Authorization": f"Bearer {server.token}"}
        with httpx.Client(base_url=server.base_url, headers=headers, timeout=timeout) as client:
            walked = list(_sheets_reads(client))
        owner = next(
            u["token"]
            for u in meta_users(server.base_url, timeout)["users"]
            if u["email"] == SHEETS_CORPUS[0]["author_email"]
        )
        headers = {"Authorization": f"Bearer {owner}"}
        with httpx.Client(base_url=server.base_url, headers=headers, timeout=timeout) as client:
            walked += _sheets_writes(client)
        return walked


def _sheets_writes(client: httpx.Client) -> list[tuple[str, Any]]:
    """One of each write, on the gridded workbook, each asked for every key it can answer with."""
    files = _ok(client.get("/drive/v3/files", params={"q": "name='Walk grid'"}))["files"]
    base = f"/sheets/v4/spreadsheets/{files[0]['id']}"
    return [
        (
            "sheets.spreadsheets.values.update",
            _ok(
                client.put(
                    f"{base}/values/Data!A3:C3",
                    params={"valueInputOption": "USER_ENTERED", "includeValuesInResponse": "true"},
                    json={"values": [["APAC", "12.5%", False]]},
                )
            ),
        ),
        (
            "sheets.spreadsheets.values.batchUpdate",
            _ok(
                client.post(
                    f"{base}/values:batchUpdate",
                    json={
                        "valueInputOption": "RAW",
                        "includeValuesInResponse": True,
                        "data": [{"range": "More!A3", "values": [["x"]]}],
                    },
                )
            ),
        ),
        (
            "sheets.spreadsheets.values.append",
            _ok(
                client.post(
                    f"{base}/values/Data!A1:append",
                    params={"valueInputOption": "RAW", "includeValuesInResponse": "true"},
                    json={"values": [["LATAM", 3, True]]},
                )
            ),
        ),
        (
            "sheets.spreadsheets.values.batchUpdateByDataFilter",
            _ok(
                client.post(
                    f"{base}/values:batchUpdateByDataFilter",
                    json={
                        "valueInputOption": "RAW",
                        "includeValuesInResponse": True,
                        "data": [{"dataFilter": {"a1Range": "More!A3"}, "values": [["y"]]}],
                    },
                )
            ),
        ),
        (
            "sheets.spreadsheets.values.batchClearByDataFilter",
            _ok(
                client.post(
                    f"{base}/values:batchClearByDataFilter",
                    json={"dataFilters": [{"a1Range": "More!A3"}]},
                )
            ),
        ),
        ("sheets.spreadsheets.values.clear", _ok(client.post(f"{base}/values/More!A3:clear"))),
        (
            "sheets.spreadsheets.values.batchClear",
            _ok(client.post(f"{base}/values:batchClear", json={"ranges": ["More!A3"]})),
        ),
        (
            "sheets.spreadsheets.sheets.copyTo",
            _ok(
                client.post(
                    f"{base}/sheets/0:copyTo", json={"destinationSpreadsheetId": files[0]["id"]}
                )
            ),
        ),
        (
            "sheets.spreadsheets.create",
            _ok(
                client.post(
                    "/sheets/v4/spreadsheets",
                    json={
                        "properties": {"title": "Walk created"},
                        "sheets": [
                            {
                                "properties": {
                                    "title": "Made",
                                    "hidden": False,
                                    "tabColor": {"red": 1},
                                    "rightToLeft": True,
                                    "gridProperties": {"frozenRowCount": 1, "hideGridlines": True},
                                }
                            },
                            {"properties": {"title": "Hidden", "hidden": True}},
                        ],
                    },
                )
            ),
        ),
    ]


def _sheets_reads(client: httpx.Client):
    files = _ok(
        client.get(
            "/drive/v3/files",
            params={"q": "mimeType='application/vnd.google-apps.spreadsheet'", "fields": "*"},
        )
    )["files"]
    for book in (f["id"] for f in files):
        base = f"/sheets/v4/spreadsheets/{book}"
        get = "sheets.spreadsheets.get"
        yield get, _ok(client.get(base, params={"includeGridData": "true"}))
        yield get, _ok(client.get(base, params={"includeGridData": "true", "ranges": "B2:C3"}))
        yield (
            "sheets.spreadsheets.getByDataFilter",
            _ok(
                client.post(
                    f"{base}:getByDataFilter",
                    json={"includeGridData": True, "dataFilters": [{"a1Range": "A1:C2"}]},
                )
            ),
        )
        yield "sheets.spreadsheets.values.get", _ok(client.get(f"{base}/values/A1:C2"))
        yield (
            "sheets.spreadsheets.values.batchGet",
            _ok(client.get(f"{base}/values:batchGet", params={"ranges": ["A1:B2", "B1"]})),
        )
    grid = next(f["id"] for f in files if f["name"] == "Walk grid")
    more = _ok(client.get(f"/sheets/v4/spreadsheets/{grid}"))["sheets"][1]["properties"]["sheetId"]
    yield (
        "sheets.spreadsheets.values.batchGetByDataFilter",
        _ok(
            client.post(
                f"/sheets/v4/spreadsheets/{grid}/values:batchGetByDataFilter",
                json={
                    "dataFilters": [
                        {"a1Range": "Data!A1:B2"},
                        {
                            "gridRange": {
                                "sheetId": more,
                                "startRowIndex": 0,
                                "endRowIndex": 2,
                                "startColumnIndex": 1,
                                "endColumnIndex": 3,
                            }
                        },
                    ]
                },
            )
        ),
    )


def _ok(response: httpx.Response) -> Any:
    if response.status_code != 200:
        raise FidelityError(
            f"Backlot answered {response.status_code} for {response.request.url}: {response.text}"
        )
    return response.json()
