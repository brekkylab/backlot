"""The walks of the Google types comparison: for each discovery document a source measures the
response types of, which reads to make of a running Backlot, over which corpus.

Which method answers which message is the discovery document's to say (a method's `response`), so
unlike Graph's walk these name methods, not types. What each has to know is how to make its reads
carry everything they can. A field none of them carries is reported missing, which is what the
baseline records.

`sheets` reads a grid holding each kind of cell a corpus states, a range that starts past the
first row and column, a second sheet for a grid range to name, and a prose spreadsheet beside it,
then writes to the grid, copies a sheet and creates a workbook, as its owner.

`gdocs` reads a document stating a title, a heading, every run mark a corpus states and a list,
one stating tabs, a child tab, a header, a footer, a footnote and a page break, and a prose
document beside them, flat and as their tabs, as their owner; then creates a document and writes
the first one with each request `documents.batchUpdate` applies, the body it sent recorded beside
the answer, and reads it again once it holds a named range.
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


GDOCS_CORPUS = [
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
            {"text": "item", "list": {"preset": "NUMBERED_DECIMAL_ALPHA_ROMAN"}},
            {"text": "nested", "list": {"preset": "NUMBERED_DECIMAL_ALPHA_ROMAN", "level": 1}},
        ],
    },
    {
        "source_type": "google_drive",
        "doc_id": "walk-doc-tabs",
        "title": "Walk tabs",
        "folder": "walk",
        "author_email": "walker@acme.com",
        "created": "2026-01-10T08:00:00Z",
        "updated": "2026-01-11T08:00:00Z",
        "tabs": [
            {
                "paragraphs": [
                    {"runs": [{"text": "cited"}, {"footnote": [{"text": "source"}]}]},
                    {"runs": [{"pageBreak": True}]},
                ],
                "header": [{"text": "head"}],
                "footer": [{"text": "foot"}],
                "tabs": [{"title": "Child", "icon": "\U0001f4cc", "paragraphs": [{"text": "c"}]}],
            }
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


def gdocs(timeout: float) -> list[tuple[Any, ...]]:
    """Each document read through `documents.get`, flat and as its tabs, as its owner: a reader is
    served no `revisionId`; then one `documents.create`, and two `documents.batchUpdate` calls that
    between them send every field it reads."""
    import backlot
    from backlot.server import meta_users

    with backlot.serve(GDOCS_CORPUS, telemetry=False) as server:
        owner = next(
            u["token"]
            for u in meta_users(server.base_url, timeout)["users"]
            if u["email"] == GDOCS_CORPUS[0]["author_email"]
        )
        headers = {"Authorization": f"Bearer {owner}"}
        with httpx.Client(base_url=server.base_url, headers=headers, timeout=timeout) as client:
            files = _ok(
                client.get(
                    "/drive/v3/files",
                    params={"q": "mimeType='application/vnd.google-apps.document'"},
                )
            )["files"]
            walked = [
                ("docs.documents.get", _ok(client.get(f"/docs/v1/documents/{f['id']}", params=q)))
                for f in files
                for q in ({}, {"includeTabsContent": "true"})
            ]
            created = _ok(client.post("/docs/v1/documents", json={"title": "Walk created doc"}))
            doc = next(f["id"] for f in files if f["name"] == "Walk doc")
            revision = _ok(client.get(f"/docs/v1/documents/{doc}"))["revisionId"]
            writes = [
                {
                    "requests": [
                        {
                            "insertText": {
                                "location": {"index": 1, "tabId": "t.0", "segmentId": ""},
                                "text": "Walked ",
                            }
                        },
                        {
                            "deleteContentRange": {
                                "range": {
                                    "startIndex": 1,
                                    "endIndex": 2,
                                    "tabId": "t.0",
                                    "segmentId": "",
                                }
                            }
                        },
                        {
                            "replaceAllText": {
                                "containsText": {
                                    "text": "section",
                                    "matchCase": False,
                                    "searchByRegex": False,
                                },
                                "replaceText": "Part",
                                "tabsCriteria": {"tabIds": ["t.0"]},
                            }
                        },
                    ],
                    "writeControl": {"requiredRevisionId": revision},
                },
                {
                    "requests": [
                        {
                            "insertText": {
                                "endOfSegmentLocation": {"tabId": "t.0", "segmentId": ""},
                                "text": "!",
                            }
                        }
                    ],
                    "writeControl": {"targetRevisionId": revision, "writeMode": "EDIT"},
                },
            ]
            written = [
                (
                    "docs.documents.batchUpdate",
                    _ok(client.post(f"/docs/v1/documents/{doc}:batchUpdate", json=body)),
                    body,
                )
                for body in writes
            ]
            written += _gdocs_structure(client, doc)
            written += _gdocs_styles(client, doc)
            return [*walked, ("docs.documents.create", created), *written]


def _gdocs_structure(client: httpx.Client, doc: str) -> list[tuple[Any, ...]]:
    """The requests that make a header, a footer, a footnote, a page break and a tab, then move the
    tab and remove it, the header and the footer, each field they read sent: the second body names
    what the first made."""
    here = {"tabId": "t.0", "segmentId": ""}
    made = {
        "requests": [
            {"createHeader": {"type": "DEFAULT", "sectionBreakLocation": {"index": 0, **here}}},
            {"createFooter": {"type": "DEFAULT", "sectionBreakLocation": {"index": 0, **here}}},
            {"createFootnote": {"location": {"index": 1, **here}}},
            {"createFootnote": {"endOfSegmentLocation": here}},
            {"insertPageBreak": {"location": {"index": 1, **here}}},
            {"insertPageBreak": {"endOfSegmentLocation": here}},
            {
                "addDocumentTab": {
                    "tabProperties": {
                        "title": "Walked tab",
                        "parentTabId": "t.0",
                        "index": 0,
                        "iconEmoji": "\U0001f4cc",
                    }
                }
            },
        ]
    }
    replies = _ok(client.post(f"/docs/v1/documents/{doc}:batchUpdate", json=made))["replies"]
    header, footer = replies[0]["createHeader"]["headerId"], replies[1]["createFooter"]["footerId"]
    tab = replies[-1]["addDocumentTab"]["tabProperties"]["tabId"]
    removed = {
        "requests": [
            {
                "updateDocumentTabProperties": {
                    "tabProperties": {
                        "tabId": tab,
                        "title": "Moved tab",
                        "parentTabId": "",
                        "index": 1,
                        "nestingLevel": 0,
                        "iconEmoji": "",
                    },
                    "fields": "*",
                }
            },
            {"deleteHeader": {"headerId": header, "tabId": "t.0"}},
            {"deleteFooter": {"footerId": footer, "tabId": "t.0"}},
            {"deleteTab": {"tabId": tab}},
        ]
    }
    return [
        ("docs.documents.batchUpdate", {"replies": replies}, made),
        (
            "docs.documents.batchUpdate",
            _ok(client.post(f"/docs/v1/documents/{doc}:batchUpdate", json=removed)),
            removed,
        ),
    ]


def _gdocs_styles(client: httpx.Client, doc: str) -> list[tuple[Any, ...]]:
    """The style, bullet and named range requests, each field they read sent, links to a URL, a
    heading and a tab among them; the Doc read as it then is, flat and as its tabs; then its bullet
    taken off, the named range replaced by its name and by its id, a name deleted in no tab, and
    the range deleted by its id."""
    here = {"tabId": "t.0", "segmentId": ""}
    heading = next(
        el["paragraph"]["paragraphStyle"]["headingId"]
        for el in _ok(client.get(f"/docs/v1/documents/{doc}"))["body"]["content"]
        if "headingId" in el.get("paragraph", {}).get("paragraphStyle", {})
    )
    color = {"color": {"rgbColor": {"red": 0.5, "green": 0.25, "blue": 1}}}
    size = {"magnitude": 12, "unit": "PT"}
    border = {"color": color, "width": size, "padding": size, "dashStyle": "DOT"}
    made = {
        "requests": [
            {
                "updateTextStyle": {
                    "range": {"startIndex": 2, "endIndex": 4, **here},
                    "textStyle": {
                        "bold": True,
                        "italic": True,
                        "underline": True,
                        "strikethrough": True,
                        "smallCaps": True,
                        "backgroundColor": color,
                        "foregroundColor": color,
                        "fontSize": size,
                        "weightedFontFamily": {"fontFamily": "Lato", "weight": 300},
                        "baselineOffset": "SUPERSCRIPT",
                        "link": {"url": "https://example.com/walk"},
                    },
                    "fields": "*",
                }
            },
            *(
                {
                    "updateTextStyle": {
                        "range": {"startIndex": at, "endIndex": at + 1, **here},
                        "textStyle": {"link": link},
                        "fields": "link",
                    }
                }
                for at, link in (
                    (5, {"headingId": heading}),
                    (6, {"heading": {"id": heading, "tabId": "t.0"}}),
                    (7, {"tabId": "t.0"}),
                )
            ),
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": 2, "endIndex": 3, **here},
                    "paragraphStyle": {
                        "namedStyleType": "HEADING_2",
                        "alignment": "CENTER",
                        "lineSpacing": 150,
                        "direction": "LEFT_TO_RIGHT",
                        "spacingMode": "NEVER_COLLAPSE",
                        "spaceAbove": size,
                        "spaceBelow": size,
                        "borderBetween": border,
                        "borderTop": border,
                        "borderBottom": border,
                        "borderLeft": border,
                        "borderRight": border,
                        "indentFirstLine": size,
                        "indentStart": size,
                        "indentEnd": size,
                        "keepLinesTogether": True,
                        "keepWithNext": True,
                        "avoidWidowAndOrphan": True,
                        "shading": {"backgroundColor": color},
                        "pageBreakBefore": False,
                    },
                    "fields": "*",
                }
            },
            {
                "createParagraphBullets": {
                    "range": {"startIndex": 1, "endIndex": 2, **here},
                    "bulletPreset": "BULLET_CHECKBOX",
                }
            },
            {
                "createNamedRange": {
                    "name": "walked",
                    "range": {"startIndex": 3, "endIndex": 5, **here},
                }
            },
        ]
    }
    replies = _ok(client.post(f"/docs/v1/documents/{doc}:batchUpdate", json=made))["replies"]
    named = replies[-1]["createNamedRange"]["namedRangeId"]
    read = [
        ("docs.documents.get", _ok(client.get(f"/docs/v1/documents/{doc}", params=q)))
        for q in ({}, {"includeTabsContent": "true"})
    ]
    tabs = {"tabIds": ["t.0"]}
    removed = {
        "requests": [
            {"deleteParagraphBullets": {"range": {"startIndex": 1, "endIndex": 2, **here}}},
            {
                "replaceNamedRangeContent": {
                    "namedRangeName": "walked",
                    "text": "named",
                    "tabsCriteria": tabs,
                }
            },
            {"replaceNamedRangeContent": {"namedRangeId": named, "text": "again"}},
            {"deleteNamedRange": {"name": "nobody", "tabsCriteria": {"tabIds": []}}},
            {"deleteNamedRange": {"namedRangeId": named, "tabsCriteria": tabs}},
        ]
    }
    return [
        ("docs.documents.batchUpdate", {"replies": replies}, made),
        *read,
        (
            "docs.documents.batchUpdate",
            _ok(client.post(f"/docs/v1/documents/{doc}:batchUpdate", json=removed)),
            removed,
        ),
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
