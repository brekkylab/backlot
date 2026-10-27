"""Google Sheets v4 (``/sheets/v4``): the spreadsheet and `values` reads, and the writes.

The routes are registered on `routers.google.router`, so they share its system parameters
(`routers.google._system_parameters`), credentials and batch endpoint; `routers.google` imports
this module last to register them.
"""

from __future__ import annotations

import datetime
import functools
import json
import re
import struct
import time
from typing import NamedTuple

from fastapi import Request
from fastapi.responses import Response

from backlot import (
    auth,
    gsheets,
    protojson,
    store,
    synth,
)
from backlot.acl import Caller
from backlot.errors import google as gerr
from backlot.openapi import qp
from backlot.routers.google import (
    _BASELINES,
    _BATCH_OUTER,
    _DRIVE_ROOT_FOLDER,
    _drive_owned_by,
    _editor_doc,
    _require,
    _service_email,
    _sheets_bool_value,
    _sheets_enum_value,
    _typed_query,
    router,
)


def _sheets_grid(content: str | None) -> list[list[str]]:
    """The stored text as a grid: one row per line, each row a SINGLE cell holding that line
    verbatim. Joined back with ``\\n`` this reproduces the stored content byte-for-byte, which is
    also what ``files.export`` serves — so the two cannot disagree.

    NOT split on a delimiter. Measured over 1,875 real spreadsheet records, none is
    delimiter-uniform CSV: 82.6% are prose, 17.4% prose wrapped around a PIPE-delimited table. So
    comma-splitting manufactures columns out of sentence punctuation. A line break is the only
    structure the stored text carries, so it is the only structure served — and choosing a column
    delimiter is a corpus-owner's decision, which a caller can still make without first undoing a
    guess made here.
    """
    return [[line] for line in (content or "").split("\n")]


# --- the standard query parameters every Sheets read accepts ---------------------------------
#
# Measured on the live API, all on `values.get` unless noted:
#
#   fields           a partial-response mask; see `_gmask`
#   prettyPrint      DEFAULT TRUE -- indented unless `false` or `0`; see `_sheets_respond`
#   alt              `json` only; `media` is 400 "Unsupported alt type ... for non byte stream
#                    request." and `zzz` 400 "Invalid value ... for query parameter 'alt'". `proto`
#                    is a third answer real gives and this module does not -- see `_sheets_respond`
#   callback         JSONP, on a GET: the body is wrapped, the type becomes text/javascript and the
#                    status becomes 200 -- a name that is not a JavaScript one is refused ahead of
#                    everything but `$.xgafv` and `alt` (`gerr.validate_system_parameters`), and an
#                    empty value is no callback at all. Declared here rather than router-wide with
#                    `$.xgafv`, because Sheets is where a SUCCESS is wrapped: the other four
#                    families honour it on their errors only, which is less than a router-wide
#                    declaration would promise. The two POST routes below share this list and so
#                    declare it as well, which real's document does for every method -- and real
#                    ignores it on a POST exactly as `gerr.jsonp_callback` does, so the declaration
#                    promises a caller no more there than the vendor's own does.
#   quotaUser        a rate-limit bucket label; any string, including empty, and no effect on the
#                    response -- Backlot enforces no quota, so there is nothing for it to select
#   upload_protocol  accepted and ignored on a read
#
# `key` is NOT here: it is an alternative way to authenticate, and honouring it means a second
# credential path through `backlot.auth` rather than a parameter this module can read. Nor are
# `access_token` and `oauth_token`, which `routers.google._credential` reads and
# `backlot.openapi.google_system_parameters` declares for every family. `uploadType` is not here
# either -- measured, the real API REFUSES it on a read ("Cannot bind query parameter"), so the
# baseline's "the vendor accepts it" is not what the vendor does.
_P_SHEETS_STD = [
    qp("fields"),
    qp("prettyPrint", "boolean"),
    qp("alt"),
    qp("callback"),
    qp("quotaUser"),
    qp("upload_protocol"),
]


def _sheets_types() -> dict[str, dict[str, str | None]]:
    """The messages a Sheets read answers with and every message they reach, each as its fields and
    the message each holds (`None` for a scalar, an enum, a map or a list of lists): the `types`
    `backlot diff` records in the `google_drive` baseline from the discovery document. A field only
    Backlot serves is not one a mask may name."""
    types = json.loads((_BASELINES / "google_drive.json").read_text(encoding="utf-8"))["types"]

    def held(type_: str) -> str | None:
        name = type_.strip("[]")
        return name if name in types else None

    return {
        name: {f: held(m["type"]) for f, m in entry.get("fields", {}).items() if "type" in m}
        for name, entry in types.items()
    }


_SHEETS_FIELDS = _sheets_types()


def _gmask_paths(mask: str) -> tuple[list[tuple[str, ...]], str]:
    """The paths a ``fields`` mask names, in its order, each as its names, and the bracket it leaves
    unpaired (``""`` for none).

    Measured grammar: ``.`` and ``/`` both descend, ``a(b,c)`` groups a sub-selection, ``,``
    separates siblings and a trailing comma is tolerated. A name is matched case-sensitively and is
    not trimmed -- `` spreadsheetId`` with a leading space 400s. An empty group names nothing, not
    even the name before it: measured 2026-10-22, `a()` answered the whole spreadsheet with no
    refusal for `a`, and `spreadsheetId,sheets()` the `spreadsheetId` alone.

    A bracket left unpaired is refused after the paths before it, a ``)`` where it stands and a
    ``(`` at the end (`gerr.unbalanced_field_mask`): measured 2026-10-22, `a)` and
    `bogus)spreadsheetId` named `a` and `bogus`, `spreadsheetId)bogus` was the `)` refusal, and
    `sheets(properties/title,bogus` named `sheets.bogus` where `sheets(properties/title` was the
    `(` one."""
    paths: list[tuple[str, ...]] = []
    # the prefix of each open group
    stack: list[tuple[str, ...]] = []
    prefix: tuple[str, ...] = ()
    token = ""

    def names(text: str) -> tuple[str, ...]:
        return tuple(text.replace("/", ".").split("."))

    for ch in mask:
        if ch == "(":
            stack.append(prefix)
            prefix += names(token) if token else ()
            token = ""
        elif ch == ")":
            if token:
                paths.append(prefix + names(token))
            if not stack:
                return paths, ")"
            prefix, token = stack.pop(), ""
        elif ch == ",":
            if token:
                paths.append(prefix + names(token))
            token = ""
        else:
            token += ch
    if token:
        paths.append(prefix + names(token))
    return paths, "(" if stack else ""


def _gmask_resolve(path: tuple[str, ...], message: str | None) -> dict | None:
    """What ``path`` selects in ``message`` (`None` for a field that holds no message), or ``None``
    when it names nothing there; ``{}`` is all of it.

    A ``*`` stands for one name. Measured 2026-10-22: `sheets.*.title` is
    `sheets.properties.title`, `*.*.title` the same, `*.*.rowCount` refused as naming nothing (the
    row count is three names under `sheets`), and `*.data` is `sheets.data`. A ``*`` that ends a
    path selects all of what it is under, a field holding no message included, and so does a run
    of them: `spreadsheetId.*`, `spreadsheetId.*.*`, a map's `criteria.*` and `*.*.*` answered,
    where `spreadsheetId.*.x` names nothing."""
    if not path:
        return {}
    head, rest = path[0], path[1:]
    if message is None:
        return {} if all(name == "*" for name in path) else None
    fields = _SHEETS_FIELDS[message]
    if head == "*":
        if not rest:
            return {}
        out = {}
        for name, held in fields.items():
            sub = _gmask_resolve(rest, held)
            if sub is not None:
                out[name] = sub
        return out or None
    if head not in fields:
        return None
    sub = _gmask_resolve(rest, fields[head])
    return None if sub is None else {head: sub}


# The messages a mask selects whole once any path names them whole, a narrower path beside it
# notwithstanding. Measured 2026-10-27: `values(userEnteredValue,userEnteredValue.formulaValue)`
# answered a string cell's `stringValue`, and so did the same for `effectiveValue`, an
# `errorValue`'s `message` beside `errorValue.type`, a `backgroundColorStyle`'s `rgbColor` beside
# `backgroundColorStyle.themeColor`, and `themeColors` whole beside `themeColors.colorType`.
_GMASK_WHOLE = frozenset(
    {"sheets.ExtendedValue", "sheets.ErrorValue", "sheets.ColorStyle", "sheets.ThemeColorPair"}
)


def _gmask_merge(into: dict, selection: dict, message: str | None) -> None:
    """Merge one path's selection into the tree, a narrower selection replacing a wider one:
    measured 2026-10-22, `spreadsheetId,*` answered `spreadsheetId` alone, and
    `sheets,sheets.properties.title` and `sheets.*,sheets.properties.title` the title alone, and
    on 2026-10-27 `effectiveFormat,effectiveFormat.numberFormat` and
    `properties(defaultFormat,defaultFormat.numberFormat)` the narrower path alone, in either
    order. A message in :data:`_GMASK_WHOLE` is the exception, kept whole."""
    fields = _SHEETS_FIELDS.get(message, {}) if message else {}
    for name, sub in selection.items():
        held = fields.get(name)
        if held in _GMASK_WHOLE and (sub == {} or into.get(name) == {}):
            into[name] = {}
        else:
            _gmask_merge(into.setdefault(name, {}), sub, held)


def _gmask_select(mask: str | None, message: str) -> tuple[dict | None, bool]:
    """The selection tree a ``fields`` mask makes of a ``message`` response, ``{}`` at a leaf
    meaning all of it, and whether the mask names a comment field. ``None`` for no mask, which an
    empty one, an empty group and a ``*`` alone each are: measured 2026-10-22, `fields=*` answered
    what no mask does, `includeGridData` deciding the grid.

    A path that names nothing is refused, the first such in the mask's order (measured on
    `sheets.properties.title,bogus2,sheets.bogus3`: `bogus2`), so a declared field the response
    holds no value for answers 200 without it rather than the 400 a field it does not declare gets.
    Checked against the whole schema, not against what this module builds: measured 2026-10-22,
    `namedRanges`, `sheets(merges,conditionalFormats)` and a cell's `userEnteredFormat` each
    answered 200 on a spreadsheet holding none. A bracket left unpaired is refused after the paths
    before it (`_gmask_paths`).

    A comment field is one a path reaches by naming something: measured 2026-10-22, `comments`,
    `comments.*`, `*.commentId` and `*.headPost` were refused for asking after comments that
    `commentsViewMode` did not request, and `*.*`, which reaches `comments` through ``*`` alone, was
    answered."""
    if not mask:
        return None, False
    tree: dict = {}
    comments = False
    paths, unpaired = _gmask_paths(mask)
    for path in paths:
        selection = _gmask_resolve(path, message)
        if selection is None:
            raise gerr.bad_field_mask(mask, ".".join(path))
        comments |= "comments" in selection and any(name != "*" for name in path)
        _gmask_merge(tree, selection, message)
    if unpaired:
        raise gerr.unbalanced_field_mask(mask, unpaired)
    return (tree or None), comments


def _gmask_wants_grid(tree: dict | None, include_grid_data: bool) -> bool:
    """Whether the cells are served: as `includeGridData` says with no mask, and otherwise when the
    mask reaches `sheets.data`.

    The discovery document says of `includeGridData`, on both methods that take it: "This parameter
    is ignored if a field mask was set in the request." Measured, that is narrower than it reads --
    the mask has to reach `sheets.data`. `fields=sheets`, `sheets.*`, `*.data` and
    `fields=sheets.data...` build the grid whatever `includeGridData` says, and
    `fields=sheets.properties.title` does not. `fields=*` is no mask at all (`_gmask_select`)."""
    if tree is None:
        return include_grid_data
    under = tree.get("sheets")
    return under is not None and (not under or "data" in under)


def _gmask_apply(tree: dict, value):
    """Project ``value`` through a selection tree, mapping over a list rather than indexing it —
    which is what lets ``sheets.properties.title`` reach into every sheet — and keeping the order
    ``value`` has: measured 2026-10-22, `*.title,spreadsheetId` answered `spreadsheetId` first."""
    if not tree:
        return value
    if isinstance(value, list):
        return [_gmask_apply(tree, v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {name: _gmask_apply(tree[name], v) for name, v in value.items() if name in tree}
    if "rowData" in out:
        _gmask_trim_rows(out)
    return out


_CELL_FORMATS = frozenset({"userEnteredFormat", "effectiveFormat"})


def _gmask_hollow(value) -> bool:
    """Whether a projected message holds nothing but empty messages."""
    return isinstance(value, dict) and all(_gmask_hollow(v) for v in value.values())


def _gmask_trim_rows(block: dict) -> None:
    """Cut a projected `GridData` block's rows the way `_sheets_grid_data` cuts them, a cell the
    mask left empty counting as an empty cell. Measured 2026-10-22 over `D1:F4` holding `a` in D1
    and `b` in F2: `rowData.values.userEnteredValue` answered
    `[{"values": [{…}]}, {"values": [{}, {}, {…}]}]`, and `rowData.values.userEnteredFormat`, which
    no cell held, no `rowData` at all.

    A cell's format the mask leaves holding nothing is left out, where every other message is kept
    empty. Measured 2026-10-27: `values(userEnteredValue,effectiveFormat.numberFormat)` served no
    `effectiveFormat` on a string, a number or a boolean, and neither did
    `effectiveFormat.textFormat.link`, while `effectiveValue.numberValue` served a string's
    `effectiveValue` as `{}`, and `sheets.properties.tabColorStyle` and
    `properties.defaultFormat.numberFormat` served `properties` and `defaultFormat` as `{}`."""
    rows = []
    for row in block["rowData"]:
        values = [
            {k: v for k, v in cell.items() if k not in _CELL_FORMATS or not _gmask_hollow(v)}
            for cell in row.get("values", [])
        ]
        while values and not values[-1]:
            values.pop()
        rows.append({"values": values} if values else {})
    while rows and not rows[-1]:
        rows.pop()
    if rows:
        block["rowData"] = rows
    else:
        del block["rowData"]


def _sheets_alt(request: Request) -> None:
    """Refuse an `alt` that names no format, ahead of everything else a Sheets read checks: measured
    2026-10-22, `alt=zzz` answered its 400 with no credential, with a bad one, beside a bad
    `includeGridData`, a bad body and a `fields` mask naming nothing, and for a spreadsheet that
    does not exist. An `alt` that names one is `_sheets_respond`'s, after the read has run."""
    alt = gerr.alt_format(request.query_params)
    if alt and alt not in ("json", "media", "proto"):
        # Through the spelling that arrived, not the folded one: measured 2026-09-17 on Sheets,
        # `alt=ZZZ` answers `Invalid value "ZZZ"`.
        raise gerr.invalid_argument(
            "Invalid value \"{}\" for query parameter 'alt'".format(
                gerr.first_repeat(request.query_params, gerr.ALT)
            )
        )


def _sheets_mask(request: Request, message: str) -> tuple[dict | None, bool]:
    """`_gmask_select` over this request's ``fields``. A read checks it once its typed values and
    body are read and before the spreadsheet is looked up: measured 2026-10-22 on the five reads, a
    mask naming nothing answered its 400 ahead of a missing spreadsheet's 404, a range that does
    not parse, an empty filter list and `alt=media`, and behind a bad `includeGridData`,
    `commentsViewMode`, `majorDimension` and body."""
    return _gmask_select(gerr.first_repeat(request.query_params, "fields"), message)


def _sheets_respond(request: Request, body: dict, tree: dict | None) -> Response:
    """One Sheets response, with the standard query parameters applied.

    Order matters and is measured: `alt` decides whether the answer can be JSON at all, then the
    `fields` selection (`_sheets_mask`) narrows the body, then `prettyPrint` decides the
    indentation, then `callback` wraps what is left. An unparseable `callback` is refused ahead of
    all of them, in ``gerr.validate_system_parameters`` — measured, that refusal beats a mistyped
    `fields` mask — so by here the name is one the answer can be handed to.

    The bytes themselves are ``gerr.respond``'s, which is also what every Google ERROR is rendered
    through: a success and a failure on the same route come back through the same serializer on
    real, so they do here."""
    # `gerr.first_repeat`, not `.get`: real answers a repeated `alt` through the first one, and
    # `gerr.jsonp_callback` reads the same parameter to decide the wrap -- so one request has to
    # see one `alt` in both places.
    alt = gerr.alt_format(request.query_params)
    if alt and alt != "json":
        # `media` or `proto`, `_sheets_alt` having refused the rest. `media` gets its own sentence,
        # the format lowercased: measured 2026-09-17 on Sheets, `alt=MEDIA` answers `Unsupported
        # alt type "media"`. `proto` gets the one `_sheets_alt` gives: real answers it with a
        # protobuf body ("Proto over HTTP is not allowed for service …") under
        # `application/x-protobuf`, a format this module does not serve.
        raise gerr.invalid_argument(
            f'Unsupported alt type "{alt}" for non byte stream request.'
            if alt == "media"
            else "Invalid value \"{}\" for query parameter 'alt'".format(
                gerr.first_repeat(request.query_params, gerr.ALT)
            )
        )
    if tree is not None:
        body = _gmask_apply(tree, body)
    # Measured 2026-09-23, twenty spellings one request each: `false` and `0` turn it off and the
    # other eighteen leave it indented, `FALSE`, `False`, `f`, `no`, `n`, `00` and a padded ` false`
    # among them -- none is refused, where the other booleans refuse a value they cannot read. It
    # is the success side alone that reads it: an error is indented whatever it says.
    compact = gerr.first_repeat(request.query_params, "prettyPrint") in _PRETTY_PRINT_FALSE
    return gerr.respond(body, compact=compact, callback=gerr.jsonp_callback(request))


_P_SHEETS_GET = [
    qp("includeGridData", "boolean"),
    qp("ranges"),
    qp("excludeTablesInBandedRanges", "boolean"),
    qp("commentsViewMode"),
    *_P_SHEETS_STD,
]


# The typed query values `spreadsheets.get` and `:getByDataFilter` read.
_SHEETS_GET_FLAGS = {
    "includeGridData": lambda raw: _sheets_bool_value(raw, "include_grid_data"),
    "excludeTablesInBandedRanges": lambda raw: _sheets_bool_value(
        raw, "exclude_tables_in_banded_ranges"
    ),
    # Read as `documents.get` reads its own (`_DOCS_COMMENTS_VIEW`), measured 2026-10-22: `03`,
    # `+1` and `comments_view_mode_included` read, `4`, `-1`, `BOGUS` and empty refused, and of
    # several the last decides.
    "commentsViewMode": lambda raw: _sheets_enum_value(
        raw, "comments_view_mode", _PJ_COMMENTS_VIEW
    ),
}


@router.get("/sheets/v4/spreadsheets/{spreadsheet_id}", openapi_extra={"parameters": _P_SHEETS_GET})
async def sheets_get(spreadsheet_id: str, request: Request):
    """The spreadsheet's structure, and its cells only if asked for.

    ``data`` is withheld unless ``includeGridData=true`` — measured: a real workbook answers 4 KB by
    default and 5.7 MB with the flag, and ``ranges`` alone does NOT unlock it. Volunteering the
    whole grid would hand a reader cells the real API never would, so the document it assembles
    would differ between the two backends. With the flag, ``ranges`` scopes the returned rows
    (measured: 5.7 MB -> 11 KB for ``A1:B2``)."""
    # `alt` (`_sheets_alt`), the credential and the typed values (`_typed_query`), the mask
    # (`_sheets_mask`), then the lookup.
    _sheets_alt(request)
    caller = _require(request)
    flags = _typed_query(request, _SHEETS_GET_FLAGS)
    tree, comments = _sheets_mask(request, "sheets.Spreadsheet")
    row, sheets = _workbook(request, spreadsheet_id)
    mode = (flags["commentsViewMode"] or [_PJ_COMMENTS_VIEW.names[0]])[-1]
    owned = _drive_owned_by(row["author_email"], caller.email)
    view = _sheets_comments_view(_PJ_COMMENTS_VIEW.names.index(mode), comments, owned)
    # A mask that reaches the cells decides the grid, and `includeGridData` is then ignored rather
    # than consulted -- the vendor's own wording. Still parsed, so a bad value is still refused.
    grid = _gmask_wants_grid(tree, (flags["includeGridData"] or [False])[-1])
    # `excludeTablesInBandedRanges` is validated above and then unused, deliberately: it drops the
    # tables that sit inside a banded range, and a corpus states neither tables nor banded ranges,
    # so there is nothing here to exclude. Leaving it unvalidated instead would accept the one
    # thing a client can get wrong about it.
    ranges = request.query_params.getlist("ranges")
    return _sheets_respond(
        request,
        _sheets_book(
            spreadsheet_id,
            row,
            sheets,
            ranges,
            grid,
            view,
            tree,
            settings=_sheets_settings(auth.conn(request), spreadsheet_id),
            url=_sheets_url(spreadsheet_id, caller),
        ),
        tree,
    )


def _sheets_book(
    spreadsheet_id: str,
    row,
    sheets: list[_Sheet],
    specs: list,
    grid: bool,
    view: str,
    tree: dict | None,
    *,
    settings: dict,
    url: str,
):
    """The `Spreadsheet` body both `spreadsheets.get` and `:getByDataFilter` answer with.

    ``specs`` is what to serve — A1 ranges from `ranges` for one, and from the data filters for the
    other A1 ranges and the empty grid ranges they may select (`_Empty`); empty means every sheet,
    whole. ``view`` is the `commentsViewMode` it answers, which a read with no mask (``tree``)
    carries only beside the grid: measured 2026-10-22, with no `fields` it was there under
    `includeGridData=true` alone, and a mask naming it got it with no grid."""
    # Each sheet paired with the cell parts to serve for it: `("", title)` is the whole grid, which
    # is what a sheet nobody named gets. Resolved ONCE, here — a sheet is never re-derived from its
    # own title further down, or one titled like a cell reference (`A1`, `AB`) would come back
    # holding the first sheet's cells, or overflow the grid and fail the whole call.
    wanted: list[tuple[_Sheet, list[tuple[str, str]]]] = [(sh, [("", sh.title)]) for sh in sheets]
    if specs:
        # A range filters the SHEETS ARRAY, not merely the cells: measured, a sheet none touches is
        # absent from the response entirely, and a sheet several touch gets one `data` block each.
        # That holds with or without `includeGridData` — without it the sheet list is still
        # filtered and no block is served.
        per_sheet: dict[int, list] = {}
        for spec in specs:
            if isinstance(spec, _Empty):
                per_sheet.setdefault(spec.sheet.index, []).append(spec)
                continue
            sheet, body = _a1_sheet(spec, sheets)
            per_sheet.setdefault(sheet.index, []).append((body, spec))
        wanted = [(sh, per_sheet[sh.index]) for sh in sheets if sh.index in per_sheet]
    out = []
    for sh, parts in wanted:
        # the grid, not the data extent — see SHEETS_GRID_ROWS
        entry = {"properties": sh.served()}
        if grid:
            entry["data"] = [
                _sheets_empty_grid_data(part)
                if isinstance(part, _Empty)
                else _sheets_grid_data(sh, *part)
                for part in parts
            ]
        out.append(entry)
    # Real's order, measured 2026-10-22 with and without the grid and under `fields=*.*`.
    stated = {**gsheets.DEFAULT_PROPERTIES, **settings}
    book = {
        "spreadsheetId": spreadsheet_id,
        "properties": {
            "title": row["title"],
            "locale": stated["locale"],
            "autoRecalc": stated["autoRecalc"],
            "timeZone": stated["timeZone"],
            "defaultFormat": SHEETS_DEFAULT_FORMAT,
            "spreadsheetTheme": SHEETS_THEME,
        },
        "sheets": out,
        "spreadsheetUrl": url,
    }
    if tree is not None or grid:
        book["commentsViewMode"] = view
    return book


# --- Sheets `values` reads ------------------------------------------------------------------
# `spreadsheets.get` serves the whole structured grid; a client that wants a slice reads
# `values.get`, and one that wants several slices reads `values:batchGet`. Both resolve an A1
# range against the same grid `sheets_get` builds, so the three calls cannot disagree about what
# a cell holds.

SHEETS_SHEET_TITLE = "Sheet1"  # Backlot shapes every spreadsheet as one sheet with this title

# A real sheet's GRID is larger than its data — Sheets creates one at 1000x26 — and every range
# behaviour below is defined against the grid rather than against the occupied cells. Measured on a
# real spreadsheet holding 14 rows: `values/<title>` echoes `A1:Z1000`, `A:A` echoes `A1:A1000`.
# So Backlot declares the same grid. This is API scaffolding, like the synthesized `sheetId` and
# sheet title beside it — not invented cell data, which `_sheets_grid` still refuses to manufacture.
SHEETS_GRID_ROWS = gsheets.GRID_ROWS
SHEETS_GRID_COLS = gsheets.GRID_COLS

# A cell's `effectiveFormat`, in the order real emits it. Measured: across every cell type a
# corpus can state it varies in ONE field, `horizontalAlignment` -- a string sits left, a number
# right, a boolean centred -- and the rest is the spreadsheet's default format, which nothing in a
# corpus can change. So the whole object is derived rather than stored, the way `formattedValue`
# is.
#
# `userEnteredFormat` is the other half of the pair. Measured, it carries only what was explicitly
# set on that cell -- a strict subset of `effectiveFormat`, absent entirely from a cell nobody
# formatted. Of 17 typed cells the only ones that had one were the percent, date, datetime, time and
# scientific cells, each carrying a lone `numberFormat` that Sheets INFERRED from what was typed.
# A corpus states no formatting, so a corpus cell has none; a cell a write gave a number format
# (`gsheets.apply`) serves it (`_sheets_cell_data`).
_CELL_FORMAT_ALIGN = {"str": "LEFT", "num": "RIGHT", "bool": "CENTER"}
_CELL_FORMAT_BOX = {
    "backgroundColor": {"red": 1, "green": 1, "blue": 1},
    "padding": {"top": 2, "right": 3, "bottom": 2, "left": 3},
}
_CELL_FORMAT_REST = {
    "verticalAlignment": "BOTTOM",
    "wrapStrategy": "OVERFLOW_CELL",
    "textFormat": {
        "foregroundColor": {},
        "fontFamily": "Arial",
        "fontSize": 10,
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "foregroundColorStyle": {"rgbColor": {}},
    },
    "hyperlinkDisplayType": "PLAIN_TEXT",
    "backgroundColorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
}


def _sheets_format(cell) -> dict:
    """The `effectiveFormat` a cell of this type carries: its `numberFormat` first when it has one,
    and an alignment by what it holds -- an error centred like a boolean, measured 2026-10-26.

    A cell with no value and no error (`gsheets.CLEARED`, or a cleared cell that kept its
    number format) carries neither `horizontalAlignment` nor `hyperlinkDisplayType`, measured the
    same day."""
    nf = gsheets.cell_format(cell)
    head = {"numberFormat": nf} if nf else {}
    value = gsheets.cell_value(cell)
    if gsheets.cell_error(cell) is None and value is None:
        rest = {k: v for k, v in _CELL_FORMAT_REST.items() if k != "hyperlinkDisplayType"}
        return {**head, **_CELL_FORMAT_BOX, **rest}
    if isinstance(value, bool) or gsheets.cell_error(cell) is not None:
        align = _CELL_FORMAT_ALIGN["bool"]
    elif isinstance(value, (int, float)):
        align = _CELL_FORMAT_ALIGN["num"]
    else:
        align = _CELL_FORMAT_ALIGN["str"]
    return {**head, **_CELL_FORMAT_BOX, "horizontalAlignment": align, **_CELL_FORMAT_REST}


# A track's default size in pixels, carried by every `rowMetadata`/`columnMetadata` entry a
# `GridData` block holds. Measured on a real workbook: every row entry is `{"pixelSize": 21}` and
# every column entry `{"pixelSize": 100}`. A corpus states no track size, so nothing varies.
SHEETS_ROW_PIXELS = 21
SHEETS_COL_PIXELS = 100

# The spreadsheet-level format a freshly created workbook carries. Unlike a cell's
# `effectiveFormat`, which the cell's own type decides, these two are SETTINGS: measured across six
# real workbooks they came in three variants, differing where someone had applied a theme or the
# document had been imported from .xlsx (Malgun Gothic, 11pt, MIDDLE alignment). A corpus states
# none of that, so what is served is the variant a new spreadsheet has -- the same footing as
# `locale`, `autoRecalc`, `timeZone` and the 1000x26 grid beside them.
#
# `foregroundColor: {}` and the TEXT theme colour's `rgbColor: {}` are black: proto3 drops a zero,
# so an all-zero colour is the empty object rather than three zeroes.
SHEETS_DEFAULT_FORMAT = {
    "backgroundColor": {"red": 1, "green": 1, "blue": 1},
    "padding": {"top": 2, "right": 3, "bottom": 2, "left": 3},
    "verticalAlignment": "BOTTOM",
    "wrapStrategy": "OVERFLOW_CELL",
    "textFormat": {
        "foregroundColor": {},
        # the CSS stack, where a CELL's textFormat resolves to the single family "Arial"
        "fontFamily": "arial,sans,sans-serif",
        "fontSize": 10,
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "foregroundColorStyle": {"rgbColor": {}},
    },
    "backgroundColorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
}
SHEETS_THEME = {
    "primaryFontFamily": "Arial",
    "themeColors": [
        {"colorType": "TEXT", "color": {"rgbColor": {}}},
        {"colorType": "BACKGROUND", "color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}},
        {
            "colorType": "ACCENT1",
            "color": {"rgbColor": {"red": 0.25882354, "green": 0.52156866, "blue": 0.95686275}},
        },
        {
            "colorType": "ACCENT2",
            "color": {"rgbColor": {"red": 0.91764706, "green": 0.2627451, "blue": 0.20784314}},
        },
        {
            "colorType": "ACCENT3",
            "color": {"rgbColor": {"red": 0.9843137, "green": 0.7372549, "blue": 0.015686275}},
        },
        {
            "colorType": "ACCENT4",
            "color": {"rgbColor": {"red": 0.20392157, "green": 0.65882355, "blue": 0.3254902}},
        },
        {
            "colorType": "ACCENT5",
            "color": {"rgbColor": {"red": 1, "green": 0.42745098, "blue": 0.003921569}},
        },
        {
            "colorType": "ACCENT6",
            "color": {"rgbColor": {"red": 0.27450982, "green": 0.7411765, "blue": 0.7764706}},
        },
        {
            "colorType": "LINK",
            "color": {"rgbColor": {"red": 0.06666667, "green": 0.33333334, "blue": 0.8}},
        },
    ],
}


_SHEETS_ENUM = "type.googleapis.com/google.apps.sheets.v4"


def _pj_enum(name: str, *values: str) -> protojson.Enum:
    return protojson.Enum(f"{_SHEETS_ENUM}.{name}", values)


# The three read enums, their names in the discovery document's order, which is each name's number:
# measured 2026-10-04 over a formula cell and a date cell, `valueRenderOption=2` answers the formula
# and `dateTimeRenderOption=1` the formatted date, and `majorDimension=2` answers by columns.
_PJ_DIMENSION = _pj_enum("Dimension", "DIMENSION_UNSPECIFIED", "ROWS", "COLUMNS")
_PJ_RENDER = _pj_enum("ValueRenderOption", "FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
_PJ_DATETIME = _pj_enum("DateTimeRenderOption", "SERIAL_NUMBER", "FORMATTED_STRING")
# One endpoint of an A1 range: a full cell (`B2`), a bare column (`B`) or a bare row (`2`). A bare
# column or row is an endpoint only INSIDE a range — measured 2026-09-12, `Sheet1!B`, `Sheet1!2` and
# a bang-less `Z` are all "Unable to parse range" while `A:A` and `1:1` answer — and row 0 is not
# one at all (`A0`, `A1:A0` are unparseable too).
_A1_END = re.compile(r"(?:(?P<col>[A-Za-z]{1,3})(?P<row>\d+)?|(?P<rowonly>\d+))\Z")
# One endpoint in R1C1 notation, which the discovery document names beside A1 for `values.get`'s
# `range` ("The A1 notation or R1C1 notation of the range to retrieve values from"), and which
# the LlamaIndex `GoogleSheetsReader` sends for every sheet as `R1C1:R{rowCount}C{columnCount}`.
#
# The grammar below is measured, not read off a document: 217 requests against a real workbook on
# 2026-09-12, every one pinned in `tests/test_google.py::MEASURED_R1C1`.
#
# * `R` and `C` each take an ABSOLUTE 1-based number (`R1C1`), a BRACKETED 0-based offset from A1
#   (`R[1]C[1]` is B2, `R[0]C[0]` is A1 — the concepts guide's "relative to the current cell" has
#   A1 as the current cell on a read), or no number at all (`R1C` is A1, `RC` is A1). Absolute 0
#   and a negative or `+`-signed offset are unparseable; a leading zero is fine (`R01C01` is A1).
# * either letter may be absent. Beside an R1C1 half a numbered `R2` is row 2 with the columns
#   unbounded (`R1C1:R2` is A1:Z2) and `C[1]` alone is column B whole (`B1:B1000`); a bare `R`, `C`
#   or `RC` is the cell A1.
# * both halves of a range are read in ONE notation: `A1:R2C2` and `R1C1:B2` are unparseable. A
#   token is read as A1 first (`R1` is the cell R1, `RC1` the cell RC1, `RC:RC` the columns RC),
#   then as R1C1 (`RC` alone is A1, and so is `R1C1` even in a workbook with a sheet named `R1C1`),
#   then — bang-less — as a sheet name (`A` is the sheet `A` when there is one).
# * a reversed R1C1 range is swapped the way an A1 one is (`R3C3:R1C1` is A1:C3), except that on
#   an axis where both halves carry a number OF THE SAME KIND — both absolute, or both offsets —
#   start == end + 1 on the numbers AS WRITTEN is unparseable: `R2C2:R1C1`, `R1C2:R1C1`,
#   `R[1]C[1]:R[0]C[0]` and `R2:R1C1` 400 while `R3C3:R1C1` and `R[2]C[2]:R[0]C[0]` answer. An
#   axis that mixes the kinds swaps freely: `R[1]C[1]:R1C1`, `R[2]C[2]:R1C1` and `R1C1:R[0]C[0]`
#   all answer. That reads as a half-open interval on the raw numbers coming out empty, checked
#   before offsets are resolved; every reversed range sent is a row of `MEASURED_R1C1`.
# * the echo is the A1 equivalent, and the grid rules are A1's: an end past the grid is clamped
#   (`R1C1:R2000C50` is A1:Z1000, `1:1001` is A1:Z1000), a start past it is refused (`R[1000]C[0]`
#   names `A1001`), and a refused whole column or row is named by its letters or numbers alone,
#   one column or row collapsing as one cell does (`ZZ:ZZ` names `ZZ`, `AA:AB` names `AA:AB`,
#   `C[26]` names `AA`, `R[1000]:R[1000]` names `1001`, `1001:1002` names `1001:1002`).
# * whitespace is refused wherever it was tried — around the whole, around the bang, around the
#   colon, inside a token, inside a quoted title, a tab as much as a space: ` A1`, `Sheet1! A1`,
#   `A1: B2`, `R1 C1`, `R[ 1]C[1]`, `'Sheet1 '!A1` — all 36 forms sent — are unparseable. An EMPTY title
#   before the bang is the first sheet (`!A1`, `''!A1`, `!R[1]C[1]`), a whitespace one is not.
_R1C1_END = re.compile(
    r"(?P<r>R(?:(?P<rabs>\d+)|\[(?P<rrel>\d+)\])?)?(?P<c>C(?:(?P<cabs>\d+)|\[(?P<crel>\d+)\])?)?\Z",
    re.IGNORECASE,
)


class _End(NamedTuple):
    """One side of a range, resolved: 0-based, ``None`` on an axis left unbounded. ``raw_row`` and
    ``raw_col`` are the number as written and its kind — ``(2, "abs")`` for `R2`, ``(2, "rel")``
    for `R[2]` — which is what real's reversed-range check reads; ``None`` where the axis carries no
    number."""

    row: int | None
    col: int | None
    raw_row: tuple[int, str] | None = None
    raw_col: tuple[int, str] | None = None


def _a1_end(part: str) -> _End | None:
    """An A1 endpoint, or ``None`` when the string is not one."""
    m = _A1_END.fullmatch(part)
    if not m:
        return None
    if m.group("rowonly"):
        n = int(m.group("rowonly"))
        return _End(n - 1, None) if n else None
    row = m.group("row")
    if row is not None and int(row) == 0:
        return None
    return _End(int(row) - 1 if row else None, _a1_col(m.group("col")))


def _r1c1_end(part: str) -> _End | None:
    """An R1C1 endpoint, or ``None`` when the string is not one — the grammar above."""
    m = _R1C1_END.fullmatch(part)
    if not m or not (m.group("r") or m.group("c")):
        return None

    def numbered(letter: str) -> tuple[int, tuple[int, str]] | None:
        """``(index, (raw, kind))`` for a letter that carries a number; ``None`` for one that does
        not."""
        absolute, relative = m.group(letter + "abs"), m.group(letter + "rel")
        if absolute is not None:
            n = int(absolute)
            if n == 0:
                raise ValueError(part)
            return n - 1, (n, "abs")
        if relative is not None:
            n = int(relative)
            return n, (n, "rel")
        return None

    try:
        rnum, cnum = numbered("r"), numbered("c")
    except ValueError:
        return None

    def index(letter: str, own, other) -> int | None:
        if own is not None:
            return own[0]
        if m.group(letter):  # present without a number: offset 0
            return 0
        return None if other is not None else 0  # absent: unbounded beside a numbered letter

    return _End(
        index("r", rnum, cnum),
        index("c", cnum, rnum),
        rnum[1] if rnum else None,
        cnum[1] if cnum else None,
    )


def _a1_classify(body: str) -> tuple[str, list[_End]] | None:
    """Which notation a cell part is in, with its endpoints: ``("a1", ends)``, ``("r1c1", ends)``,
    or ``None`` when it is neither. A1 first, then R1C1 — the order `_R1C1_END`'s comment states —
    and a lone A1 token has to be a full cell (see `_A1_END`)."""
    halves = body.split(":")
    if len(halves) > 2:
        return None
    a1 = [_a1_end(h) for h in halves]
    if all(a1) and (len(a1) == 2 or (a1[0].row is not None and a1[0].col is not None)):
        return "a1", a1  # type: ignore[return-value]
    r1c1 = [_r1c1_end(h) for h in halves]
    if all(r1c1):
        return "r1c1", r1c1  # type: ignore[return-value]
    return None


def _a1_col(letters: str) -> int:
    """Column letters to a 0-based index, base-26 with no zero digit (``A``->0, ``Z``->25,
    ``AA``->26)."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


# The two values that turn `prettyPrint` off, matched exactly -- see `_sheets_respond`.
_PRETTY_PRINT_FALSE = frozenset({"false", "0"})


def _a1_find(title: str, sheets: list[_Sheet]) -> _Sheet | None:
    """The sheet a title names, or None.

    Measured: lookup is CASE-INSENSITIVE (`data!B1:B3` answers from `Data`; compared as titles
    collide, `gsheets.same_title`, so `STRASSE` and `Straße` beside it stay two sheets), quoting
    is optional even for a title holding a space, a colon, brackets or a bang, and inside quotes a
    doubled apostrophe is one apostrophe."""
    if title[:1] == "'" and title[-1:] == "'":
        title = title[1:-1].replace("''", "'")
    return next((s for s in sheets if gsheets.same_title(s.title, title)), None)


def _a1_looks_like_a_range(body: str) -> bool:
    return _a1_classify(body) is not None


def _a1_sheet(spec: str, sheets: list[_Sheet]) -> tuple[_Sheet, str]:
    """``(sheet, body)`` — which sheet a spec addresses, and the cell part left over (``""`` for a
    bare sheet name, meaning the whole grid).

    Measured against a real multi-sheet workbook:

    * the separator is the LAST bang, not the first — `has!bang!A1` addresses the sheet `has!bang`,
      and splitting at the first would leave `bang!A1` as the cell part
    * a spec with NO bang is parsed as a range FIRST and only then as a sheet name, so bare `A1` is
      cell A1 of the first sheet even in a workbook that has a sheet named `A1`, while bare `Data`
      is the sheet because four letters cannot be a cell reference. R1C1 before a sheet name too
      (the order `_R1C1_END`'s comment states, with the sheets it was measured against)
    * an unqualified range answers from the sheet at index 0
    * a name no sheet has 400s with the same `Unable to parse range` message unparseable garbage
      gets — resolving to an empty grid instead would be indistinguishable from an empty range
    """
    # Not stripped anywhere — the whitespace rule in `_R1C1_END`'s comment.
    bare = spec
    if "!" in bare:
        title, _, body = bare.rpartition("!")
        if title in ("", "''") and _a1_looks_like_a_range(body):
            # An EMPTY title is the first sheet (the rule in `_R1C1_END`'s comment), and only
            # before a range: `!Data` and a bare `!` are unparseable.
            return sheets[0], body
        found = _a1_find(title, sheets)
        if found is not None and body:
            return found, body
        # A title that itself holds a bang, named bare: `has!bang` is the WHOLE sheet, so the
        # split above leaves `has` (no such sheet) over `bang` (no such range). Measured.
        # `Sheet1!` with nothing after it falls here too, and is malformed either way.
        whole = _a1_find(bare, sheets)
        if whole is None:
            raise gerr.invalid_argument(f"Unable to parse range: {spec}")
        return whole, ""
    if _a1_looks_like_a_range(bare):
        return sheets[0], bare
    found = _a1_find(bare, sheets)
    if found is None:
        raise gerr.invalid_argument(f"Unable to parse range: {spec}")
    return found, ""


def _a1_range(
    spec: str, body: str, sheet: _Sheet, *, clamp: bool = True
) -> tuple[int, int, int, int]:
    """Resolve the cell part of an A1 range to half-open ``(r0, c0, r1, c1)`` against this sheet.

    Handles every form a client may send: ``A1:B2``, ``B2`` (one cell), ``A:B`` / ``1:3`` (whole
    columns / rows), ``A2:B`` (one edge unbounded), ``R1C1:R2C2`` (either half in R1C1) and ``""``
    (the whole sheet, which is what a bare sheet name resolves to). Everything resolves against the
    GRID, so a range may be wider than the data — the caller trims.

    Two boundary rules, measured against a real spreadsheet: the range's END may overflow and is
    CLAMPED (``A1:AA5`` on a 26-column sheet returns ``A1:Z5``), its START may not. A write's
    bound is the range as sent (``clamp=False``), an end past the grid kept.

    ``spec`` is the whole requested range, because that — not the offending half — is what real
    Sheets names back: `A1:` reports "Unable to parse range: A1:", never a bare "".
    """
    nrows, ncols = sheet.rows, sheet.cols
    if not body:
        return 0, 0, nrows, ncols
    classified = _a1_classify(body)
    if classified is None:
        raise gerr.invalid_argument(f"Unable to parse range: {spec}")
    notation, ends = classified
    if len(ends) == 1:  # a single reference: one cell, one whole row, one column
        (start,) = ends
        r0f, c0f = (0 if start.row is None else start.row), (0 if start.col is None else start.col)
        r1 = nrows if start.row is None else start.row + 1
        c1 = ncols if start.col is None else start.col + 1
    else:
        start, end = ends
        if notation == "r1c1":
            # Real's reversed-range rule, on the numbers as written — see `_R1C1_END`.
            for a, b in ((start.raw_row, end.raw_row), (start.raw_col, end.raw_col)):
                if a is not None and b is not None and a[1] == b[1] and a[0] == b[0] + 1:
                    raise gerr.invalid_argument(f"Unable to parse range: {spec}")
        r0f = 0 if start.row is None else start.row
        c0f = 0 if start.col is None else start.col
        r1 = nrows if end.row is None else end.row + 1
        c1 = ncols if end.col is None else end.col + 1
        # Ranges are inclusive and may be written in either order (`B2:A1` == `A1:B2`), in R1C1
        # as in A1 (`R3C3:R1C1` == `A1:C3`), once past the rule above.
        if r1 < r0f + 1:
            r0f, r1 = r1 - 1, r0f + 1
        if c1 < c0f + 1:
            c0f, c1 = c1 - 1, c0f + 1
    if r0f >= nrows or c0f >= ncols or r0f < 0 or c0f < 0:
        # The START is outside the grid — refused, with the range echoed back unclamped; whole
        # columns by their letters alone and whole rows by their numbers alone (`_a1_axis_name`).
        if all(e.row is None for e in ends):
            named = _a1_axis_name(sheet, _a1_col_letters(c0f), _a1_col_letters(c1 - 1))
        elif all(e.col is None for e in ends):
            named = _a1_axis_name(sheet, str(r0f + 1), str(r1))
        else:
            named = _a1_name(sheet, r0f, c0f, r1, c1)
        raise gerr.invalid_argument(
            f"Range ({named}) exceeds grid limits. Max rows: {nrows}, max columns: {ncols}"
        )
    if not clamp:
        return r0f, c0f, r1, c1
    return r0f, c0f, min(r1, nrows), min(c1, ncols)


# A title the echo may spell without quotes. Measured: `Data` echoes bare while `'Second Sheet'`,
# `'2024'`, `'Bob''s Sheet'`, `'a:b'`, `'a[b]'`, `'has!bang'` and `'A1'` echo quoted. This pattern
# (plus the A1-reference exclusion below) is INFERRED from that sample rather than measured: it
# accounts for every title measured, but a title the sample does not cover could contradict it.
_A1_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _a1_title(title: str) -> str:
    """A sheet title as the echoed ``range`` spells it, quoted unless it is a plain identifier that
    is not itself a cell reference. An embedded apostrophe doubles.

    Measured 2026-09-12 on sheets so named: `R1C1` and `RC` echo quoted (`'RC'!A1`), as `A1` does,
    while `A`, no reference on its own, echoes bare (`A!A1:Z1000`)."""
    if _A1_PLAIN.fullmatch(title) and _a1_classify(title) is None:
        return title
    return "'" + title.replace("'", "''") + "'"


def _a1_col_letters(i: int) -> str:
    """A 0-based column index as A1 letters — the inverse of :func:`_a1_col`."""
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def _a1_axis_name(sheet: _Sheet, start: str, end: str) -> str:
    """Whole columns by their letters alone, or whole rows by their numbers alone, the way real
    names them when refusing a range past the grid — the naming rule in `_R1C1_END`'s comment,
    never ``ZZ1:ZZ1000`` or ``A1001:Z1001``."""
    title = _a1_title(sheet.title)
    return f"{title}!{start}" if start == end else f"{title}!{start}:{end}"


def _a1_name(sheet: _Sheet, r0: int, c0: int, r1: int, c1: int) -> str:
    """The resolved range in A1 form, which is what the response echoes.

    A single cell echoes as a bare reference (``Sheet1!A1``), not as ``A1:A1`` — measured: real
    Sheets collapses a 1x1 range even when the request spelled it out as ``A1:A1``."""
    title = _a1_title(sheet.title)
    start = f"{_a1_col_letters(c0)}{r0 + 1}"
    if r1 - r0 == 1 and c1 - c0 == 1:
        return f"{title}!{start}"
    return f"{title}!{start}:{_a1_col_letters(c1 - 1)}{r1}"


def _rstrip_empty(cells: list[str]) -> list[str]:
    while cells and cells[-1] == "":
        cells.pop()
    return cells


def _sheets_block(sheet: _Sheet, body: str, spec: str):
    """``(r0, c0, r1, c1, cells)`` for the cell part ``body`` against ``sheet``: the range as
    resolved against that sheet's grid, and the cells it covers as STORED — trimming happens in the
    caller, which knows how it is rendering them. ``spec`` is only what an error echoes.

    Takes an already-resolved sheet rather than re-parsing one out of a spec, so a caller that
    knows which sheet it wants cannot have it reinterpreted: a title reading as a cell or column
    reference (``A1``, ``AB``) would resolve against the FIRST sheet instead of itself.

    The bounds are the RANGE's, not the data's: callers echo them, so they must not shrink to the
    occupied cells."""
    rows = sheet.grid
    r0, c0, r1, c1 = _a1_range(spec, body, sheet)
    block = [
        [(rows[r][c] if c < len(rows[r]) else None) for c in range(c0, c1)]
        for r in range(r0, min(r1, len(rows)))
    ]
    return r0, c0, r1, c1, block


def _sheets_value(cell) -> dict:
    """A cell's ``ExtendedValue``.

    Measured: a string is ``stringValue``, a number ``numberValue``, a boolean ``boolValue``, and
    an empty cell carries no value object at all.

    ``bool`` is tested BEFORE the numeric branch because it is a subclass of ``int`` in Python —
    unguarded, TRUE would serve as ``numberValue: 1``.

    An error cell is ``errorValue`` with its type alone: an error typed as a literal (`#N/A`)
    carries no ``message``, measured 2026-10-26. A real cell may also hold ``formulaValue``, which
    nothing here stores (`gsheets.apply`)."""
    if (error := gsheets.cell_error(cell)) is not None:
        return {"errorValue": {"type": error}}
    cell = gsheets.cell_value(cell)
    if cell is None or cell == "":
        return {}
    if isinstance(cell, bool):
        return {"boolValue": cell}
    if isinstance(cell, (int, float)):
        return {"numberValue": cell}
    return {"stringValue": cell}


def _sheets_empty_grid_data(empty: _Empty) -> dict:
    """The `GridData` block for an empty grid range: where it starts and the metadata of the axis
    that is not empty, with no `rowData`. Measured 2026-10-04: an empty row range at row 1 is
    ``{"startRow": 1, "columnMetadata": [26 entries]}``, an empty column range at column 1
    ``{"startColumn": 1, "rowMetadata": [1000 entries]}``, and one empty on both axes the two
    starts alone. The keys come in that order, the starts first, measured 2026-10-05."""
    out: dict = {}
    if empty.r0:
        out["startRow"] = empty.r0
    if empty.c0:
        out["startColumn"] = empty.c0
    rows = min(empty.r1, empty.sheet.rows) - empty.r0
    cols = min(empty.c1, empty.sheet.cols) - empty.c0
    if rows > 0:
        out["rowMetadata"] = [{"pixelSize": SHEETS_ROW_PIXELS} for _ in range(rows)]
    if cols > 0:
        out["columnMetadata"] = [{"pixelSize": SHEETS_COL_PIXELS} for _ in range(cols)]
    return out


def _sheets_grid_data(sheet: _Sheet, body: str, spec: str) -> dict:
    """One ``GridData`` block for ``spreadsheets.get?includeGridData=true``.

    Measured: ``startRow``/``startColumn`` omitted when zero, proto3 dropping its defaults; and no
    ``rowData`` key at all on an empty sheet, whose block is metadata alone.

    Measured on 2026-10-05, through ``ranges`` and through an ``a1Range`` filter: a row's
    ``values`` end at its last cell holding a value, an empty cell before that being ``{}``; a row
    holding no value is ``{}`` itself; ``rowData`` ends at the last row holding a value; and the
    keys come in the order ``startRow``, ``startColumn``, ``rowData``, ``rowMetadata``,
    ``columnMetadata``.

    ``userEnteredValue`` and ``effectiveValue`` are equal here and both absent from an empty cell.
    Measured, they differ on real Sheets only for a FORMULA cell — the formula in the first, its
    result in the second — and a formula is stored as its text, so there is nothing to differ over.

    ``rowMetadata``/``columnMetadata`` cover the RANGE, one entry per row and column of it —
    measured, 2 and 2 for ``Data!A1:B2`` against the same sheet whose unscoped block carries 1000
    and 26. Every entry is identical (``pixelSize`` 21 for a row, 100 for a column), those being
    the default track sizes; a corpus states no track size, so there is nothing to vary."""
    r0, c0, r1x, c1, block = _sheets_block(sheet, body, spec)
    width = c1 - c0
    while block and not any(_sheets_cell_data(c) for c in block[-1]):
        block.pop()
    out: dict = {}
    if r0:
        out["startRow"] = r0
    if c0:
        out["startColumn"] = c0
    if block:
        row_data = []
        for row in block:
            vals = [_sheets_cell_data(row[i]) if i < len(row) else {} for i in range(width)]
            while vals and not vals[-1]:
                vals.pop()
            row_data.append({"values": vals} if vals else {})
        out["rowData"] = row_data
    out["rowMetadata"] = [{"pixelSize": SHEETS_ROW_PIXELS} for _ in range(r1x - r0)]
    out["columnMetadata"] = [{"pixelSize": SHEETS_COL_PIXELS} for _ in range(width)]
    return out


def _sheets_cell_data(cell) -> dict:
    """One cell's ``CellData``, or ``{}`` for one holding nothing.

    Keys in real's order, measured 2026-10-26: ``userEnteredValue``, ``effectiveValue``,
    ``formattedValue``, then ``userEnteredFormat`` for a cell given a number format, then
    ``effectiveFormat``. A cell a write emptied is served with its format keys alone
    (``gsheets.EMPTY_STRING``, ``gsheets.CLEARED``)."""
    v = _sheets_value(cell)
    nf = gsheets.cell_format(cell)
    if not v and not isinstance(cell, dict):
        return {}
    out = {}
    if v:
        out = {
            "userEnteredValue": v,
            "effectiveValue": v,
            "formattedValue": gsheets.formatted(cell),
        }
    if nf:
        out["userEnteredFormat"] = {"numberFormat": nf}
    out["effectiveFormat"] = _sheets_format(cell)
    return out


def _sheets_render(cell, render: str, date_time: str = "SERIAL_NUMBER"):
    """One cell as ``values.get`` returns it under ``render``.

    Measured: ``FORMATTED_VALUE`` gives the display string, while ``UNFORMATTED_VALUE`` and
    ``FORMULA`` both give the raw typed value — a JSON number for a number, a JSON boolean for a
    boolean. The two agree for every NON-FORMULA cell on the real API, and a formula is stored as
    its text (`gsheets.apply`), so they agree here for every cell.

    An empty cell is ``""`` under every option, measured — a JSON string even under
    ``UNFORMATTED_VALUE``, never null.

    Measured 2026-10-26: an error is its bare code under ``FORMATTED_VALUE`` and ``<code> ()``
    under the other two when it carries no message, and ``date_time=FORMATTED_STRING`` serves a
    cell with a DATE, TIME or DATE_TIME format as its display string under every option."""
    if (error := gsheets.cell_error(cell)) is not None:
        code = gsheets.ERROR_CODES[error]
        return code if render == "FORMATTED_VALUE" else f"{code} ()"
    value = gsheets.cell_value(cell)
    if value is None:
        return ""
    if render == "FORMATTED_VALUE":
        return gsheets.formatted(cell)
    nf = gsheets.cell_format(cell)
    if (
        date_time == "FORMATTED_STRING"
        and nf
        and nf["type"] in gsheets.DATE_TIME_TYPES
        and not isinstance(value, (bool, str))
    ):
        return gsheets.formatted(cell)
    return value


def _sheets_value_range(
    spec: str, sheets: list[_Sheet], major: str, render: str, date_time: str = "SERIAL_NUMBER"
) -> dict:
    """One ``ValueRange``.

    Trailing empty cells are dropped PER ROW rather than padded out to the requested bounds, so
    rows come back ragged; an interior gap stays ``""``; trailing empty rows are dropped
    altogether. Under ``COLUMNS`` the same rule applies per column, and a fully empty INTERIOR
    column comes back as ``[]`` rather than being dropped. All measured.

    A range holding nothing omits ``values`` entirely — a client tests for the key's presence, so
    an empty list would claim the range exists and is blank."""
    sheet, body = _a1_sheet(spec, sheets)
    r0, c0, r1, c1, raw = _sheets_block(sheet, body, spec)
    block = [[_sheets_render(c, render, date_time) for c in row] for row in raw]
    out = {"range": _a1_name(sheet, r0, c0, r1, c1), "majorDimension": major}
    if major == "COLUMNS":
        width = max((len(r) for r in block), default=0)
        block = [_rstrip_empty([(r[i] if i < len(r) else "") for r in block]) for i in range(width)]
    else:
        block = [_rstrip_empty(row) for row in block]
    while block and not block[-1]:
        block.pop()
    if block:
        out["values"] = block
    return out


_Sheet = gsheets.Sheet


def _workbook(request: Request, spreadsheet_id: str) -> tuple:
    """``(row, sheets)`` — the Drive row behind a spreadsheet and the sheets it serves.

    A document that STATES a grid answers with its stored sheets; one that does not answers with a
    SINGLE synthesized sheet holding ``_sheets_grid``'s line-per-cell reading. Prose is therefore
    one more grid, which is what leaves a single serving path below and stops the five Sheets reads
    disagreeing about a cell.

    A prose sheet's cells are strings and STAY strings. Nothing here sniffs a line for a number or
    a boolean: a cell's type is something a corpus states, never something this module infers.

    Inside a batch there is no lookup: real's batch does not implement the Sheets reads. Measured
    2026-10-04 on the five reads this module serves, and on the two POST reads' bodies 2026-10-06,
    the answer is 501 after the credential, the typed query values and a POST read's body and before
    the spreadsheet is looked up, so one that does not exist gets it as well. A `fields` mask naming
    nothing is refused ahead of it: measured 2026-10-22, `values/A1?fields=bogus` on a spreadsheet
    that exists and on one that does not, and `:getByDataFilter?fields=bogus` on one that does not,
    were the mask's 400 inside a batch."""
    if _BATCH_OUTER.get() is not None:
        raise gerr.unimplemented()
    row = _editor_doc(request, spreadsheet_id, expect="spreadsheet")
    stored = store.gdrive_sheets_for(auth.conn(request), spreadsheet_id)
    if not stored:
        return row, [_Sheet(0, 0, SHEETS_SHEET_TITLE, _sheets_grid(row["content"]))]
    return row, [
        _Sheet(
            s["sheet_id"],
            s["sheet_index"],
            s["title"],
            json.loads(s["grid"]),
            json.loads(s["properties"]) if s["properties"] else {},
        )
        for s in stored
    ]


def _sheets_settings(conn, spreadsheet_id: str) -> dict:
    """A workbook's stated settings (`locale`, `timeZone`, `autoRecalc`), empty when it states
    none (`store`'s `gdrive_spreadsheets`)."""
    rows = store.parts_for(
        conn, "google_drive", "gdrive_spreadsheets", (spreadsheet_id,), order_by="file_id"
    )
    return json.loads(rows[0]["properties"]) if rows else {}


def _sheets_url(spreadsheet_id: str, caller: Caller) -> str:
    """`spreadsheetUrl`. Measured 2026-10-26 with Google Workspace members' tokens, the accounts
    Backlot's callers are: real puts the CALLER's numeric account id on it as `ouid`, the owner and
    a reader of one spreadsheet each getting their own. A consumer account's token gets the bare
    link (measured 2026-10-27), and so does the admin/service token here, which is no account."""
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    return f"{url}?ouid={synth.google_user_id(caller.email)}" if caller.email else url


def _sheets_options(request: Request) -> tuple[str, str, str]:
    """Validate the read enums and return ``(majorDimension, valueRenderOption,
    dateTimeRenderOption)``. Real Sheets 400s
    on an unknown value; accepting one silently would hand back ROWS-shaped data to a client that
    asked for columns, and a silently unapplied option is worse than a refusal.
    """
    enums = _typed_query(
        request,
        {
            "majorDimension": lambda raw: _sheets_enum_value(raw, "major_dimension", _PJ_DIMENSION),
            # Measured over typed cells: FORMATTED_VALUE gives the display string "12",
            # UNFORMATTED_VALUE the JSON number 12, and FORMULA the same raw value as
            # UNFORMATTED_VALUE for every cell that is not a formula. A spreadsheet whose cells are
            # lines of stored text has only strings, so all three agree on one; a spreadsheet that
            # STATES its grid does not.
            "valueRenderOption": lambda raw: _sheets_enum_value(
                raw, "value_render_option", _PJ_RENDER
            ),
            # A date, time or date-time cell's serial number or its display string. Only a written
            # cell can be one: a corpus states no number formats.
            "dateTimeRenderOption": lambda raw: _sheets_enum_value(
                raw, "date_time_render_option", _PJ_DATETIME
            ),
        },
    )
    major = (enums["majorDimension"] or ["ROWS"])[-1]
    render = (enums["valueRenderOption"] or ["FORMATTED_VALUE"])[-1]
    date_time = (enums["dateTimeRenderOption"] or ["SERIAL_NUMBER"])[-1]
    return _sheets_major(major), render, date_time


def _sheets_major(name: str) -> str:
    """The dimension a read is answered by: `DIMENSION_UNSPECIFIED` answers by rows, and echoes
    `ROWS`, measured 2026-10-04 in the query string and in a JSON body."""
    return "COLUMNS" if name == "COLUMNS" else "ROWS"


_P_SHEETS_VALUES = [
    qp("majorDimension"),
    qp("valueRenderOption"),
    qp("dateTimeRenderOption"),
    *_P_SHEETS_STD,
]
_P_SHEETS_BATCH = [qp("ranges"), *_P_SHEETS_VALUES]


@router.get(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values:batchGet",
    openapi_extra={"parameters": _P_SHEETS_BATCH},
)
async def sheets_values_batch_get(spreadsheet_id: str, request: Request):
    """Several ranges in one round trip. Declared before ``values/{range}`` for clarity only —
    ``values:batchGet`` is a single path segment, so the two cannot collide.

    One unusable range fails the whole call rather than yielding a short ``valueRanges`` list: a
    partial batch leaves the caller unable to say which range it is missing.

    With no ``ranges`` at all, nothing is selected and ``valueRanges`` is omitted. NOTE: that is
    the natural reading of a parameter with no default, NOT a response diffed against real
    Sheets — unlike the rest of this module's behaviour, it is unverified."""
    _sheets_alt(request)
    _require(request)
    major, render, date_time = _sheets_options(request)
    tree, _ = _sheets_mask(request, "sheets.BatchGetValuesResponse")
    _row, sheets = _workbook(request, spreadsheet_id)
    ranges = request.query_params.getlist("ranges")
    body = {"spreadsheetId": spreadsheet_id}
    if ranges:
        body["valueRanges"] = [
            _sheets_value_range(r, sheets, major, render, date_time) for r in ranges
        ]
    return _sheets_respond(request, body, tree)


@router.get(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values/{a1_range:path}",
    openapi_extra={"parameters": _P_SHEETS_VALUES},
)
async def sheets_values_get(spreadsheet_id: str, a1_range: str, request: Request):
    """One range of a spreadsheet, ACL-enforced through the same lookup as ``spreadsheets.get``."""
    _sheets_alt(request)
    _require(request)
    major, render, date_time = _sheets_options(request)
    tree, _ = _sheets_mask(request, "sheets.ValueRange")
    _row, sheets = _workbook(request, spreadsheet_id)
    return _sheets_respond(
        request, _sheets_value_range(a1_range, sheets, major, render, date_time), tree
    )


# --- the two reads issued over POST ------------------------------------------------------------
#
# A DataFilter selects the same cells an A1 range does, by range or by grid indices, or selects them
# by developer metadata (`developerMetadataLookup`), which a corpus never carries, so a lookup
# selects nothing (`_sheets_check_lookup`). Measured, the two endpoints disagree about an ABSENT
# filter list: `values:batchGetByDataFilter` refuses it ("Must specify at least one dataFilter.")
# while `spreadsheets:getByDataFilter` treats it as "every sheet".
#
# The request messages as `backlot.protojson` reads them: the Sheets v4 discovery document's fields,
# in proto field order (the order real echoes a filter back in) rather than the order the document
# lists them, with the wrapper fields measured 2026-10-04 by the `.value` real's refusal names.


_PJ_LOCATION_TYPE = _pj_enum(
    "DeveloperMetadataLocationType",
    "DEVELOPER_METADATA_LOCATION_TYPE_UNSPECIFIED",
    "ROW",
    "COLUMN",
    "SHEET",
    "SPREADSHEET",
)
_PJ_MATCHING = _pj_enum(
    "DeveloperMetadataLocationMatchingStrategy",
    "DEVELOPER_METADATA_LOCATION_MATCHING_STRATEGY_UNSPECIFIED",
    "EXACT_LOCATION",
    "INTERSECTING_LOCATION",
)
_PJ_VISIBILITY = _pj_enum(
    "DeveloperMetadataVisibility",
    "DEVELOPER_METADATA_VISIBILITY_UNSPECIFIED",
    "DOCUMENT",
    "PROJECT",
)
_PJ_GRID_RANGE = protojson.Message(
    f"{_SHEETS_ENUM}.GridRange",
    (
        protojson.Field("sheetId", "sheet_id", "int32"),
        protojson.Field("startRowIndex", "start_row_index", "int32", wrapper=True),
        protojson.Field("endRowIndex", "end_row_index", "int32", wrapper=True),
        protojson.Field("startColumnIndex", "start_column_index", "int32", wrapper=True),
        protojson.Field("endColumnIndex", "end_column_index", "int32", wrapper=True),
    ),
)
_PJ_DIMENSION_RANGE = protojson.Message(
    f"{_SHEETS_ENUM}.DimensionRange",
    (
        protojson.Field("sheetId", "sheet_id", "int32"),
        protojson.Field("dimension", "dimension", "enum", enum=_PJ_DIMENSION),
        protojson.Field("startIndex", "start_index", "int32", wrapper=True),
        protojson.Field("endIndex", "end_index", "int32", wrapper=True),
    ),
)
_PJ_METADATA_LOCATION = protojson.Message(
    f"{_SHEETS_ENUM}.DeveloperMetadataLocation",
    (
        protojson.Field("locationType", "location_type", "enum", enum=_PJ_LOCATION_TYPE),
        protojson.Field("spreadsheet", "spreadsheet", "bool", oneof="location"),
        protojson.Field("sheetId", "sheet_id", "int32", oneof="location"),
        protojson.Field(
            "dimensionRange",
            "dimension_range",
            "message",
            message=_PJ_DIMENSION_RANGE,
            oneof="location",
        ),
    ),
)
_PJ_METADATA_LOOKUP = protojson.Message(
    f"{_SHEETS_ENUM}.DeveloperMetadataLookup",
    (
        protojson.Field("locationType", "location_type", "enum", enum=_PJ_LOCATION_TYPE),
        protojson.Field(
            "metadataLocation", "metadata_location", "message", message=_PJ_METADATA_LOCATION
        ),
        protojson.Field(
            "locationMatchingStrategy", "location_matching_strategy", "enum", enum=_PJ_MATCHING
        ),
        protojson.Field("metadataId", "metadata_id", "int32", wrapper=True),
        protojson.Field("metadataKey", "metadata_key", "string", wrapper=True),
        protojson.Field("metadataValue", "metadata_value", "string", wrapper=True),
        protojson.Field("visibility", "visibility", "enum", enum=_PJ_VISIBILITY),
    ),
)
_PJ_DATA_FILTER = protojson.Message(
    f"{_SHEETS_ENUM}.DataFilter",
    (
        protojson.Field(
            "developerMetadataLookup",
            "developer_metadata_lookup",
            "message",
            message=_PJ_METADATA_LOOKUP,
            oneof="filter",
        ),
        protojson.Field("a1Range", "a1_range", "string", oneof="filter"),
        protojson.Field(
            "gridRange", "grid_range", "message", message=_PJ_GRID_RANGE, oneof="filter"
        ),
    ),
)
_PJ_FILTERS = protojson.Field(
    "dataFilters", "data_filters", "message", message=_PJ_DATA_FILTER, repeated=True
)
_PJ_BATCH_GET_BY_FILTER = protojson.Message(
    f"{_SHEETS_ENUM}.BatchGetValuesByDataFilterRequest",
    (
        _PJ_FILTERS,
        protojson.Field("majorDimension", "major_dimension", "enum", enum=_PJ_DIMENSION),
        protojson.Field("valueRenderOption", "value_render_option", "enum", enum=_PJ_RENDER),
        protojson.Field(
            "dateTimeRenderOption", "date_time_render_option", "enum", enum=_PJ_DATETIME
        ),
    ),
)
_PJ_COMMENTS_VIEW = _pj_enum(
    "CommentsViewMode",
    "COMMENTS_VIEW_MODE_UNSPECIFIED",
    "COMMENTS_VIEW_MODE_DEFAULT_FOR_CURRENT_ACCESS",
    "COMMENTS_VIEW_MODE_OMITTED",
    "COMMENTS_VIEW_MODE_INCLUDED",
)


def _sheets_comments_view(mode: int, comments: bool, can_comment: bool) -> str:
    """The `commentsViewMode` a spreadsheet read answers with, for the mode it was asked for by
    number, refusing a mask that names a comment field (`_gmask_select`) when it asked for none, and
    a caller who may not comment when it asked for them outright.

    Measured 2026-10-22 as the spreadsheet's owner, and as a user it was shared with as a reader and
    then as a commenter: `UNSPECIFIED` and `OMITTED`, an absent mode among them, answer `OMITTED`
    and refuse a comment field with the 400 below. `DEFAULT_FOR_CURRENT_ACCESS` answers `INCLUDED`
    to a caller whose `files.get` says `canComment: true` and `OMITTED` to the reader, and
    `INCLUDED` the same to the first and the 403 below to the reader. Here the owner alone may
    comment (`routers.google._drive_file_capabilities`). A number the enum does not declare,
    which a body may send (`4`, `99`, `-1`), answers `OMITTED`, takes a comment field and is no
    403. These follow the spreadsheet's lookup and precede the `ranges` `spreadsheets.get` reads,
    where `:getByDataFilter` reads its filters first."""
    if mode in (0, 2):
        if comments:
            raise gerr.invalid_argument(
                "Field mask may not contain comment-specific fields if comments are not requested"
            )
        return "COMMENTS_VIEW_MODE_OMITTED"
    if mode == 3 and not can_comment:
        raise gerr.GoogleError(
            403,
            "You do not have permission to view comments on this spreadsheet.",
            reason="forbidden",
            status="PERMISSION_DENIED",
        )
    if mode in (1, 3) and can_comment:
        return "COMMENTS_VIEW_MODE_INCLUDED"
    return "COMMENTS_VIEW_MODE_OMITTED"


_PJ_GET_BY_FILTER = protojson.Message(
    f"{_SHEETS_ENUM}.GetSpreadsheetByDataFilterRequest",
    (
        _PJ_FILTERS,
        protojson.Field("includeGridData", "include_grid_data", "bool"),
        protojson.Field("excludeTablesInBandedRanges", "exclude_tables_in_banded_ranges", "bool"),
        protojson.Field("commentsViewMode", "comments_view_mode", "enum", enum=_PJ_COMMENTS_VIEW),
    ),
)


class _Empty(NamedTuple):
    """A grid range that holds no cell, an end equal to its start on either axis. Real answers one
    at 200, measured 2026-10-04: `#REF!` as the range on the values-level read, and on the
    spreadsheet-level one a block with no rows or no columns. ``name`` is how a DataFilter clear
    names it (`_grid_range_name`)."""

    sheet: _Sheet
    r0: int
    c0: int
    r1: int
    c1: int
    name: str = ""


def _int32(n: int) -> int:
    """``n`` as a Java int would hold it. Real names the row after `startRowIndex` 2147483647 as
    `-2147483648`, measured 2026-10-04."""
    return (n + 2**31) % 2**32 - 2**31


def _grid_range_name(sheet: _Sheet, grid: dict) -> str:
    """A grid range as real names it when refusing one past the grid: only the edges the request
    set, start and end each as column letters (none past `ZZZ`, column 18277) then a row number, one
    of them alone when the two read the same, and an `(empty) ` in front when an axis was sent with
    its end at its start (an end of 0 with no start does not count). Measured 2026-10-04 on every
    combination of the indexes sent, with the start row or the start column past the grid, among
    them (start row, end row, start column, end column, `-` for one not sent)::

        1000, -, -, -       Sheet1!1001:
        1000, -, 1, 3       Sheet1!B1001:C
        -, -, 26, 27        Sheet1!AA
        5, 5, 26, -         (empty) Sheet1!AA6:5
        -, 0, 26, -         Sheet1!AA:0
        -, -, 18278, -      Sheet1!
        -, -, 26, 18279     Sheet1!AA:
    """
    sr, er = grid.get("start_row_index"), grid.get("end_row_index")
    sc, ec = grid.get("start_column_index"), grid.get("end_column_index")

    def letters(column: int | None) -> str:
        return _a1_col_letters(column) if column is not None and column <= 18277 else ""

    start = letters(sc) + (str(_int32(sr + 1)) if sr is not None else "")
    end = letters(None if ec is None else ec - 1) + (str(er) if er is not None else "")
    name = start if start == end else f"{start}:{end}"
    empty = (sr is not None and er == sr) or (sc is not None and ec == sc)
    return ("(empty) " if empty else "") + f"{_a1_title(sheet.title)}!{name}"


def _sheets_grid_selection(grid: dict, sheets: list[_Sheet], sheet_word: str):
    """A `gridRange`, checked as real checks it once the spreadsheet is found, as the A1 spec it
    selects or an :class:`_Empty`.

    In real's order, measured 2026-10-04 by sending two of them wrong together: the sheet
    (`No grid with id` on the values-level read, `No sheet with id` on the other: ``sheet_word``),
    then a negative index, then an end before its start (rows before columns), then a start past
    the grid (:func:`_grid_range_name`), and only then an empty range. An end past the grid is
    clamped, as an A1 range's is."""
    sheet_id = grid.get("sheet_id", 0)
    sheet = next((s for s in sheets if s.sheet_id == sheet_id), None)
    if sheet is None:
        raise gerr.invalid_argument(f"No {sheet_word} with id: {sheet_id}")
    sr, er = grid.get("start_row_index"), grid.get("end_row_index")
    sc, ec = grid.get("start_column_index"), grid.get("end_column_index")
    if any(i is not None and i < 0 for i in (sr, er, sc, ec)):
        raise gerr.invalid_argument("GridRange indexes must be >= 0")
    for start, end, axis in ((sr, er, "Row"), (sc, ec, "Column")):
        if start is not None and end is not None and end < start:
            raise gerr.invalid_argument(
                f"end{axis}Index[{end}] cannot be before start{axis}Index[{start}]"
            )
    r0, c0 = sr or 0, sc or 0
    r1 = sheet.rows if er is None else er
    c1 = sheet.cols if ec is None else ec
    if r0 >= sheet.rows or c0 >= sheet.cols:
        raise gerr.invalid_argument(
            f"Range ({_grid_range_name(sheet, grid)}) exceeds grid limits. "
            f"Max rows: {sheet.rows}, max columns: {sheet.cols}"
        )
    if r1 == r0 or c1 == c0:
        return _Empty(sheet, r0, c0, r1, c1, _grid_range_name(sheet, grid))
    # An end past the grid is cut to it here rather than left to the A1 parser, which reads three
    # column letters at most: `endColumnIndex: 20000` answers `A1:Z1000`, measured 2026-10-04.
    return _a1_name(sheet, r0, c0, min(r1, sheet.rows), min(c1, sheet.cols))


def _sheets_bounds(selection, sheets: list[_Sheet]) -> tuple[_Sheet, int, int, int, int]:
    """The sheet and half-open ``(r0, c0, r1, c1)`` a filter's A1 spec or :class:`_Empty` covers,
    an end past the grid cut to it."""
    if isinstance(selection, _Empty):
        e = selection
        return e.sheet, e.r0, e.c0, min(e.r1, e.sheet.rows), min(e.c1, e.sheet.cols)
    sheet, part = _a1_sheet(selection, sheets)
    return (sheet, *_a1_range(selection, part, sheet))


def _sheets_check_lookup(lookup: dict, sheets: list[_Sheet]) -> None:
    """A `developerMetadataLookup`'s refusals. A corpus states no developer metadata, so a lookup
    that passes them matches nothing — real's answer for one on a spreadsheet carrying none,
    measured 2026-10-04 — and the caller serves it as such.

    Real's checks, in the order it makes them, measured the same day over every combination of
    six `locationType`s, eight locations and four `locationMatchingStrategy`s (a location with no
    member, `{}`, counts as none)::

        locationType SPREADSHEET, a location other than spreadsheet: true
                                            Cannot limit by location type of SPREADSHEET for the
                                            location <the location's member>
        EXACT_LOCATION beside a locationType     The locationMatchingStrategy was specified as …
        a strategy and no location               A locationMatchingStrategy was specified, but …
        INTERSECTING_LOCATION, spreadsheet: true DeveloperMetadataLookup.spreadsheet is true, …
        a locationType number not in the enum    500
        the location (:func:`_sheets_check_dimension_range`, or a sheetId no sheet has)
        ROW, COLUMN or SHEET with a spreadsheet location, a strategy or a visibility number not
        in the enum                              500

    The location's own `locationType` is read and ignored."""
    location_type = lookup.get("location_type", 0)
    strategy = lookup.get("location_matching_strategy", 0)
    location = lookup.get("metadata_location") or {}
    member = next(
        (field for field in _PJ_METADATA_LOCATION.fields if field.name in location and field.oneof),
        None,
    )
    whole_spreadsheet = (
        member is not None and member.name == "spreadsheet" and location["spreadsheet"]
    )
    if location_type == 4 and member is not None and not whole_spreadsheet:
        raise gerr.invalid_argument(
            f"Cannot limit by location type of SPREADSHEET for the location {member.json_name}"
        )
    if strategy == 1 and location_type != 0:
        raise gerr.invalid_argument(
            "The locationMatchingStrategy was specified as EXACT_LOCATION, but a locationType was "
            "also specified: lookups cannot limit by a location type when matching an exact "
            "location."
        )
    if strategy != 0 and member is None:
        raise gerr.invalid_argument(
            "A locationMatchingStrategy was specified, but no metadataLocation was specified: "
            "lookups must always specify a metadataLocation when specifying a "
            "locationMatchingStrategy."
        )
    if strategy == 2 and whole_spreadsheet:
        raise gerr.invalid_argument(
            "DeveloperMetadataLookup.spreadsheet is true, but locationMatchingStrategy was "
            "specified as INTERSECTING."
        )
    if not 0 <= location_type < len(_PJ_LOCATION_TYPE.names):
        raise gerr.internal_error()
    if member is not None and member.name == "sheet_id":
        if all(s.sheet_id != location["sheet_id"] for s in sheets):
            raise gerr.invalid_argument(f"No grid with id: {location['sheet_id']}")
    if member is not None and member.name == "dimension_range":
        _sheets_check_dimension_range(location["dimension_range"], sheets)
    if location_type in (1, 2, 3) and member is not None and member.name == "spreadsheet":
        raise gerr.internal_error()
    if not 0 <= strategy < len(_PJ_MATCHING.names):
        raise gerr.internal_error()
    if not 0 <= lookup.get("visibility", 0) < len(_PJ_VISIBILITY.names):
        raise gerr.internal_error()


def _sheets_check_dimension_range(dimension_range: dict, sheets: list[_Sheet]) -> None:
    """A lookup's `dimensionRange`, checked in the order real checks it (measured 2026-10-04 with
    two members wrong at once): both indexes, exactly one row or column (`endIndex` one past
    `startIndex`), both indexes non-negative, the sheet, a dimension, a dimension number the enum
    does not declare (real's 500), and a start inside the grid."""
    if "start_index" not in dimension_range or "end_index" not in dimension_range:
        raise gerr.invalid_argument(
            "DimensionRange must specify both a startIndex and an endIndex."
        )
    start, end = dimension_range["start_index"], dimension_range["end_index"]
    # In Java int arithmetic: `startIndex: 2147483647, endIndex: -2147483648` passes this and is
    # refused as negative, measured 2026-10-04.
    if _int32(end - start) != 1:
        raise gerr.invalid_argument("DimensionRange must represent a single row or column.")
    if start < 0 or end < 0:
        raise gerr.invalid_argument("DimensionRange indexes must be >= 0")
    sheet_id = dimension_range.get("sheet_id", 0)
    sheet = next((s for s in sheets if s.sheet_id == sheet_id), None)
    if sheet is None:
        raise gerr.invalid_argument(f"No grid with id: {sheet_id}")
    dimension = dimension_range.get("dimension", 0)
    if dimension == 0:
        raise gerr.invalid_argument("No dimension specified")
    if dimension not in (1, 2):
        raise gerr.internal_error()
    size, axis = (sheet.rows, "ROWS") if dimension == 1 else (sheet.cols, "COLUMNS")
    if start >= size:
        raise gerr.invalid_argument(
            f"DimensionRange startIndex [{start}] is after the last {axis} index "
            f"[{size - 1}] of the sheet [{sheet_id}]."
        )


def _sheets_selections(
    filters: list[dict], sheets: list[_Sheet], *, values_level: bool, prefix: str | None = None
) -> list:
    """What each filter selects, in the order sent: an A1 spec, an :class:`_Empty`, or ``None`` for
    a developer metadata lookup (:func:`_sheets_check_lookup`).

    Real checks the filters one at a time and answers the first that fails, measured 2026-10-04
    with a failing filter on either side of another, a lookup's refusal and a range's alike. The
    values-level read puts `Invalid dataFilter[N]: ` in front of the refusal and the
    spreadsheet-level one does not. An `a1Range` is resolved here, past-grid check and all, so its
    refusal is reported against the filter that carried it."""
    selections = []
    for i, f in enumerate(filters):
        try:
            if "a1_range" in f:
                spec = f["a1_range"]
                sheet, part = _a1_sheet(spec, sheets)
                _a1_range(spec, part, sheet)
                selections.append(spec)
            elif "grid_range" in f:
                word = "grid" if values_level else "sheet"
                selections.append(_sheets_grid_selection(f["grid_range"], sheets, word))
            elif "developer_metadata_lookup" in f:
                _sheets_check_lookup(f["developer_metadata_lookup"], sheets)
                selections.append(None)
            else:
                raise gerr.invalid_argument("dataFilter.filter must be specified.")
        except gerr.GoogleError as exc:
            if exc.status_code != 400 or not values_level:
                raise
            lead = f"Invalid dataFilter[{i}]: " if prefix is None else prefix
            raise gerr.invalid_argument(f"{lead}{exc.message}") from None
    return selections


def _sheets_filter_echo(f: dict) -> dict:
    """A filter as the values-level read echoes it beside its answer: the proto real read, back in
    JSON, so an accepted spelling comes back canonical — `"startRowIndex": "+1"` as `1` —
    with a plain field at its default left out and a wrapper field kept: `sheetId: 0` is absent
    and `startRowIndex: 0` present. Measured 2026-10-04."""
    if "a1_range" in f:
        return {"a1Range": f["a1_range"]}
    grid = f["grid_range"]
    return {
        "gridRange": {
            field.json_name: grid[field.name]
            for field in _PJ_GRID_RANGE.fields
            if field.name in grid and (field.wrapper or grid[field.name])
        }
    }


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values:batchGetByDataFilter",
    openapi_extra={"parameters": _P_SHEETS_STD},
)
async def sheets_values_batch_get_by_data_filter(spreadsheet_id: str, request: Request):
    """``values:batchGet`` addressed by DataFilter rather than by A1 string.

    A read, issued over POST because the filters do not fit in a query string. Each entry carries
    the ``valueRange`` AND the filters that selected it — measured — so a caller that sent several
    can tell which answer belongs to which. `majorDimension`, `valueRenderOption` and
    `dateTimeRenderOption` may come in the query too, which wins (`_sheets_query_and_body`).

    In the order real answers, measured 2026-10-04 with two wrong at once: an `alt` naming no format
    (`_sheets_alt`), the credential, then the body (``protojson.read``), then the `fields` mask
    (`_sheets_mask`), then the spreadsheet's lookup, then a `valueRenderOption` number the
    enum does not declare (a 400 of the service's own), then an empty filter list, then the filters
    (:func:`_sheets_selections`), then a `majorDimension` number it does not declare (a 500). A
    `dateTimeRenderOption` number it does not declare reads as `SERIAL_NUMBER`."""
    _sheets_alt(request)
    _require(request)
    query, body = await _sheets_query_and_body(
        request,
        {
            "majorDimension": lambda raw: _sheets_enum_value(raw, "major_dimension", _PJ_DIMENSION),
            "valueRenderOption": lambda raw: _sheets_enum_value(
                raw, "value_render_option", _PJ_RENDER
            ),
            "dateTimeRenderOption": lambda raw: _sheets_enum_value(
                raw, "date_time_render_option", _PJ_DATETIME
            ),
        },
        _PJ_BATCH_GET_BY_FILTER,
    )
    tree, _ = _sheets_mask(request, "sheets.BatchGetValuesByDataFilterResponse")
    _row, sheets = _workbook(request, spreadsheet_id)
    render = query["valueRenderOption"]
    if render is None:
        render = body.get("value_render_option", 0)
        if not 0 <= render < len(_PJ_RENDER.names):
            raise gerr.invalid_argument("Invalid valueRenderOption: UNRECOGNIZED")
        render = _PJ_RENDER.names[render]
    filters = body.get("data_filters", [])
    if not filters:
        raise gerr.invalid_argument("Must specify at least one dataFilter.")
    selections = _sheets_selections(filters, sheets, values_level=True)
    major = query["majorDimension"]
    if major is None:
        major = body.get("major_dimension", 0)
        if not 0 <= major < len(_PJ_DIMENSION.names):
            raise gerr.internal_error()
        major = _PJ_DIMENSION.names[major]
    major = _sheets_major(major)
    date_time = query["dateTimeRenderOption"]
    if date_time is None:
        date_time = body.get("date_time_render_option", 0)
        date_time = _PJ_DATETIME.names[date_time] if 0 <= date_time < 2 else "SERIAL_NUMBER"

    # NOT the order the filters arrived in. Measured: the answers come back sorted by where each
    # range starts, column before row — `Data!A2` precedes `Data!B1`, `Data!B9` precedes
    # `Data!B10`, a shorter range precedes the one that extends it, and a sheet earlier in the
    # workbook comes first. Each entry still carries the filters that selected it, so a caller
    # pairs by those rather than by position.
    def where(i: int):
        sheet, r0, c0, r1, c1 = _sheets_bounds(selections[i], sheets)
        return (sheet.index, c0, r0, c1, r1)

    def answer(selection) -> dict:
        if isinstance(selection, _Empty):
            return {"range": "#REF!", "majorDimension": major}
        return _sheets_value_range(selection, sheets, major, render, date_time)

    out: dict = {"spreadsheetId": spreadsheet_id}
    # Filters whose answers name the same range share one entry, which lists them in the order
    # sent, measured 2026-10-04: `Data!A1:A1` beside `Data!A1`, an `a1Range` beside the `gridRange`
    # for its cells, two ranges equal once an end past the grid is cut, and two ranges with no
    # cells in different places, rows or columns, both `#REF!`.
    first: dict[str, int] = {}
    entries: dict[str, dict] = {}
    for i, selection in enumerate(selections):
        if selection is None:
            continue
        value_range = answer(selection)
        key = value_range["range"]
        if key not in entries:
            first[key] = i
            entries[key] = {"valueRange": value_range, "dataFilters": []}
        entries[key]["dataFilters"].append(_sheets_filter_echo(filters[i]))
    if entries:
        # A lookup selects nothing and leaves no entry; with nothing else, there is no
        # `valueRanges` at all, measured 2026-10-04.
        out["valueRanges"] = [entries[k] for k in sorted(entries, key=lambda k: where(first[k]))]
    return _sheets_respond(request, out, tree)


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}:getByDataFilter",
    openapi_extra={"parameters": _P_SHEETS_STD},
)
async def sheets_get_by_data_filter(spreadsheet_id: str, request: Request):
    """``spreadsheets.get`` addressed by DataFilter. Same response, and the filters scope the
    ``sheets`` array as ``ranges`` does but for cells two filters cover (below) — measured,
    including that NO filter means every sheet rather than the refusal its values-level sibling
    gives, and so does a list of developer metadata lookups alone, which select nothing (measured
    2026-10-04). The credential, the body, the spreadsheet's lookup and the filters come in the
    order its sibling's docstring gives. `includeGridData`, `excludeTablesInBandedRanges` and
    `commentsViewMode` may come in the query too, read as `spreadsheets.get` reads them, and the
    query's wins (`_sheets_query_and_body`)."""
    _sheets_alt(request)
    caller = _require(request)
    query, body = await _sheets_query_and_body(request, _SHEETS_GET_FLAGS, _PJ_GET_BY_FILTER)
    tree, comments = _sheets_mask(request, "sheets.Spreadsheet")
    row, sheets = _workbook(request, spreadsheet_id)
    selections = _sheets_selections(body.get("data_filters", []), sheets, values_level=False)
    owned = _drive_owned_by(row["author_email"], caller.email)
    mode = query["commentsViewMode"]
    mode = (
        body.get("comments_view_mode", 0) if mode is None else _PJ_COMMENTS_VIEW.names.index(mode)
    )
    view = _sheets_comments_view(mode, comments, owned)
    # Filters that cover the same cells share one `data` block, placed where the first of them was
    # sent, measured 2026-10-04 on the pairs with cells `sheets_values_batch_get_by_data_filter`
    # lists and on one empty range sent twice. Two empty ranges in different places stay two
    # blocks, and `spreadsheets.get` answers a `ranges` value sent twice with a block for each.
    specs, seen = [], set()
    for selection in selections:
        if selection is None:
            continue
        sheet, *cells = _sheets_bounds(selection, sheets)
        if (sheet.index, *cells) not in seen:
            seen.add((sheet.index, *cells))
            specs.append(selection)
    include = query["includeGridData"]
    grid = _gmask_wants_grid(
        tree, body.get("include_grid_data", False) if include is None else include
    )
    return _sheets_respond(
        request,
        _sheets_book(
            spreadsheet_id,
            row,
            sheets,
            specs,
            grid,
            view,
            tree,
            settings=_sheets_settings(auth.conn(request), spreadsheet_id),
            url=_sheets_url(spreadsheet_id, caller),
        ),
        tree,
    )


# --- Sheets `values` writes ----------------------------------------------------------------------
#
# Measured against the live Sheets v4 API on 2026-10-26, as a Workspace member writing a
# spreadsheet it owns and as one it was shared with to read. What a written string becomes is
# `gsheets.apply`'s, how a number displays `gsheets.render`'s; this section is the
# request, its refusals, and the counts the answer carries.
#
# A write replaces the workbook's sheets in the overlay together with the file's `content` and
# `updated_ts` (`store.replace_parts`, `store.PATCHABLE`). Real's Drive shows both about a minute
# later; this shows them at once.

_PJ_INSERT_DATA = _pj_enum("InsertDataOption", "OVERWRITE", "INSERT_ROWS")
_PJ_VALUE_INPUT = _pj_enum(
    "ValueInputOption", "INPUT_VALUE_OPTION_UNSPECIFIED", "RAW", "USER_ENTERED"
)
_PJ_VALUE_RANGE = protojson.Message(
    f"{_SHEETS_ENUM}.ValueRange",
    (
        protojson.Field("range", "range", "string"),
        protojson.Field("majorDimension", "major_dimension", "enum", enum=_PJ_DIMENSION),
        protojson.Field("values", "values", "rows"),
    ),
)
# `values.update` takes its `ValueRange` as the request's `data` field, which every refusal names.
_PJ_UPDATE = protojson.Message(
    f"{_SHEETS_ENUM}.UpdateValuesRequest",
    (protojson.Field("data", "data", "message", message=_PJ_VALUE_RANGE),),
)
_PJ_BATCH_UPDATE = protojson.Message(
    f"{_SHEETS_ENUM}.BatchUpdateValuesRequest",
    (
        protojson.Field("valueInputOption", "value_input_option", "enum", enum=_PJ_VALUE_INPUT),
        protojson.Field("data", "data", "message", message=_PJ_VALUE_RANGE, repeated=True),
        protojson.Field("includeValuesInResponse", "include_values_in_response", "bool"),
        protojson.Field(
            "responseValueRenderOption", "response_value_render_option", "enum", enum=_PJ_RENDER
        ),
        protojson.Field(
            "responseDateTimeRenderOption",
            "response_date_time_render_option",
            "enum",
            enum=_PJ_DATETIME,
        ),
    ),
)
_PJ_CLEAR = protojson.Message(f"{_SHEETS_ENUM}.ClearValuesRequest", ())
_PJ_BATCH_CLEAR = protojson.Message(
    f"{_SHEETS_ENUM}.BatchClearValuesRequest",
    (protojson.Field("ranges", "ranges", "string", repeated=True),),
)

_P_SHEETS_UPDATE = [
    qp("valueInputOption"),
    qp("includeValuesInResponse", "boolean"),
    qp("responseValueRenderOption"),
    qp("responseDateTimeRenderOption"),
    *_P_SHEETS_STD,
]


def _sheets_write_readers(*, append: bool = False) -> dict:
    """The typed query values a write reads, refused as the reads' are (`_typed_query`): a bad one
    is a 400 ahead of the lookup, measured, so a reader sending `includeValuesInResponse=bogus`
    gets it rather than its 403. ``append`` adds the `insertDataOption` only `values.append`
    declares."""
    extra = (
        {
            "insertDataOption": lambda raw: _sheets_enum_value(
                raw, "insert_data_option", _PJ_INSERT_DATA
            )
        }
        if append
        else {}
    )
    return {
        **extra,
        "valueInputOption": lambda raw: _sheets_enum_value(
            raw, "value_input_option", _PJ_VALUE_INPUT
        ),
        "includeValuesInResponse": lambda raw: _sheets_bool_value(
            raw, "include_values_in_response"
        ),
        "responseValueRenderOption": lambda raw: _sheets_enum_value(
            raw, "response_value_render_option", _PJ_RENDER
        ),
        "responseDateTimeRenderOption": lambda raw: _sheets_enum_value(
            raw, "response_date_time_render_option", _PJ_DATETIME
        ),
    }


async def _sheets_query_and_body(
    request: Request, readers: dict, message: protojson.Message, *, field: str | None = None
) -> tuple[dict, dict]:
    """``(query, body)`` of a POST or PUT: each typed query parameter in ``readers`` by its last
    repeat (``None`` when absent), and the body read into ``message``.

    A query parameter names a field of the request, and real reads both. Measured 2026-10-27 on
    update, append, batchUpdate, batchUpdateByDataFilter, batchGetByDataFilter and
    getByDataFilter: with both refused, one 400 lists the body's refusals and then the query's --
    ``Invalid value at 'data.values' (…), "x"`` then
    ``Invalid value at 'include_values_in_response' (TYPE_BOOL), "on"`` -- an unknown body name
    among them; with both set, the query's value is the one used (`_sheets_batch_options`)."""
    raw = await request.body()
    try:
        parsed = _typed_query(request, readers)
    except gerr.GoogleError as exc:
        refused = gerr.field_violations(exc)
        try:
            protojson.read(raw, message, field=field)
        except gerr.GoogleError as body_exc:
            if refused and gerr.field_violations(body_exc):
                raise gerr.invalid_field_values(gerr.field_violations(body_exc) + refused) from None
        raise
    query = {name: (values[-1] if values else None) for name, values in parsed.items()}
    return query, protojson.read(raw, message, field=field)


async def _sheets_write_request(
    request: Request, message: protojson.Message, *, field: str | None = None, append: bool = False
) -> tuple[dict, dict]:
    """``(query options, body)`` of a write (`_sheets_write_readers`)."""
    return await _sheets_query_and_body(
        request, _sheets_write_readers(append=append), message, field=field
    )


def _sheets_batch_options(query: dict, body: dict) -> tuple[str, bool, dict]:
    """``(valueInputOption, includeValuesInResponse, render options)`` of a batch write: each from
    the query when it is there and from the body otherwise. Measured 2026-10-27 on batchUpdate:
    `valueInputOption=USER_ENTERED` in the query beside `RAW` in the body read `5%` as 0.05, `RAW`
    beside `USER_ENTERED` kept it as text, and `includeValuesInResponse=false` beside `true` left
    `updatedData` out. A body's enum is read by its number, on both batch writes: `0` is the first
    name, so a `valueInputOption` of `0` is the 400 an absent one gets, and a number outside the
    enum (`3`, `99`) is ``Invalid '<name>'``, checked in the order `valueInputOption`,
    `responseValueRenderOption`, `responseDateTimeRenderOption`."""

    def pick(name: str, field: str, enum: protojson.Enum | None = None):
        if query[name] is not None:
            return query[name]
        value = body.get(field)
        if enum is None or value is None:
            return value
        if not 0 <= value < len(enum.names):
            raise gerr.invalid_argument(f"Invalid '{name}'")
        return enum.names[value]

    option = _sheets_input_option(pick("valueInputOption", "value_input_option", _PJ_VALUE_INPUT))
    include = bool(pick("includeValuesInResponse", "include_values_in_response"))
    opts = {
        "responseValueRenderOption": pick(
            "responseValueRenderOption", "response_value_render_option", _PJ_RENDER
        )
        or _PJ_RENDER.names[0],
        "responseDateTimeRenderOption": pick(
            "responseDateTimeRenderOption", "response_date_time_render_option", _PJ_DATETIME
        )
        or _PJ_DATETIME.names[0],
    }
    return option, include, opts


def _sheets_input_option(name: str | None) -> str:
    """The `valueInputOption` a write runs under. It has no default: absent, and as its zero value
    by name or number, it is 400 ``'valueInputOption' is required but not specified``, measured."""
    if name in (None, _PJ_VALUE_INPUT.names[0]):
        raise gerr.invalid_argument("'valueInputOption' is required but not specified")
    return name


def _sheets_writable(request: Request, spreadsheet_id: str) -> tuple:
    """``(row, sheets)`` for a spreadsheet the caller may write, or the refusal real gives.

    The lookup is the reads' (`_workbook`): a missing id or a Doc is 404, a file nobody shared with
    the caller 403. Of the files the caller can read, only its own may be written (the admin's
    being what its service account created, `routers.google._drive_owned_by`) -- every grant
    here makes a reader (`routers.google._drive_file_capabilities`), and a reader's write is the
    same 403 ``The caller does not have permission``, measured on update, batchUpdate, clear,
    batchClear and append. Both come ahead of a missing `valueInputOption` and of any check on the
    range."""
    row, sheets = _workbook(request, spreadsheet_id)
    if not _drive_owned_by(row["author_email"], _require(request).email):
        raise gerr.permission_denied()
    return row, sheets


class _Writing(NamedTuple):
    """One `ValueRange` resolved against its sheet: where it starts, the bound it may not pass
    (``None`` for a single cell, which is an anchor the values grow from), and its rows."""

    sheet: _Sheet
    r0: int
    c0: int
    bound: tuple[int, int, int, int] | None
    rows: list[list]


def _sheets_writing(spec: str, sheets: list[_Sheet], values: list, major: str, *, prefix: str = ""):
    """Resolve one write, refusing it the way real does. ``prefix`` is how a batch names the
    entry (``Invalid data[1]: ``).

    Measured: a range of one cell is an ANCHOR -- `B2` with two rows of two writes `B2:C3` -- and
    the grid grows to fit what runs past its edge. Any other range, `A7:A7` included, is a BOUND:
    a row or column past it is ``Requested writing within range [Data!A7], but tried writing to
    column [B]``. A bound is the range as sent, so one ending past the grid's edge grows the grid
    as an anchor does (`Y999:AB999` on a 26-column sheet makes it 28 wide) and is named unclamped
    when refused; only a START past the edge, an anchor's or a bound's, is the grid-limits 400. A
    cell that is an object or a list is refused (`_sheets_refuse_unstorable`)."""
    try:
        sheet, body = _a1_sheet(spec, sheets)
        classified = _a1_classify(body) if body else None
        single = classified is not None and len(classified[1]) == 1
        single = single and classified[1][0].row is not None and classified[1][0].col is not None
        r0, c0, r1, c1 = _a1_range(spec, body, sheet, clamp=False)
        rows = _sheets_rows(values, major)
        if not single:
            _sheets_check_bound(sheet, rows, r0, c0, r1, c1)
        _sheets_refuse_unstorable(values, major, r0, c0)
    except gerr.GoogleError as exc:
        if not prefix:
            raise
        raise gerr.invalid_argument(prefix + exc.message) from None
    return _Writing(sheet, r0, c0, None if single else (r0, c0, r1, c1), rows)


def _sheets_check_bound(sheet: _Sheet, rows: list[list], r0: int, c0: int, r1: int, c1: int):
    """Refuse rows that run past a bound (`_sheets_writing`)."""
    name = _a1_name(sheet, r0, c0, r1, c1)
    for i, row in enumerate(rows):
        if i >= r1 - r0:
            raise gerr.invalid_argument(
                f"Requested writing within range [{name}], but tried writing to row [{r0 + i + 1}]"
            )
        if len(row) > c1 - c0:
            raise gerr.invalid_argument(
                f"Requested writing within range [{name}], but tried writing to column "
                f"[{_a1_col_letters(c1)}]"
            )


def _sheets_rows(values: list, major: str) -> list[list]:
    """A body's ``values`` as rows, each inner list a column under ``COLUMNS``."""
    if major != "COLUMNS":
        return [list(row) for row in values]
    height = max((len(col) for col in values), default=0)
    return [[col[i] if i < len(col) else None for col in values] for i in range(height)]


def _sheets_refuse_unstorable(values: list, major: str, r0: int, c0: int) -> None:
    """Refuse a cell that is an object or a list as ``Invalid values[<i>][<j>]: <its proto
    text>``, indexed by the request's own lists: ``i`` is the list's position offset by where the
    write starts on that list's axis -- its row in the sheet, or under ``COLUMNS`` its column --
    and ``j`` the position inside the list. Measured 2026-10-27: at `A!U950`, ``ROWS``
    `[["a"], [["l"]]]` is `values[950][0]` and ``COLUMNS`` `[["a"], ["b"], [{"x": 1}]]` is
    `values[22][0]`."""
    offset = c0 if major == "COLUMNS" else r0
    for i, inner in enumerate(values):
        for j, value in enumerate(inner):
            if isinstance(value, (dict, list)):
                raise gerr.invalid_argument(
                    f"Invalid values[{offset + i}][{j}]: {protojson.value_debug(value)}"
                )


def _sheets_grids(sheets: list[_Sheet]) -> dict[int, list[list]]:
    """Each sheet's cells as a list a write can change in place, by sheet index."""
    return {sh.index: [list(row) for row in sh.grid] for sh in sheets}


def _sheets_put(grid: list[list], r: int, c: int, cell) -> None:
    """Set one cell, growing the stored grid to hold it."""
    while len(grid) <= r:
        grid.append([])
    row = grid[r]
    while len(row) <= c:
        row.append(None)
    row[c] = cell


def _sheets_apply(writing: _Writing, grids: dict, option: str, today) -> set[tuple[int, int]]:
    """Write one resolved range into ``grids`` and return the cells it wrote. A JSON null leaves
    its cell as it was and is not counted; ``""`` is a write, measured."""
    grid = grids[writing.sheet.index]
    written = set()
    for i, row in enumerate(writing.rows):
        for j, value in enumerate(row):
            if value is None:
                continue
            r, c = writing.r0 + i, writing.c0 + j
            current = grid[r][c] if r < len(grid) and c < len(grid[r]) else None
            _sheets_put(grid, r, c, gsheets.apply(current, value, option, today))
            written.add((r, c))
    return written


def _sheets_update_response(spreadsheet_id: str, writing: _Writing, written: set) -> dict:
    """An ``UpdateValuesResponse``. Measured: ``updatedRange`` runs from the range's first cell to
    the last row and column written (a null at either end trims it, `[[null, "x"]]` at `A1:C1` is
    `A1:B1`), and the three counts are the distinct rows, columns and cells written. With nothing
    written it is the first cell alone and the counts are absent."""
    out = {"spreadsheetId": spreadsheet_id}
    sheet = writing.sheet
    if not written:
        out["updatedRange"] = _a1_name(
            sheet, writing.r0, writing.c0, writing.r0 + 1, writing.c0 + 1
        )
        return out
    r1 = max(r for r, _ in written) + 1
    c1 = max(c for _, c in written) + 1
    out["updatedRange"] = _a1_name(sheet, writing.r0, writing.c0, r1, c1)
    out["updatedRows"] = len({r for r, _ in written})
    out["updatedColumns"] = len({c for _, c in written})
    out["updatedCells"] = len(written)
    return out


def _sheets_reloaded(sheets: list[_Sheet], grids: dict) -> list[_Sheet]:
    """The sheets as they read after a write, each grid rectangular again
    (`gsheets.normalise_grid`'s invariant) and as tall as the write made it."""
    out = []
    for sh in sheets:
        grid = grids[sh.index]
        width = max((len(r) for r in grid), default=0)
        out.append(sh._replace(grid=[r + [None] * (width - len(r)) for r in grid]))
    return out


def _sheets_sheet_rows(sheets: list[_Sheet]) -> list[dict]:
    """A workbook's sheets as `gdrive_sheets` rows."""
    return [
        {
            "sheet_index": sh.index,
            "sheet_id": sh.sheet_id,
            "title": sh.title,
            "grid": json.dumps(sh.grid, sort_keys=True),
            "properties": json.dumps(sh.stated(), sort_keys=True),
        }
        for sh in sheets
    ]


def _sheets_save(conn, row, sheets: list[_Sheet]) -> None:
    """Store a written workbook: every sheet, the first one's CSV as the file's `content`, and the
    write's time as its `updated_ts`."""
    store.replace_parts(
        conn,
        "google_drive",
        (row["id"],),
        {"gdrive_sheets": _sheets_sheet_rows(sheets)},
        {"content": gsheets.to_csv(sheets[0].grid), "updated_ts": int(time.time())},
    )


def _sheets_today(settings: dict) -> datetime.date:
    """The date a typed date without a year falls in: measured, `1/15` stored 2026-01-15 when
    written in 2026. Taken in the spreadsheet's time zone, and in UTC for `Etc/Unknown`, which the
    zone database does not name."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    zone = {**gsheets.DEFAULT_PROPERTIES, **settings}["timeZone"]
    try:
        tz = ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        tz = datetime.UTC
    return datetime.datetime.now(tz).date()


def _sheets_updated_data(
    writing: _Writing, spec: str, answer: dict, sheets: list[_Sheet], major, opts
):
    """``updatedData`` beside the ``UpdateValuesResponse`` ``answer``, read back after the write
    under the response render options. Measured: a bound reads back the REQUEST range --
    `[[null, "=1+1"]]` at `A1:D2` answers ``updatedRange`` `A1:B1` beside ``updatedData`` over
    `A1:D2`, cells the write left alone included -- and an anchor its ``updatedRange``: a row of
    ten at `Sheet1!A6` reads back `Sheet1!A6:J6`, as an append's values do."""
    return _sheets_value_range(
        spec if writing.bound else answer["updatedRange"],
        sheets,
        major,
        opts["responseValueRenderOption"] or "FORMATTED_VALUE",
        opts["responseDateTimeRenderOption"] or "SERIAL_NUMBER",
    )


@router.put(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values/{a1_range:path}",
    openapi_extra={"parameters": _P_SHEETS_UPDATE},
)
async def sheets_values_update(spreadsheet_id: str, a1_range: str, request: Request):
    """``spreadsheets.values.update``.

    Refused in real's order, measured: the credential, the typed query values and the body
    (`_sheets_write_request`), the `fields` mask, the lookup and who may write (`_sheets_writable`), then a missing
    `valueInputOption`, then a body `range` that is not the URL's, then the range and its values
    (`_sheets_writing`). The body's `range`, when sent, must be the URL's as typed: `data!a1:b1`
    and `'Data'!A1:B1` against `Data!A1:B1` are each ``Request range[Data!A1:B1] does not match
    value's range[data!a1:b1]``. Where a mismatched `range` and an unparseable URL range fall
    relative to each other was not measured."""
    _sheets_alt(request)
    _require(request)
    opts, body = await _sheets_write_request(request, _PJ_UPDATE, field="data")
    body = body.get("data", {})
    tree, _ = _sheets_mask(request, "sheets.UpdateValuesResponse")
    row, sheets = _sheets_writable(request, spreadsheet_id)
    option = _sheets_input_option(opts["valueInputOption"])
    if "range" in body and body["range"] != a1_range:
        raise gerr.invalid_argument(
            f"Request range[{a1_range}] does not match value's range[{body['range']}]"
        )
    major = _sheets_major(_PJ_DIMENSION.names[body.get("major_dimension", 0)])
    writing = _sheets_writing(a1_range, sheets, body.get("values", []), major)
    grids = _sheets_grids(sheets)
    written = _sheets_apply(
        writing, grids, option, _sheets_today(_sheets_settings(auth.conn(request), spreadsheet_id))
    )
    after = _sheets_reloaded(sheets, grids)
    if written:
        _sheets_save(auth.conn(request), row, after)
    out = _sheets_update_response(spreadsheet_id, writing, written)
    if opts["includeValuesInResponse"]:
        out["updatedData"] = _sheets_updated_data(writing, a1_range, out, after, major, opts)
    return _sheets_respond(request, out, tree)


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values:batchUpdate",
    openapi_extra={"parameters": _P_SHEETS_STD},
)
async def sheets_values_batch_update(spreadsheet_id: str, request: Request):
    """``spreadsheets.values.batchUpdate``: every entry checked before any is written, so one bad
    entry writes nothing, and its refusal names it -- ``Invalid data[1]: Unable to parse range:
    Nope!A1``, ``Invalid data[0]: ValueRange.range is required but not specified``. Measured.

    The entries are written in order, so where two overlap the later wins. The options may come in
    the body or the query (`_sheets_batch_options`). The totals count distinct rows, columns and
    cells per sheet across the whole batch (one cell written twice totals 1), and
    ``totalUpdatedSheets`` every sheet an entry names, one that wrote nothing included. With no
    entries the answer is ``spreadsheetId`` alone."""
    _sheets_alt(request)
    _require(request)
    query, body = await _sheets_write_request(request, _PJ_BATCH_UPDATE)
    tree, _ = _sheets_mask(request, "sheets.BatchUpdateValuesResponse")
    row, sheets = _sheets_writable(request, spreadsheet_id)
    option, include, opts = _sheets_batch_options(query, body)
    entries = body.get("data", [])
    writings = []
    for i, entry in enumerate(entries):
        if not entry.get("range"):
            raise gerr.invalid_argument(
                f"Invalid data[{i}]: ValueRange.range is required but not specified"
            )
        major = _sheets_major(_PJ_DIMENSION.names[entry.get("major_dimension", 0)])
        writings.append(
            (
                _sheets_writing(
                    entry["range"],
                    sheets,
                    entry.get("values", []),
                    major,
                    prefix=f"Invalid data[{i}]: ",
                ),
                entry["range"],
                major,
            )
        )
    out: dict = {"spreadsheetId": spreadsheet_id}
    if not writings:
        return _sheets_respond(request, out, tree)
    grids = _sheets_grids(sheets)
    today = _sheets_today(_sheets_settings(auth.conn(request), spreadsheet_id))
    results = [
        (w, spec, major, _sheets_apply(w, grids, option, today)) for w, spec, major in writings
    ]
    after = _sheets_reloaded(sheets, grids)
    every = {(w.sheet.index, r, c) for w, _, _, written in results for r, c in written}
    if every:
        _sheets_save(auth.conn(request), row, after)
    totals = {
        "totalUpdatedRows": len({(s, r) for s, r, _ in every}),
        "totalUpdatedColumns": len({(s, c) for s, _, c in every}),
        "totalUpdatedCells": len(every),
        "totalUpdatedSheets": len({w.sheet.index for w, _, _, _ in results}),
    }
    out.update({k: v for k, v in totals.items() if v})
    responses = []
    for w, spec, major, written in results:
        answer = _sheets_update_response(spreadsheet_id, w, written)
        if include:
            answer["updatedData"] = _sheets_updated_data(w, spec, answer, after, major, opts)
        responses.append(answer)
    out["responses"] = responses
    return _sheets_respond(request, out, tree)


def _sheets_clearing(spec: str, sheets: list[_Sheet], *, prefix: str = ""):
    """``(sheet, r0, c0, r1, c1)`` a clear covers. Resolved as a read resolves a range -- an end
    past the grid is cut (`Z1:AC2` clears `Z1:AA2`) and a start past it refused -- measured."""
    try:
        sheet, body = _a1_sheet(spec, sheets)
        return (sheet, *_a1_range(spec, body, sheet))
    except gerr.GoogleError as exc:
        if not prefix:
            raise
        raise gerr.invalid_argument(prefix + exc.message) from None


def _sheets_clear(grids: dict, clearing) -> bool:
    """Empty the cells a clear covers, keeping each one's number format, as real does (measured:
    a percent cell cleared and then written a RAW `0.5` displays `50.00%`). Whether anything
    changed."""
    sheet, r0, c0, r1, c1 = clearing
    grid = grids[sheet.index]
    changed = False
    for r in range(r0, min(r1, len(grid))):
        row = grid[r]
        for c in range(c0, min(c1, len(row))):
            cell = row[c]
            if gsheets.cell_value(cell) is not None or gsheets.cell_error(cell):
                row[c] = gsheets.cell(None, number_format=gsheets.cell_format(cell))
                changed = True
    return changed


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values/{a1_range:path}:clear",
    openapi_extra={"parameters": _P_SHEETS_STD},
)
async def sheets_values_clear(spreadsheet_id: str, a1_range: str, request: Request):
    """``spreadsheets.values.clear``: ``{spreadsheetId, clearedRange}``, the range as resolved.
    Its body is an empty message, so any name in it is ``Unknown name "bogus": Cannot find
    field.``, with no location, measured."""
    _sheets_alt(request)
    _require(request)
    protojson.read(await request.body(), _PJ_CLEAR)
    tree, _ = _sheets_mask(request, "sheets.ClearValuesResponse")
    row, sheets = _sheets_writable(request, spreadsheet_id)
    clearing = _sheets_clearing(a1_range, sheets)
    grids = _sheets_grids(sheets)
    if _sheets_clear(grids, clearing):
        _sheets_save(auth.conn(request), row, _sheets_reloaded(sheets, grids))
    sheet, r0, c0, r1, c1 = clearing
    out = {"spreadsheetId": spreadsheet_id, "clearedRange": _a1_name(sheet, r0, c0, r1, c1)}
    return _sheets_respond(request, out, tree)


def _sheets_append_bounds(spec: str, sheets: list[_Sheet]):
    """``(sheet, r0, c0, r1, c1)``: where an append searches for a table, the range cut at the
    grid's edge. A range starting past the edge is not refused, as an update's is: measured, an
    append to `Other!AB1` on a 26-column sheet wrote there. Its search covers nothing."""
    sheet, body = _a1_sheet(spec, sheets)
    try:
        return (sheet, *_a1_range(spec, body, sheet))
    except gerr.GoogleError:
        classified = _a1_classify(body) if body else None
        if classified is None:
            raise
        ends = classified[1]
        r0 = min((e.row for e in ends if e.row is not None), default=0)
        c0 = min((e.col for e in ends if e.col is not None), default=0)
        return sheet, r0, c0, r0, c0


def _sheets_table(grid: list[list], r0: int, c0: int, r1: int, c1: int):
    """The table an append writes below, as inclusive ``(top, left, bottom, right)``, or None.

    Measured: it starts at the last cell in the range holding a value, row by row -- the lowest
    such row, and in it the rightmost -- so a title in `A1`, a blank row and a table at `A3:B5`
    append below `A3:B5`, and in `A40:E49` holding `A40:A41` and `C45` the table is `C45`. It
    grows while a cell holding one sits in the row just above or below it or the column just left
    or right, within its span and past the range alike -- so `A1`, `A2`, `B2` and `C1` make
    `A1:C2`, while `C2` diagonal to `A1:B1` is left out. A cell a write emptied holds no value and
    joins nothing."""

    def filled(r: int, c: int) -> bool:
        return 0 <= r < len(grid) and 0 <= c < len(grid[r]) and gsheets.formatted(grid[r][c]) != ""

    start = next(
        (
            (r, c)
            for r in reversed(range(r0, min(r1, len(grid))))
            for c in reversed(range(c0, c1))
            if filled(r, c)
        ),
        None,
    )
    if start is None:
        return None
    top, bottom, left, right = start[0], start[0], start[1], start[1]
    grown = True
    while grown:
        grown = False
        if any(filled(top - 1, c) for c in range(left, right + 1)):
            top, grown = top - 1, True
        if any(filled(bottom + 1, c) for c in range(left, right + 1)):
            bottom, grown = bottom + 1, True
        if any(filled(r, left - 1) for r in range(top, bottom + 1)):
            left, grown = left - 1, True
        if any(filled(r, right + 1) for r in range(top, bottom + 1)):
            right, grown = right + 1, True
    return top, left, bottom, right


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values/{a1_range:path}:append",
    openapi_extra={"parameters": [qp("insertDataOption"), *_P_SHEETS_UPDATE]},
)
async def sheets_values_append(spreadsheet_id: str, a1_range: str, request: Request):
    """``spreadsheets.values.append``: ``{spreadsheetId, tableRange, updates}``, with ``tableRange``
    absent when the range holds no table (`_sheets_table`).

    Measured: the values go on the first row below the table, from the table's first column
    whatever column the range named; with no table, at the range's first cell. A row is not
    bounded by the range, and the grid grows to hold what runs past it. ``INSERT_ROWS`` inserts
    that many rows there first, every column of them, and the sheet's row count grows by as
    many even far from its edge; ``OVERWRITE``, the default, writes over what is there.
    ``updatedData`` reads back ``updatedRange`` (`_sheets_updated_data`).
    Refused in an update's order (`sheets_values_update`)."""
    _sheets_alt(request)
    _require(request)
    opts, body = await _sheets_write_request(request, _PJ_UPDATE, field="data", append=True)
    body = body.get("data", {})
    tree, _ = _sheets_mask(request, "sheets.AppendValuesResponse")
    row, sheets = _sheets_writable(request, spreadsheet_id)
    option = _sheets_input_option(opts["valueInputOption"])
    if "range" in body and body["range"] != a1_range:
        raise gerr.invalid_argument(
            f"Request range[{a1_range}] does not match value's range[{body['range']}]"
        )
    major = _sheets_major(_PJ_DIMENSION.names[body.get("major_dimension", 0)])
    sheet, r0, c0, r1, c1 = _sheets_append_bounds(a1_range, sheets)
    grids = _sheets_grids(sheets)
    grid = grids[sheet.index]
    table = _sheets_table(grid, r0, c0, r1, c1)
    at_row, at_col = (table[2] + 1, table[1]) if table else (r0, c0)
    rows = _sheets_rows(body.get("values", []), major)
    _sheets_refuse_unstorable(body.get("values", []), major, at_row, at_col)
    inserted = bool(rows) and opts["insertDataOption"] == "INSERT_ROWS"
    if inserted:
        grid[at_row:at_row] = [[] for _ in rows] if at_row < len(grid) else []
        grown = sheet.stated()
        grown["gridProperties"]["rowCount"] = sheet.rows + len(rows)
        sheet = sheet._replace(properties=grown)
        sheets = [sheet if s.index == sheet.index else s for s in sheets]
    writing = _Writing(sheet, at_row, at_col, None, rows)
    written = _sheets_apply(
        writing, grids, option, _sheets_today(_sheets_settings(auth.conn(request), spreadsheet_id))
    )
    after = _sheets_reloaded(sheets, grids)
    if written or inserted:
        _sheets_save(auth.conn(request), row, after)
    out: dict = {"spreadsheetId": spreadsheet_id}
    if table:
        out["tableRange"] = _a1_name(sheet, table[0], table[1], table[2] + 1, table[3] + 1)
    updates = _sheets_update_response(spreadsheet_id, writing, written)
    if opts["includeValuesInResponse"]:
        updates["updatedData"] = _sheets_updated_data(
            writing, a1_range, updates, after, major, opts
        )
    out["updates"] = updates
    return _sheets_respond(request, out, tree)


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values:batchClear",
    openapi_extra={"parameters": _P_SHEETS_STD},
)
async def sheets_values_batch_clear(spreadsheet_id: str, request: Request):
    """``spreadsheets.values.batchClear``: ``clearedRanges`` in the order sent, each as resolved,
    and nothing cleared when one range is refused -- ``Invalid range[1]: Unable to parse range:
    Nope!A1``. With no ranges the answer is ``spreadsheetId`` alone. Measured."""
    _sheets_alt(request)
    _require(request)
    body = protojson.read(await request.body(), _PJ_BATCH_CLEAR)
    tree, _ = _sheets_mask(request, "sheets.BatchClearValuesResponse")
    row, sheets = _sheets_writable(request, spreadsheet_id)
    clearings = [
        _sheets_clearing(spec, sheets, prefix=f"Invalid range[{i}]: ")
        for i, spec in enumerate(body.get("ranges", []))
    ]
    out: dict = {"spreadsheetId": spreadsheet_id}
    if not clearings:
        return _sheets_respond(request, out, tree)
    grids = _sheets_grids(sheets)
    changed = [_sheets_clear(grids, c) for c in clearings]
    if any(changed):
        _sheets_save(auth.conn(request), row, _sheets_reloaded(sheets, grids))
    out["clearedRanges"] = [_a1_name(*c) for c in clearings]
    return _sheets_respond(request, out, tree)


# --- request bodies read from the recorded Sheets messages --------------------------------------
#
# `spreadsheets.create` takes a whole `Spreadsheet`, a tree of a hundred and more messages. Each is
# read with a `protojson.Message` generated from the message the `google_drive` baseline records
# from the discovery document (`backlot diff` keeps it current), so a name the vendor's message
# lacks is refused where real refuses it and every name it has is read. The baseline records an
# enum as `string`; the enums this module acts on are declared here, with the discovery
# document's values in number order.

_SHEETS_RECORDED = json.loads((_BASELINES / "google_drive.json").read_text(encoding="utf-8"))[
    "types"
]
_PJ_ENUM_FIELDS = {
    ("sheets.SheetProperties", "sheetType"): _pj_enum(
        "SheetType", "SHEET_TYPE_UNSPECIFIED", "GRID", "OBJECT", "DATA_SOURCE"
    ),
    ("sheets.SpreadsheetProperties", "autoRecalc"): _pj_enum(
        "RecalculationInterval", "RECALCULATION_INTERVAL_UNSPECIFIED", "ON_CHANGE", "MINUTE", "HOUR"
    ),
    ("sheets.ErrorValue", "type"): _pj_enum(
        "ErrorType",
        "ERROR_TYPE_UNSPECIFIED",
        "ERROR",
        "NULL_VALUE",
        "DIVIDE_BY_ZERO",
        "VALUE",
        "REF",
        "NAME",
        "NUM",
        "N_A",
        "LOADING",
    ),
    ("sheets.NumberFormat", "type"): _pj_enum(
        "NumberFormatType",
        "NUMBER_FORMAT_TYPE_UNSPECIFIED",
        "TEXT",
        "NUMBER",
        "PERCENT",
        "CURRENCY",
        "DATE",
        "TIME",
        "DATE_TIME",
        "SCIENTIFIC",
    ),
}
_PJ_SCALAR_KINDS = {"integer": "int32", "number": "double", "boolean": "bool", "string": "string"}


@functools.cache
def _pj_message(name: str) -> protojson.Message:
    """The recorded message ``name`` (`sheets.SheetProperties`) as a `protojson.Message`."""
    fields = []
    for json_name, member in _SHEETS_RECORDED[name].get("fields", {}).items():
        type_ = member.get("type", "any")
        repeated = type_.startswith("[") and type_ != "[[any]]"
        inner = type_[1:-1] if repeated else type_
        proto = re.sub(r"(?<!^)(?=[A-Z])", "_", json_name).lower()
        enum = _PJ_ENUM_FIELDS.get((name, json_name))
        if enum is not None:
            fields.append(protojson.Field(json_name, proto, "enum", enum=enum, repeated=repeated))
        elif inner in _SHEETS_RECORDED:
            fields.append(
                protojson.Field(
                    json_name, proto, "message", message=_pj_message(inner), repeated=repeated
                )
            )
        elif type_ == "[[any]]":
            fields.append(protojson.Field(json_name, proto, "rows"))
        elif inner in _PJ_SCALAR_KINDS:
            fields.append(
                protojson.Field(json_name, proto, _PJ_SCALAR_KINDS[inner], repeated=repeated)
            )
        else:  # a map or a bare JSON value: kept as sent
            fields.append(protojson.Field(json_name, proto, "any"))
    return protojson.Message(f"{_SHEETS_ENUM}.{name.split('.', 1)[1]}", tuple(fields))


# --- `spreadsheets.create` -----------------------------------------------------------------------

# A new spreadsheet's title when the request names none. It follows the ACCOUNT's language, not the
# request's `locale`: measured 2026-10-26, a Korean account got `제목 없는 스프레드시트` under
# `locale: en_US`, and an English one this under `locale: ko_KR`. Backlot's callers are English
# accounts.
SHEETS_UNTITLED = "Untitled spreadsheet"

# The refusals real words in the account's language, each as an English account got it, measured
# 2026-10-26.
_SHEETS_TITLE_TAKEN = 'A sheet with the name "{title}" already exists. Please enter another name.'
_SHEETS_TITLE_TOO_LONG = "The sheet name cannot be greater than 100 characters."
_SHEETS_INDEX_TOO_HIGH = "The new sheet index is too high."
_SHEETS_ALL_HIDDEN = "You can't hide all the sheets in a document."
_SHEETS_NO_COLUMNS = "You can't delete all the columns on the sheet."
_SHEETS_NO_ROWS = "You can't delete all the rows on the sheet."

_PJ_CREATE = protojson.Message(
    f"{_SHEETS_ENUM}.CreateSpreadsheetRequest",
    (
        protojson.Field(
            "spreadsheet", "spreadsheet", "message", message=_pj_message("sheets.Spreadsheet")
        ),
    ),
)


def _sheets_cell(cell: dict):
    """A `CellData` a request states, as a stored cell: its `userEnteredValue`, and its
    `userEnteredFormat.numberFormat` as stated -- a type with no pattern included, which real keeps
    that way and displays in the type's default (`gsheets.render`). A formula is kept as a
    written one is (`gsheets.apply`); an `errorValue` is refused by the caller."""
    value = cell.get("user_entered_value") or {}
    fmt = (cell.get("user_entered_format") or {}).get("number_format") or {}
    kind = _PJ_ENUM_FIELDS[("sheets.NumberFormat", "type")].names[fmt.get("type", 0)]
    number_format = None
    if fmt and kind != "NUMBER_FORMAT_TYPE_UNSPECIFIED":
        number_format = {
            "type": kind,
            **({"pattern": fmt["pattern"]} if fmt.get("pattern") else {}),
        }
    for key in ("string_value", "formula_value"):
        if key in value:
            text = value[key]
            return (
                gsheets.cell(text, number_format=number_format)
                if text
                else dict(gsheets.EMPTY_STRING)
            )
    if "number_value" in value:
        return gsheets.cell(gsheets.collapse(value["number_value"]), number_format=number_format)
    if "bool_value" in value:
        return gsheets.cell(value["bool_value"], number_format=number_format)
    return gsheets.cell(None, number_format=number_format) if number_format else None


def _sheets_stated_sheet(i: int, sheet: dict, book: gsheets.Workbook) -> _Sheet:
    """One `Sheet` of a create request, checked as real checks it, as the sheet it makes.

    In real's order, measured 2026-10-26 by sending two refusals at once: the id (negative, then
    taken) ahead of the title (too long, then taken), and the row count ahead of the column
    count."""
    where = f"Invalid sheets[{i}]"
    props = sheet.get("properties") or {}
    kind = _PJ_ENUM_FIELDS[("sheets.SheetProperties", "sheetType")].names[
        props.get("sheet_type", 0)
    ]
    if kind not in ("SHEET_TYPE_UNSPECIFIED", "GRID"):
        raise gerr.invalid_argument("Object sheets must have exactly one embedded object")
    title = props.get("title") or book.default_title()
    if "sheet_id" in props:
        if props["sheet_id"] < 0:
            raise gerr.invalid_argument(f"{where}: Sheet id must be non-negative.")
        if book.sheet_by_id(props["sheet_id"]) is not None:
            raise gerr.invalid_argument(
                f"{where}.id: Duplicate ids are not allowed. Multiple sheets are assigned to id "
                f"{props['sheet_id']}"
            )
        sheet_id = props["sheet_id"]
    else:
        sheet_id = book.new_sheet_id(title)
    if len(title) > 100:
        raise gerr.invalid_argument(f"{where}: {_SHEETS_TITLE_TOO_LONG}")
    if book.title_taken(title):
        raise gerr.invalid_argument(f"{where}: {_SHEETS_TITLE_TAKEN.format(title=title)}")
    grid = props.get("grid_properties") or {}
    if grid.get("row_count", 0) < 0:
        raise gerr.invalid_argument(f"{where}: {_SHEETS_NO_ROWS}")
    if grid.get("column_count", 0) < 0:
        raise gerr.invalid_argument(f"{where}: {_SHEETS_NO_COLUMNS}")
    stated: dict = {
        "gridProperties": {
            "rowCount": grid.get("row_count") or gsheets.GRID_ROWS,
            "columnCount": grid.get("column_count") or gsheets.GRID_COLS,
            **{
                json_name: grid[proto]
                for json_name, proto in (
                    ("frozenRowCount", "frozen_row_count"),
                    ("frozenColumnCount", "frozen_column_count"),
                    ("hideGridlines", "hide_gridlines"),
                    ("rowGroupControlAfter", "row_group_control_after"),
                    ("columnGroupControlAfter", "column_group_control_after"),
                )
                if grid.get(proto)
            },
        }
    }
    if props.get("hidden"):
        stated["hidden"] = True
    if props.get("tab_color_style") or props.get("tab_color"):
        stated.update(_sheets_tab_color(props))
    if props.get("right_to_left"):
        stated["rightToLeft"] = True
    made = _Sheet(sheet_id, len(book.sheets), title, [], stated)
    cells: list[list] = []
    for d, block in enumerate(sheet.get("data") or []):
        r0, c0 = block.get("start_row", 0), block.get("start_column", 0)
        # measured 2026-10-27: a block starting past the grid names the grid's coordinate, and one
        # starting inside it and running past says what it attempted
        outside = r0 >= made.rows or c0 >= made.cols
        for k, row in enumerate(block.get("row_data") or []):
            for j, cell in enumerate(row.get("values") or []):
                r, c = r0 + k, c0 + j
                for at, size, axis in ((r, made.rows, "row"), (c, made.cols, "column")):
                    if at < size:
                        continue
                    if outside:
                        raise gerr.invalid_argument(
                            f"{where}.data[{d}]: GridCoordinate.{axis}Index[{at}] is after last "
                            f"{axis} in grid[{size - 1}]"
                        )
                    raise gerr.invalid_argument(
                        f"{where}.data[{d}]: Attempting to write {axis}: {at}, beyond the last "
                        f"requested {axis} of: {size - 1}"
                    )
                if "error_value" in (cell.get("user_entered_value") or {}):
                    # measured 2026-10-26: a cell is not created holding an error
                    raise gerr.invalid_argument(
                        f"{where}.data[{d}]: Cannot set cell to an error value"
                    )
                stored = _sheets_cell(cell)
                if stored is not None:
                    _sheets_put(cells, r, c, stored)
    width = max((len(r) for r in cells), default=0)
    return made._replace(grid=[r + [None] * (width - len(r)) for r in cells])


def _float32(x: float) -> float:
    """``x`` as the shortest decimal that reads back as the same float32, as proto3 JSON prints a
    `float`: 127/255 is `0.49803922`."""
    f = struct.unpack("f", struct.pack("f", x))[0]
    return next(
        float(text)
        for text in (f"{f:.{p}g}" for p in range(1, 10))
        if struct.unpack("f", struct.pack("f", float(text)))[0] == f
    )


def _sheets_rgb(color: dict) -> dict:
    """A stated `Color` as a sheet keeps it: each component a byte, 255 times the float in
    float32 arithmetic truncated toward zero and taken modulo 256, served as that byte over 255 --
    `0.5` is `0.49803922`, `1.5` is 126/255, `-0.2` is 205/255, `0.002` is gone -- with no
    `alpha`. Measured 2026-10-27 on fourteen tab colours."""
    out = {}
    for name in ("red", "green", "blue"):
        scaled = _float32(_float32(color.get(name, 0.0)) * 255)
        byte = int(scaled) % 256
        if byte:
            out[name] = gsheets.collapse(_float32(byte / 255))
    return out


_THEME_COLORS = {c["colorType"]: c["color"]["rgbColor"] for c in SHEETS_THEME["themeColors"]}


def _sheets_tab_color(props: dict) -> dict:
    """The `tabColor` and `tabColorStyle` a stated sheet keeps. Measured 2026-10-27: a
    `tabColorStyle` wins over a `tabColor` beside it, a colour is kept as `_sheets_rgb` keeps it,
    a theme colour keeps its `themeColor` and serves the theme's colour as `tabColor`, and a colour
    with every component zero serves no `tabColor` beside a `tabColorStyle` of `{"rgbColor": {}}`."""
    style = props.get("tab_color_style") or {}
    if "theme_color" in style:
        theme = style["theme_color"]
        name = theme if isinstance(theme, str) else [None, *_THEME_COLORS][theme]
        rgb, kept = _THEME_COLORS.get(name, {}), {"themeColor": name}
    else:
        rgb = _sheets_rgb(style.get("rgb_color") or props.get("tab_color") or {})
        kept = {"rgbColor": rgb}
    return {**({"tabColor": rgb} if rgb else {}), "tabColorStyle": kept}


def _sheets_json(message: dict) -> dict:
    """A message `protojson` read, back as the JSON real echoes: camelCase names, and a whole
    double without its decimal point (`"red": 1`), as proto3 JSON prints one."""
    return {
        re.sub(r"_([a-z])", lambda m: m.group(1).upper(), k): (
            _sheets_json(v) if isinstance(v, dict) else gsheets.collapse(v)
        )
        for k, v in message.items()
    }


def _sheets_creator(request: Request, caller: Caller) -> str:
    """Who a created file belongs to: the caller, or for the admin/service token the service
    account, which on real owns what it creates."""
    return caller.email or _service_email(request)


def _sheets_new_file_id(conn, owner: str) -> str:
    """A Drive file id no stored file has, minted as the importer mints one
    (`synth.gdrive_file_id`) from a seed only this creation has."""
    while True:
        file_id = synth.gdrive_file_id(f"created:{owner}:{time.time_ns()}")
        if store.gdrive_by_id(conn, file_id) is None:
            return file_id


@router.post("/sheets/v4/spreadsheets", openapi_extra={"parameters": _P_SHEETS_STD})
async def sheets_create(request: Request):
    """``spreadsheets.create``: a new workbook at the caller's My Drive root, owned by the caller
    and shared with nobody, answered as `spreadsheets.get` answers it without grid data.

    Measured 2026-10-26:

    * with no ``sheets`` the one sheet is ``sheetId 0``, and with ``sheets`` each gets an id of
      its own unless it states one, a later sheet stating 0 included;
    * a sheet with no title is named `Sheet1` in the workbook's language
      (`gsheets.Workbook.default_title`), and titles collide without regard to case;
    * ``rowCount: 0`` is no row count, and the grid is 1000 x 26;
    * ``data`` is stored though the answer carries no grid;
    * a ``locale`` not spelled as one of :data:`gsheets.LOCALES` is refused, and a ``timeZone``
      is served by its CLDR name (`gsheets.time_zone`), one that names no zone as `Etc/GMT`;
    * the Drive file's ``parents`` is the caller's root, and its one permission the owner's.

    What a request states and this does not keep is listed in `docs/write-overlay.md`."""
    _sheets_alt(request)
    caller = _require(request)
    body = protojson.read(await request.body(), _PJ_CREATE, field="spreadsheet").get(
        "spreadsheet", {}
    )
    tree, _ = _sheets_mask(request, "sheets.Spreadsheet")
    if _BATCH_OUTER.get() is not None:
        raise gerr.unimplemented()
    conn = auth.conn(request)
    owner = _sheets_creator(request, caller)
    file_id = _sheets_new_file_id(conn, owner)
    stated = body.get("properties") or {}
    settings = dict(gsheets.DEFAULT_PROPERTIES)
    if stated.get("locale"):
        if stated["locale"] not in gsheets.LOCALES:
            raise gerr.invalid_argument(
                f"Invalid properties: Unsupported locale: {stated['locale']}"
            )
        settings["locale"] = stated["locale"]
    if stated.get("time_zone"):
        settings["timeZone"] = gsheets.time_zone(stated["time_zone"])
    if stated.get("auto_recalc"):
        settings["autoRecalc"] = _PJ_ENUM_FIELDS[
            ("sheets.SpreadsheetProperties", "autoRecalc")
        ].names[stated["auto_recalc"]]
    # measured: the properties are refused ahead of a stated `spreadsheetId`
    if body.get("spreadsheet_id"):
        raise gerr.invalid_argument("Invalid spreadsheet.spreadsheetId: The id is invalid.")
    book = gsheets.Workbook(file_id, [], settings)
    requested = body.get("sheets") or []
    misplaced = False
    for i, sheet in enumerate(requested):
        made = _sheets_stated_sheet(i, sheet, book)
        index = (sheet.get("properties") or {}).get("index")
        if index is not None and index > len(book.sheets):
            misplaced, index = True, None
        book.add_sheet(made, index)
    if not requested:
        book.add_sheet(_Sheet(0, 0, book.default_title(), [], {}))
    # measured: every sheet hidden is refused ahead of an index past the end
    if all(s.properties.get("hidden") for s in book.sheets):
        raise gerr.invalid_argument(f"Invalid sheets[0]: {_SHEETS_ALL_HIDDEN}")
    if misplaced:
        raise gerr.invalid_argument(f"Invalid sheets: {_SHEETS_INDEX_TOO_HIGH}")
    title = stated.get("title") or SHEETS_UNTITLED
    now = int(time.time())
    store.insert_document(
        conn,
        "google_drive",
        {
            "id": file_id,
            "folder": _DRIVE_ROOT_FOLDER,
            "author_email": owner,
            "title": title,
            "content": gsheets.to_csv(book.sheets[0].grid),
            "subtype": "spreadsheet",
            "parents": json.dumps([synth.drive_root_id(owner)]),
            "created_ts": now,
            "updated_ts": now,
            "trashed": 0,
            "title_fold": store.drive_name_fold(title),
        },
        grants=[("user", owner)],
        parts={
            "gdrive_sheets": _sheets_sheet_rows(book.sheets),
            "gdrive_spreadsheets": [{"properties": json.dumps(settings, sort_keys=True)}],
        },
    )
    row = store.gdrive_by_id(conn, file_id)
    out = _sheets_book(
        file_id,
        row,
        book.sheets,
        [],
        False,
        _PJ_COMMENTS_VIEW.names[0],
        tree,
        settings=settings,
        url=_sheets_url(file_id, caller),
    )
    return _sheets_respond(request, out, tree)


# --- `sheets.copyTo` ----------------------------------------------------------------------------

_PJ_COPY_TO = protojson.Message(
    f"{_SHEETS_ENUM}.CopySheetToAnotherSpreadsheetRequest",
    (protojson.Field("destinationSpreadsheetId", "destination_spreadsheet_id", "string"),),
)


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/sheets/{sheet_id}:copyTo",
    openapi_extra={"parameters": _P_SHEETS_STD},
)
async def sheets_copy_to(spreadsheet_id: str, sheet_id: str, request: Request):
    """``spreadsheets.sheets.copyTo``: the copy's `SheetProperties`.

    Measured 2026-10-26: reading the source is enough, while the destination has to be the
    caller's to write -- one the caller cannot write or cannot see is 403, and one that does not
    exist is 400 ``Invalid destinationSpreadsheetId [<id>]``. The copy goes last, with an id of
    its own, the source's properties and cells, and a title in the DESTINATION's language
    (`gsheets.Workbook.copy_title`). Copying into the same workbook is allowed."""
    _sheets_alt(request)
    _require(request)
    body = protojson.read(await request.body(), _PJ_COPY_TO)
    tree, _ = _sheets_mask(request, "sheets.SheetProperties")
    try:
        wanted = protojson.to_int32(("string", sheet_id))
    except protojson.ConversionError:
        raise gerr.invalid_field_value(
            "sheet_id", f"Invalid value at 'sheet_id' (TYPE_INT32), \"{sheet_id}\""
        ) from None
    _row, sheets = _workbook(request, spreadsheet_id)
    source = next((s for s in sheets if s.sheet_id == wanted), None)
    if source is None:
        raise gerr.invalid_argument(f"The sheet ({wanted}) does not exist.")
    dest_id = body.get("destination_spreadsheet_id", "")
    conn = auth.conn(request)
    dest_row = store.gdrive_by_id(conn, dest_id) if dest_id else None
    if dest_row is None or (dest_row["subtype"] or "document") != "spreadsheet":
        raise gerr.invalid_argument(f"Invalid destinationSpreadsheetId [{dest_id}]")
    dest_row, dest_sheets = _sheets_writable(request, dest_id)
    book = gsheets.Workbook(dest_id, dest_sheets, _sheets_settings(conn, dest_id))
    title = book.copy_title(source.title)
    copied = _Sheet(
        book.new_sheet_id(title),
        len(book.sheets),
        title,
        [list(r) for r in source.grid],
        source.stated(),
    )
    placed = book.add_sheet(copied)
    _sheets_save(conn, dest_row, book.sheets)
    return _sheets_respond(request, placed.served(), tree)


# --- the two writes addressed by DataFilter -------------------------------------------------------

_PJ_DATA_FILTER_VALUE_RANGE = protojson.Message(
    f"{_SHEETS_ENUM}.DataFilterValueRange",
    (
        protojson.Field("dataFilter", "data_filter", "message", message=_PJ_DATA_FILTER),
        protojson.Field("majorDimension", "major_dimension", "enum", enum=_PJ_DIMENSION),
        protojson.Field("values", "values", "rows"),
    ),
)
_PJ_BATCH_UPDATE_BY_FILTER = protojson.Message(
    f"{_SHEETS_ENUM}.BatchUpdateValuesByDataFilterRequest",
    (
        protojson.Field("valueInputOption", "value_input_option", "enum", enum=_PJ_VALUE_INPUT),
        protojson.Field(
            "data", "data", "message", message=_PJ_DATA_FILTER_VALUE_RANGE, repeated=True
        ),
        protojson.Field("includeValuesInResponse", "include_values_in_response", "bool"),
        protojson.Field(
            "responseValueRenderOption", "response_value_render_option", "enum", enum=_PJ_RENDER
        ),
        protojson.Field(
            "responseDateTimeRenderOption",
            "response_date_time_render_option",
            "enum",
            enum=_PJ_DATETIME,
        ),
    ),
)
_PJ_BATCH_CLEAR_BY_FILTER = protojson.Message(
    f"{_SHEETS_ENUM}.BatchClearValuesByDataFilterRequest", (_PJ_FILTERS,)
)


def _a1_absolute(sheet: _Sheet, r0: int, c0: int, r1: int, c1: int) -> str:
    """A range with each reference absolute, the way a DataFilter write's refusal names what its
    filter matched: `Other!$A$7`, `Other!$A$10:$B$10`."""
    start = f"${_a1_col_letters(c0)}${r0 + 1}"
    if r1 - r0 == 1 and c1 - c0 == 1:
        return f"{_a1_title(sheet.title)}!{start}"
    return f"{_a1_title(sheet.title)}!{start}:${_a1_col_letters(c1 - 1)}${r1}"


# How a DataFilter write's fit refusal names its `majorDimension`: the dimension the lists run
# along, then the one inside a list. Measured 2026-10-27 (`_sheets_filter_fit`).
_FIT_WORDS = {
    "DIMENSION_UNSPECIFIED": ("dimension_unspecified", "rows"),
    "ROWS": ("rows", "columns"),
    "COLUMNS": ("columns", "rows"),
}


def _sheets_filter_fit(values: list, named: str, height: int, width: int, matched: str) -> None:
    """Refuse values a DataFilter's match cannot hold, before they are written.

    Measured 2026-10-27 over 1x1, 1x2, 2x1, 2x2 and empty matches with eight shapes of values
    under each `majorDimension`: more lists than the match has rows -- columns under ``COLUMNS``
    -- is ``…matched range Other!$Z$957 whose rows cannot fit the data described in
    data.values.``, and a list longer than the other side is the same naming that list,
    ``data.values[1]``, in the words :data:`_FIT_WORDS` gives. An unspecified dimension is
    checked as ``COLUMNS`` is, and then written by rows against the match as a bound
    (`_sheets_check_bound`), so `[["x"], ["y"]]` into `Y957:Z957` passes here and is
    ``Requested writing within range [Other!Y957:Z957], but tried writing to row [958]``."""
    lists, across = (height, width) if named == "ROWS" else (width, height)
    outer, inner = _FIT_WORDS[named]
    lead = f"dataFilterValueRange.dataFilter matched range {matched} whose"
    if len(values) > lists:
        raise gerr.invalid_argument(f"{lead} {outer} cannot fit the data described in data.values.")
    for i, cells in enumerate(values):
        if len(cells) > across:
            raise gerr.invalid_argument(
                f"{lead} {inner} cannot fit the data described in data.values[{i}]."
            )


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values:batchUpdateByDataFilter",
    openapi_extra={"parameters": _P_SHEETS_STD},
)
async def sheets_values_batch_update_by_data_filter(spreadsheet_id: str, request: Request):
    """``spreadsheets.values.batchUpdateByDataFilter``: `values:batchUpdate` with each range named
    by a DataFilter.

    Measured 2026-10-26: what a filter matches is always a bound, a single cell included, and
    values past it are refused (`_sheets_filter_fit`). An empty grid range is refused as
    ``Unable to parse range: #REF!`` once its values fit (2026-10-27), so no entry of that batch
    is written. A developer metadata lookup matches nothing and leaves no entry. Each entry echoes
    its filter and carries no ``spreadsheetId``, and a refusal names the entry as
    ``Invalid dataFilterValueRange[<n>]: ``."""
    _sheets_alt(request)
    _require(request)
    query, body = await _sheets_write_request(request, _PJ_BATCH_UPDATE_BY_FILTER)
    tree, _ = _sheets_mask(request, "sheets.BatchUpdateValuesByDataFilterResponse")
    row, sheets = _sheets_writable(request, spreadsheet_id)
    option, include, opts = _sheets_batch_options(query, body)
    writings = []
    for i, entry in enumerate(body.get("data", [])):
        where = f"Invalid dataFilterValueRange[{i}]: "
        if "data_filter" not in entry:
            raise gerr.invalid_argument(
                f"{where}dataFilterValueRange.filter is required but not specified."
            )
        (selection,) = _sheets_selections(
            [entry["data_filter"]], sheets, values_level=True, prefix=where
        )
        if selection is None:
            continue
        sheet, r0, c0, r1, c1 = _sheets_bounds(selection, sheets)
        empty = isinstance(selection, _Empty)
        named = _PJ_DIMENSION.names[entry.get("major_dimension", 0)]
        values = entry.get("values", [])
        matched = "#REF!" if empty else _a1_absolute(sheet, r0, c0, r1, c1)
        try:
            _sheets_filter_fit(values, named, r1 - r0, c1 - c0, matched)
            if empty:
                raise gerr.invalid_argument("Unable to parse range: #REF!")
            major = _sheets_major(named)
            rows = _sheets_rows(values, major)
            _sheets_check_bound(sheet, rows, r0, c0, r1, c1)
            _sheets_refuse_unstorable(values, major, r0, c0)
        except gerr.GoogleError as exc:
            raise gerr.invalid_argument(where + exc.message) from None
        name = _a1_name(sheet, r0, c0, r1, c1)
        writings.append((_Writing(sheet, r0, c0, (r0, c0, r1, c1), rows), name, major, entry))
    out: dict = {"spreadsheetId": spreadsheet_id}
    if not writings:
        return _sheets_respond(request, out, tree)
    grids = _sheets_grids(sheets)
    today = _sheets_today(_sheets_settings(auth.conn(request), spreadsheet_id))
    results = [(w, n, m, e, _sheets_apply(w, grids, option, today)) for w, n, m, e in writings]
    after = _sheets_reloaded(sheets, grids)
    every = {(w.sheet.index, r, c) for w, _, _, _, done in results for r, c in done}
    if every:
        _sheets_save(auth.conn(request), row, after)
    totals = {
        "totalUpdatedRows": len({(s, r) for s, r, _ in every}),
        "totalUpdatedColumns": len({(s, c) for s, _, c in every}),
        "totalUpdatedCells": len(every),
        "totalUpdatedSheets": len({w.sheet.index for w, _, _, _, _ in results}),
    }
    out.update({k: v for k, v in totals.items() if v})
    responses = []
    for w, name, major, entry, done in results:
        answer = _sheets_update_response(spreadsheet_id, w, done)
        del answer["spreadsheetId"]
        answer["dataFilter"] = _sheets_filter_echo(entry["data_filter"])
        if include:
            answer["updatedData"] = _sheets_updated_data(w, name, answer, after, major, opts)
        responses.append(answer)
    out["responses"] = responses
    return _sheets_respond(request, out, tree)


@router.post(
    "/sheets/v4/spreadsheets/{spreadsheet_id}/values:batchClearByDataFilter",
    openapi_extra={"parameters": _P_SHEETS_STD},
)
async def sheets_values_batch_clear_by_data_filter(spreadsheet_id: str, request: Request):
    """``spreadsheets.values.batchClearByDataFilter``: ``clearedRanges`` for what the filters
    match, in order. Measured 2026-10-26: a developer metadata lookup clears nothing and names
    nothing, an open grid range clears the whole sheet, and a refusal names the filter as
    ``Invalid dataFilter[<n>]: ``. An empty grid range clears nothing and is named in its place
    as ``(empty) A!Z950:Z949``, measured 2026-10-27."""
    _sheets_alt(request)
    _require(request)
    body = protojson.read(await request.body(), _PJ_BATCH_CLEAR_BY_FILTER)
    tree, _ = _sheets_mask(request, "sheets.BatchClearValuesByDataFilterResponse")
    row, sheets = _sheets_writable(request, spreadsheet_id)
    selections = _sheets_selections(body.get("data_filters", []), sheets, values_level=True)
    matched = [s for s in selections if s is not None]
    out: dict = {"spreadsheetId": spreadsheet_id}
    if not matched:
        return _sheets_respond(request, out, tree)
    clearings = [_sheets_bounds(s, sheets) for s in matched if not isinstance(s, _Empty)]
    grids = _sheets_grids(sheets)
    if any([_sheets_clear(grids, c) for c in clearings]):
        _sheets_save(auth.conn(request), row, _sheets_reloaded(sheets, grids))
    out["clearedRanges"] = [
        s.name if isinstance(s, _Empty) else _a1_name(*_sheets_bounds(s, sheets)) for s in matched
    ]
    return _sheets_respond(request, out, tree)
