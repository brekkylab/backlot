"""Google Docs v1 (``/docs/v1``): ``documents.get``, ``documents.create`` and
``documents.batchUpdate``.

The routes are registered on `routers.google.router` as `routers.google_sheets` describes, and a
``fields`` mask is read as the Sheets routes read theirs (`routers.google._gmask_select`).

The answers below were measured on docs.googleapis.com between 2026-10-09 and 2026-11-09, each
comment dating its own, by a Google Workspace member on Docs it owns and by a second member it was
shared with as a reader.
"""

from __future__ import annotations

import base64
import binascii
import copy
import functools
import json
import math
import re
import struct
import time
from pathlib import Path

from fastapi import Request

from backlot import auth, gdocs, protojson, store
from backlot.errors import google as gerr
from backlot.errors.atlassian import strip_java_whitespace
from backlot.openapi import proto_body, qp
from backlot.routers.google import (
    _BATCH_OUTER,
    _GOOGLE_FIELDS,
    _GOOGLE_RECORDED,
    _drive_owned_by,
    _drive_version,
    _editor_batch,
    _editor_creator,
    _editor_doc,
    _editor_new_file_id,
    _editor_requests,
    _editor_store,
    _editor_writable,
    _EditorRefused,
    _gmask_apply,
    _gmask_select,
    _pj_messages,
    _query_and_body,
    _query_bool,
    _query_enum,
    _require,
    _typed_query,
    router,
)

# `documents.get`'s two enums, names in number order. Measured 2026-10-09: `suggestionsViewMode`
# reads `0` through `3`, `+1` and `01` (the value numbered 1) and a name in any ASCII case, and
# refuses `4`, `-1`, `1.0`, a padded `" 1"`, empty and `NOPE`. `commentsViewMode` reads the same
# way (2026-10-14: `3`, `+1` and `comments_view_mode_omitted` read, `4` and `-1` refused).
# `includeTabsContent` is the boolean `_query_bool` reads (2026-10-14: `no`, `n`, `y`, `T`
# and `F` read, `on`, `off`, `2` and `tru` refused). A value that cannot be read is a 400 before
# the document is looked up.
_DOCS_VIEW = "type.googleapis.com/google.apps.docs.v1"

# What the baseline cannot say of the `Document` and the messages it holds, measured 2026-10-29 by
# sending `documents.create` a value of the wrong kind in every field of each of the 125 messages
# and reading the type each refusal named: each enum's type, with the discovery document's names in
# number order; a field held in a `StringValue`, `BoolValue`, `FloatValue` or `Int32Value`, its
# refusal at `….value`; a `float`; a `timestamp`; and the members of each oneof, refused as a
# second member is.
_DOCS_PROTO = json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "gdocs_proto.json").read_text(
        encoding="utf-8"
    )
)


def _docs_enum(name: str) -> protojson.Enum:
    return protojson.Enum(f"{_DOCS_VIEW}.{name}", tuple(_DOCS_PROTO["enums"][name]))


_DOCS_SUGGESTIONS_VIEW = _docs_enum("SuggestionsViewMode")
_DOCS_COMMENTS_VIEW = _docs_enum("CommentsViewMode")
_DOCS_PREVIEWS = frozenset(_DOCS_SUGGESTIONS_VIEW.names[2:])

# The members a tab carries as well as the document, which a mask may not name beside the tabs:
# measured 2026-10-26, each of the twelve was the 400 below under `includeTabsContent=true`.
_DOCS_LEGACY = frozenset(_GOOGLE_FIELDS["docs.Document"]) & frozenset(
    _GOOGLE_FIELDS["docs.DocumentTab"]
)

_P_DOCS_GET = [
    qp("suggestionsViewMode"),
    qp("includeTabsContent", "boolean"),
    qp("commentsViewMode"),
    qp("fields"),
]


def _docs_comment_fields(named: dict) -> bool:
    """Whether a mask names a comment-specific field: measured 2026-10-26, `comments`,
    `suggestions`, `suggestions.*`, `*.commentId`, `tabs.documentTab.commentAnchors`,
    `tabs.*.commentAnchors` and `*.*.commentAnchors` were refused for it, and `*.*`, `*.*.*`,
    `tabs` and `tabs.documentTab.*`, which reach them through ``*`` alone, were answered. A child
    tab's anchors are one as well, however deep: measured 2026-10-28,
    `tabs.childTabs.documentTab.commentAnchors`, `tabs.childTabs.*.commentAnchors`,
    `tabs.*.*.commentAnchors`, `tabs.*.*.*.commentAnchors` and
    `tabs.childTabs.childTabs.documentTab.commentAnchors` were each refused on a one-tab Doc."""

    def anchored(tab: dict) -> bool:
        return bool(tab) and (
            "commentAnchors" in tab.get("documentTab", {}) or anchored(tab.get("childTabs", {}))
        )

    return "comments" in named or "suggestions" in named or anchored(named.get("tabs", {}))


def _docs_stored(conn, row) -> dict:
    """A Doc as `backlot.gdocs` stores it, or its line reading when it states no structure."""
    stored = store.gdrive_doc_for(conn, row["id"])
    return stored if stored is not None else gdocs.from_prose(row["content"] or "")


def _docs_tab_properties(tab: dict, parent: dict | None, index: int, depth: int) -> dict:
    """A tab's `tabProperties` in real's order, its parent, index and depth read off the tree:
    measured 2026-11-02, a root tab has no `parentTabId` and no `nestingLevel`, and the first tab
    serves `index` 0."""
    props = tab["tabProperties"]
    out = {"tabId": props["tabId"], "title": props["title"]}
    if parent is not None:
        out["parentTabId"] = parent["tabProperties"]["tabId"]
    out["index"] = index
    if depth:
        out["nestingLevel"] = depth
    if props.get("iconEmoji"):
        out["iconEmoji"] = props["iconEmoji"]
    return out


def _docs_tabs(tabs: list[dict], parent: dict | None = None, depth: int = 0) -> list[dict]:
    """The `tabs` real serves: each tab's properties, its child tabs and then its contents."""
    out = []
    for i, tab in enumerate(tabs):
        served = {"tabProperties": _docs_tab_properties(tab, parent, i, depth)}
        if tab["childTabs"]:
            served["childTabs"] = _docs_tabs(tab["childTabs"], tab, depth + 1)
        served["documentTab"] = gdocs.document_tab(tab)
        out.append(served)
    return out


# Real's `revisionId` is `AHj4eM` and about a hundred characters of URL-safe base64 (measured
# 2026-10-26). This one encodes the Doc and its revision, so a write can tell an id of another Doc
# from one of no Doc at all, as real does (`docs_batch_update`).
_DOCS_REVISION_PREFIX = "AHj4eM"


def _docs_revision(document_id: str, revision: int = 0) -> str:
    """The `revisionId` the owner is served after a Doc's ``revision``-th `documents.batchUpdate`:
    measured 2026-10-26, a created Doc's next `documents.get` served its create's, and on
    2026-10-29 each batchUpdate answered a new one, which the next read served."""
    encoded = base64.urlsafe_b64encode(f"{revision}:{document_id}".encode()).decode()
    return _DOCS_REVISION_PREFIX + encoded.rstrip("=")


def _docs_revision_of(revision_id: str) -> tuple[str, int] | None:
    """The Doc and revision a `revisionId` this server served names, or None for any other."""
    if not revision_id.startswith(_DOCS_REVISION_PREFIX):
        return None
    encoded = revision_id[len(_DOCS_REVISION_PREFIX) :]
    try:
        text = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        revision, document_id = text.split(":", 1)
        return document_id, int(revision)
    except (ValueError, binascii.Error):
        return None


def _docs_refuse_suggestions() -> gerr.GoogleError:
    return gerr.GoogleError(
        403,
        "You do not have permission to access the document suggestions.",
        reason="forbidden",
        status="PERMISSION_DENIED",
    )


@router.get("/docs/v1/documents/{document_id}", openapi_extra={"parameters": _P_DOCS_GET})
async def docs_get(document_id: str, request: Request):
    """The document as Docs structural elements: its stored tabs, or one tab holding a
    ``paragraph`` per line of its text when it states none (`backlot.gdocs`), flat as its first
    tab or as all of them. Measured 2026-11-02, the flat answer is the first tab's whatever its id.

    The order real answers in, measured 2026-10-26: the credential, the typed values, the mask
    (`fields=nope` on an id that does not exist is its 400), the lookup, a reader's suggestions
    403, the `commentsViewMode` 400s, a comment field's 400 and then a legacy field's. Each comes
    ahead of the next when both hold, `fields=body,comments&includeTabsContent=true` answering the
    comment field's.

    A caller who is not the owner is a reader here (`routers.google._drive_file_capabilities`), the
    admin token of every file it did not create among them (`routers.google._drive_owned_by`). A
    reader is answered with no `revisionId`, and a `suggestionsViewMode` it does not ask for is
    `PREVIEW_WITHOUT_SUGGESTIONS`, where the owner's is `SUGGESTIONS_INLINE`; a reader asking for
    `SUGGESTIONS_INLINE` or `PREVIEW_SUGGESTIONS_ACCEPTED` is the 403. A mode asked for is echoed by
    its name, its number included. No Doc here holds a suggestion, so a preview serves the same
    content."""
    caller = _require(request)
    flags = _typed_query(
        request,
        {
            "suggestionsViewMode": lambda raw: _query_enum(
                raw, "suggestions_view_mode", _DOCS_SUGGESTIONS_VIEW
            ),
            "includeTabsContent": lambda raw: _query_bool(raw, "include_tabs_content"),
            "commentsViewMode": lambda raw: _query_enum(
                raw, "comments_view_mode", _DOCS_COMMENTS_VIEW
            ),
        },
    )
    tree, named = _gmask_select(gerr.first_repeat(request.query_params, "fields"), "docs.Document")
    row = _editor_doc(request, document_id, expect="document")
    owned = _drive_owned_by(row["author_email"], caller.email)

    asked = (flags["suggestionsViewMode"] or [None])[-1]
    if not owned and asked in ("SUGGESTIONS_INLINE", "PREVIEW_SUGGESTIONS_ACCEPTED"):
        raise _docs_refuse_suggestions()
    if asked in (None, "DEFAULT_FOR_CURRENT_ACCESS"):
        suggestions = "SUGGESTIONS_INLINE" if owned else "PREVIEW_WITHOUT_SUGGESTIONS"
    else:
        suggestions = asked

    # The tabbed answer is asked for by `includeTabsContent`, or by a mask naming `tabs`: measured
    # 2026-10-26, `fields=tabs` and `*.tabProperties` answered it beside `includeTabsContent=false`
    # and without it, and `fields=*`, `*.*` and `tabs()` answered the flat document.
    tabs = (flags["includeTabsContent"] or [False])[-1] or "tabs" in named
    comments = (flags["commentsViewMode"] or ["COMMENTS_VIEW_MODE_UNSPECIFIED"])[-1]
    asks_comments = comments in (
        "COMMENTS_VIEW_MODE_DEFAULT_FOR_CURRENT_ACCESS",
        "COMMENTS_VIEW_MODE_INCLUDED",
    )
    # A mode other than `UNSPECIFIED` needs the tabs, and the preview refusal is the explicit
    # preview's: a reader's `DEFAULT_FOR_CURRENT_ACCESS` beside no `suggestionsViewMode`, which it
    # is served as a preview, answered 200.
    if comments != "COMMENTS_VIEW_MODE_UNSPECIFIED" and not tabs:
        raise gerr.invalid_argument(
            "Comments view mode may only be specified if tabs content is also requested."
        )
    if asks_comments and asked in _DOCS_PREVIEWS:
        raise gerr.invalid_argument("Comments may not be requested when previewing suggestions.")
    if comments == "COMMENTS_VIEW_MODE_INCLUDED" and asked != "SUGGESTIONS_INLINE":
        raise gerr.invalid_argument(
            "Comments may only be explicitly included if inline suggestions are also explicitly "
            "requested."
        )
    # The comment fields follow the mode asked for, not the one answered: a reader's
    # `DEFAULT_FOR_CURRENT_ACCESS` is answered `OMITTED` and takes `fields=comments`.
    if not asks_comments and _docs_comment_fields(named):
        raise gerr.invalid_argument(
            "Field mask may not contain comment-specific fields if comments are not requested"
        )
    if tabs and _DOCS_LEGACY & named.keys():
        raise gerr.invalid_argument(
            "Field mask may not contain legacy text-level Document resource fields while "
            "requesting tabs content"
        )
    # `INCLUDED` to the owner, who alone here may comment; no Doc here holds a comment, so it
    # adds nothing to the answer.
    view = (
        "COMMENTS_VIEW_MODE_INCLUDED" if asks_comments and owned else "COMMENTS_VIEW_MODE_OMITTED"
    )

    conn = auth.conn(request)
    stored = _docs_stored(conn, row)
    written = store.gdrive_doc_revision(conn, document_id)
    revision = {"revisionId": _docs_revision(document_id, written)} if owned else {}
    # Real's orders, measured 2026-10-16 on twelve Docs and 2026-10-26 on the tabbed answer, the
    # same under each mode and whatever order a mask names the members in. Real also serves
    # `lists` after `namedStyles` and `inlineObjects` before `documentId` when a Doc has them; this
    # server serves neither.
    if tabs:
        doc = {
            "title": row["title"],
            **revision,
            "suggestionsViewMode": suggestions,
            "documentId": document_id,
            "tabs": _docs_tabs(stored["tabs"]),
            "commentsViewMode": view,
        }
    else:
        doc = {
            "title": row["title"],
            **gdocs.document_tab(stored["tabs"][0], len(list(gdocs.walk(stored["tabs"]))) == 1),
            **revision,
            "suggestionsViewMode": suggestions,
            "documentId": document_id,
            "commentsViewMode": view,
        }
    return doc if tree is None else _gmask_apply(tree, doc)


# --- `documents.create` ---------------------------------------------------------------------------

# A new Doc's title when the request names none, an English account's: measured 2026-10-28, a
# missing, `null` or empty `title` was this, and `"   "` was kept as sent.
DOCS_UNTITLED = "Untitled document"

# The longest title a create keeps, in UTF-16 code units: measured 2026-10-29, 10,000 `x` or
# 5,000 `😀` were kept and 10,001 `x` or 5,001 `😀` refused, from the body or the query alike,
# after the body's refusals, the mask's and a batch's 501.
_DOCS_TITLE_UNITS = 10_000

_DOCS_WRAPPED = {
    "StringValue": "string",
    "BoolValue": "bool",
    "FloatValue": "float",
    "Int32Value": "int32",
}


def _docs_shape(spec: str) -> dict:
    if spec.startswith("enum "):
        return {"kind": "enum", "enum": _docs_enum(spec.removeprefix("enum "))}
    if spec in _DOCS_WRAPPED:
        return {"kind": _DOCS_WRAPPED[spec], "wrapper": True}
    return {"kind": spec}


_DOCS_SHAPES = {
    (f"docs.{message}", field): _docs_shape(spec)
    for message, fields in _DOCS_PROTO["fields"].items()
    for field, spec in fields.items()
}
for _message, _groups in _DOCS_PROTO["oneofs"].items():
    for _group, _members in _groups.items():
        for _member in _members:
            _DOCS_SHAPES.setdefault((f"docs.{_message}", _member), {})["oneof"] = _group

_DOCS_WRITE_MODE = protojson.Enum(
    f"{_DOCS_VIEW}.WriteControl.WriteMode", ("WRITE_MODE_UNSPECIFIED", "EDIT", "SUGGEST")
)
# The oneofs of a `documents.batchUpdate` body, each named as real's refusal of a second member
# names it: measured 2026-10-29, a request with two kinds was refused with
# `Invalid value at 'requests[0]' (oneof), oneof field 'request' is already set.`, and likewise
# `insertion_location` and `control`.
for _key, _group in {
    **{("docs.Request", kind): "request" for kind in _GOOGLE_RECORDED["docs.Request"]["fields"]},
    ("docs.InsertTextRequest", "location"): "insertion_location",
    ("docs.InsertTextRequest", "endOfSegmentLocation"): "insertion_location",
    ("docs.InsertPageBreakRequest", "location"): "insertion_location",
    ("docs.InsertTableRequest", "location"): "insertion_location",
    ("docs.InsertTableRequest", "endOfSegmentLocation"): "insertion_location",
    ("docs.InsertPageBreakRequest", "endOfSegmentLocation"): "insertion_location",
    ("docs.CreateFootnoteRequest", "location"): "footnote_reference_location",
    ("docs.CreateFootnoteRequest", "endOfSegmentLocation"): "footnote_reference_location",
    ("docs.WriteControl", "requiredRevisionId"): "control",
    ("docs.WriteControl", "targetRevisionId"): "control",
    ("docs.ReplaceAllTextRequest", "containsText"): "criteria",
    ("docs.DeleteNamedRangeRequest", "namedRangeId"): "named_range_reference",
    ("docs.DeleteNamedRangeRequest", "name"): "named_range_reference",
    ("docs.ReplaceNamedRangeContentRequest", "namedRangeId"): "named_range_reference",
    ("docs.ReplaceNamedRangeContentRequest", "namedRangeName"): "named_range_reference",
    # measured 2026-11-09
    ("docs.UpdateTableCellStyleRequest", "tableRange"): "cells",
    ("docs.UpdateTableCellStyleRequest", "tableStartLocation"): "cells",
    **{
        (f"docs.{kind}Request", member): "thread_id"
        for kind in ("AddCommentReply", "UpdateCommentPost", "DeleteCommentReply")
        for member in ("commentId", "suggestionId")
    },
}.items():
    _DOCS_SHAPES[_key] = {"oneof": _group}
# `createHeader`'s and `createFooter`'s `type`: measured 2026-11-02, `1` was read as `DEFAULT`, and
# 2026-11-03, `FIRST_PAGE`, which the discovery document leaves out, was read as 2.
_DOCS_HEADER_FOOTER = protojson.Enum(
    f"{_DOCS_VIEW}.HeaderFooterType", ("HEADER_FOOTER_TYPE_UNSPECIFIED", "DEFAULT", "FIRST_PAGE")
)
# `createParagraphBullets`' `bulletPreset`, its names in number order (`gdocs.PRESETS`): measured
# 2026-11-03, a name it lacks is refused naming this type, as `NOPE` and `1.5` were.
_DOCS_PRESETS = gdocs.PRESETS
_DOCS_BULLET_PRESET = protojson.Enum(
    f"{_DOCS_VIEW}.BulletGlyphPreset", ("BULLET_GLYPH_PRESET_UNSPECIFIED", *_DOCS_PRESETS)
)

# A request is declared inside the body's message: `"requests": "x"` is refused naming
# `….BatchUpdateDocumentRequest.Request`.
_PJ_DOCS = _pj_messages(
    _DOCS_VIEW,
    {
        ("docs.WriteControl", "writeMode"): _DOCS_WRITE_MODE,
        ("docs.CreateHeaderRequest", "type"): _DOCS_HEADER_FOOTER,
        ("docs.CreateFooterRequest", "type"): _DOCS_HEADER_FOOTER,
        ("docs.CreateParagraphBulletsRequest", "bulletPreset"): _DOCS_BULLET_PRESET,
        ("docs.InsertSectionBreakRequest", "sectionType"): _docs_enum("SectionStyle.SectionType"),
        # measured 2026-11-09: `FOO` is refused naming this type, and -1 and 7 are read
        ("docs.ReplaceImageRequest", "imageReplaceMethod"): protojson.Enum(
            f"{_DOCS_VIEW}.ImageReplaceMethod", ("IMAGE_REPLACE_METHOD_UNSPECIFIED", "CENTER_CROP")
        ),
    },
    _DOCS_SHAPES,
    {"docs.Request": "BatchUpdateDocumentRequest.Request"},
)

# The request body is the `Document` itself, read against the recorded message
# (`routers.google._pj_messages`) and `_DOCS_SHAPES`: measured 2026-10-28, an unknown name is
# ``Unknown name "nope": Cannot find field.`` with no field path, a nested one names its message
# (``at 'body'``), and a name the `Document` has is typed (``Invalid value at 'title' (TYPE_STRING),
# 5``); and 2026-10-29, a number in any enum field is read whether or not the enum declares it,
# and a map's entries are read as its value's message.
_PJ_DOCUMENT = _PJ_DOCS("docs.Document")

# A query parameter binds to the `Document` field it names. Measured 2026-10-28: `title` names the
# Doc over the body's, its last repeat read and an empty one naming none; the enums are read as
# `documents.get` reads them and refused beside the body's refusals (`_query_and_body`), ahead of
# the `fields` mask's. `documentId` and `revisionId` bind and change nothing, as in the body.
_DOCS_CREATE_QUERY = {
    "title": lambda raw: raw,
    "suggestionsViewMode": lambda raw: _query_enum(
        raw, "suggestions_view_mode", _DOCS_SUGGESTIONS_VIEW
    ),
    "commentsViewMode": lambda raw: _query_enum(raw, "comments_view_mode", _DOCS_COMMENTS_VIEW),
}

_P_DOCS_CREATE = [qp("fields")]


@router.post(
    "/docs/v1/documents",
    openapi_extra={"parameters": _P_DOCS_CREATE, **proto_body(_PJ_DOCUMENT, keep={"title": None})},
)
async def docs_create(request: Request):
    """``documents.create``: a blank Doc at the caller's My Drive root, owned by the caller and
    shared with nobody (`routers.google._editor_store`), answered as its owner's `documents.get`
    answers it with `tabs` beside the flat members and no `commentsViewMode`.

    Measured 2026-10-28:

    * only `title` is kept: a stated `body`, `tabs`, `documentStyle`, `namedStyles`, `documentId`,
      `revisionId`, `suggestionsViewMode` and `commentsViewMode` are read, typed, and dropped;
    * the body is a section break and one empty paragraph `[1, 2)`;
    * the order is the credential, the body and the typed query values, then the `fields` mask;
    * inside a batch it is the 501 real's batch answered it with;
    * a mask is applied to the whole answer, tabs included, so `fields=*` serves the tabs, and
      neither `comments` nor a member a tab also carries is refused beside them;
    * a title past `_DOCS_TITLE_UNITS` is refused last."""
    caller = _require(request)
    query, body = await _query_and_body(request, _DOCS_CREATE_QUERY, _PJ_DOCUMENT)
    tree, _ = _gmask_select(gerr.first_repeat(request.query_params, "fields"), "docs.Document")
    if _BATCH_OUTER.get() is not None:
        raise gerr.unimplemented()
    title = query["title"] if query["title"] is not None else body.get("title")
    title = title or DOCS_UNTITLED
    if len(title.encode("utf-16-le")) > 2 * _DOCS_TITLE_UNITS:
        raise gerr.invalid_argument("Request contains an invalid argument.")
    conn = auth.conn(request)
    owner = _editor_creator(request, caller)
    file_id = _editor_new_file_id(conn, owner)
    _editor_store(conn, file_id, owner, title, "document", "")
    blank = gdocs.from_prose("")
    doc = {
        "title": title,
        **gdocs.document_tab(blank["tabs"][0], True),
        "revisionId": _docs_revision(file_id),
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
        "documentId": file_id,
        "tabs": _docs_tabs(blank["tabs"]),
    }
    return doc if tree is None else _gmask_apply(tree, doc)


# --- `documents.batchUpdate` ----------------------------------------------------------------------
#
# Measured against the live Docs v1 API on 2026-10-29, 2026-11-02 and 2026-11-03, as a Workspace
# member writing a Doc it owns and as a second member it was shared with as a reader. How each
# request changes the document is `gdocs.Body`'s and `gdocs.Document`'s; this section is the
# request, its refusals and the answer.

_PJ_BATCH_UPDATE = _PJ_DOCS("docs.BatchUpdateDocumentRequest")


def _docs_tab(doc: gdocs.Document, where: dict) -> dict:
    """The tab a location or range names, the first for none, refused when the Doc has no such tab.
    Measured, the tab is checked before anything else the location or range states."""
    tab = doc.tab(where.get("tab_id"))
    if tab is None:
        raise _EditorRefused("Cannot apply request to an invalid tab ID.")
    return tab


def _docs_segment(doc: gdocs.Document, tab: dict, where: dict) -> tuple[str, gdocs.Body]:
    """The kind of segment (`gdocs.Document.kind`) a location or range names in ``tab``, and the
    segment opened, refused when the tab has no such header, footer or footnote (a header's or
    footnote's id names nothing in another tab)."""
    segment = where.get("segment_id", "")
    kind = doc.kind(tab, segment)
    if kind is None:
        raise _EditorRefused(
            f"Segment with ID {segment} was not found. If a segment ID is provided, it must be a "
            "header, footer or footnote ID. Use an empty segment ID to reference the body."
        )
    return kind, doc.open(tab, segment)


def _docs_negative(index: int) -> None:
    if index < 0:
        raise _EditorRefused("Index must be greater than or equal to 0.")


def _docs_past_end(index: int, body: gdocs.Body) -> None:
    if index >= body.end:
        raise _EditorRefused(
            f"Index {index} must be less than the end index of the referenced segment, {body.end}."
        )


# What an insert at the first section break is refused with, and what `insertText` adds to it:
# measured 2026-10-29.
_DOCS_OUTSIDE = "The insertion index must be inside the bounds of an existing paragraph."
_DOCS_NEWLINES = " You can still create new paragraphs by inserting newlines."


def _docs_in_paragraph(body: gdocs.Body, index: int, message: str) -> None:
    """Refuse an index at a body's section break or at a table's start, row, cell or end
    (`gdocs.TABLE`): measured 2026-11-03, an insert at each was refused as one at 0."""
    pos = body.position(index)
    if index < body.base or pos is not None and not body.in_paragraph(pos):
        raise _EditorRefused(message)


def _docs_cluster(body: gdocs.Body, index: int) -> int:
    pos = body.position(index)
    if pos is None or body.splits_cluster(pos):
        raise _EditorRefused("The insertion index cannot be within a grapheme cluster.")
    return pos


def _docs_insert_text(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``insertText``, in real's order of refusals, measured: no location, the tab, no text, an
    index below zero, the segment, then the index at or past the segment's end, a body's section
    break at 0, inside a grapheme cluster. `endOfSegmentLocation` inserts before the segment's last
    newline. The characters `insertText` strips are dropped, and text that was nothing else inserts
    nothing and is taken. Real fails on one that holds a character in `gdocs.UNWRITABLE` once every
    request is read."""
    where = req.get("location", req.get("end_of_segment_location"))
    if where is None:
        raise _EditorRefused("No insertion location set.")
    tab = _docs_tab(doc, where)
    text = req.get("text", "")
    if not text:
        raise _EditorRefused("Insert text requests must specify text to insert.")
    index = where.get("index", 0)
    if "location" in req:
        _docs_negative(index)
    _, body = _docs_segment(doc, tab, where)
    if "location" in req:
        _docs_past_end(index, body)
        _docs_in_paragraph(body, index, _DOCS_OUTSIDE + _DOCS_NEWLINES)
        pos = _docs_cluster(body, index)
    else:
        pos = len(body.chars) - 1
    kept = gdocs.STRIPPED.sub("", text)
    if kept:
        body.insert(pos, kept)
    return {}, bool(gdocs.UNWRITABLE.search(kept))


def _docs_range(doc: gdocs.Document, rng: dict | None, missing: str) -> tuple:
    """The tab, the kind of segment, the segment opened and the two indices a request's range
    names, in real's order of refusals, measured 2026-10-29 on `deleteContentRange` and 2026-11-03
    on the style, bullet and named range requests: no range (``missing``), the tab, an index
    missing (a `null` one included), the end before the start, an index below zero, the segment,
    then the start's and the end's last index at or past the segment's end."""
    if rng is None:
        raise _EditorRefused(missing)
    tab = _docs_tab(doc, rng)
    if "start_index" not in rng or "end_index" not in rng:
        raise _EditorRefused(_DOCS_NO_INDEX)
    start, end = rng["start_index"], rng["end_index"]
    if end < start:
        raise _EditorRefused("Invalid range: end index cannot be less than start index")
    _docs_negative(start)
    kind, body = _docs_segment(doc, tab, rng)
    _docs_past_end(start, body)
    _docs_past_end(end - 1, body)
    return tab, kind, body, start, end


_DOCS_NO_INDEX = "Invalid range: must contain a start and end index"


def _docs_delete_content_range(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``deleteContentRange``, in real's order of refusals, measured: the range (`_docs_range`), an
    empty range, a body's section break at 0, the segment's last newline, and either end inside a
    surrogate pair or the range taking part of a table (`gdocs.Body.cuts_table`). Deleting a
    combining mark without the letter it sits on is taken, and deleting a footnote reference
    deletes its footnote."""
    _, _, body, start, end = _docs_range(
        doc, req.get("range"), "Delete content range requests must include a range to delete."
    )
    if start == end:
        raise _EditorRefused("The range should not be empty.")
    if start < body.base:
        raise _EditorRefused("Cannot operate on the first section break in the document.")
    if end == body.end:
        raise _EditorRefused(
            "The range cannot include the newline character at the end of the segment."
        )
    first, last = body.position(start), body.position(end)
    if (
        first is None
        or last is None
        or body.cuts_table(first, last)
        or body.cuts_section(first, last)
    ):
        raise _EditorRefused("Invalid deletion range. Cannot delete the requested range.")
    body.delete(first, last)
    return {}, False


def _docs_replace_all_text(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``replaceAllText``: in every tab, or only in those a `tabsCriteria` lists, in each tab's
    body, header, footer and footnotes. Measured 2026-11-02: with no `tabsCriteria` a child tab was
    replaced in too, one listing no tab replaced nothing, a tab listed twice counted once, and a
    match in a header, a footer and a footnote each counted. Real fails at this request on a
    `tabsCriteria` naming a tab the Doc does not have (measured 2026-11-03), and once every request
    is read on a replacement holding a character in `gdocs.UNWRITABLE` that matched; measured, a
    zero-match replace by U+FDD0 was a 200. What matches is `gdocs.Body.find`'s, and an empty or
    missing `replaceText` deletes it; the characters `insertText` strips are stripped from it too. A
    `searchByRegex` replacement is the text as sent: `$1` is not a group."""
    criteria = req.get("contains_text") or {}
    if not criteria.get("text"):
        raise _EditorRefused("The substring match text should not be empty")
    tabs = doc.flat()
    if "tabs_criteria" in req:
        # measured 2026-11-03: an empty id named `t.0`, not the first tab, and was a 500 once
        # `t.0` was deleted
        named = [n or "t.0" for n in req["tabs_criteria"].get("tab_ids", [])]
        if any(not any(t["tabProperties"]["tabId"] == n for t in tabs) for n in named):
            raise gerr.internal_error()
        tabs = [t for t in tabs if t["tabProperties"]["tabId"] in named]
        # measured 2026-11-02: a `tabsCriteria` of no tab kept the `revisionId`, where a replace
        # that matched nothing in a tab moved it
        doc.unchanged += not tabs
    text = gdocs.STRIPPED.sub("", req.get("replace_text", ""))
    found = 0
    for tab in tabs:
        for body in doc.segments(tab):
            matches = body.find(
                criteria["text"],
                match_case=criteria.get("match_case", False),
                regex=criteria.get("search_by_regex", False),
            )
            body.replace(matches, text)
            found += len(matches)
    fails = bool(found and gdocs.UNWRITABLE.search(text))
    return {"replaceAllText": {"occurrencesChanged": found} if found else {}}, fails


def _docs_insert_element(doc: gdocs.Document, req: dict, what: str) -> tuple[dict, gdocs.Body, int]:
    """Where a footnote reference or a page break goes, in real's order of refusals, measured
    2026-11-02: no location, the tab, an index below zero, the segment, the index at or past the
    segment's end, a header, footer or footnote, then 0 and inside a grapheme cluster.
    `endOfSegmentLocation` is before the body's last newline. Measured 2026-11-03, a page break at
    a table's row, cell or end or in a cell's text is refused as in a table, ahead of the refusal
    at 0; at a table's start it is refused as at 0, and so is a footnote reference at any of a
    table's indices (`_docs_in_paragraph`)."""
    where = req.get("location", req.get("end_of_segment_location"))
    if where is None:
        raise _EditorRefused(
            "No footnote location set." if what == "footnote" else "No insertion location set."
        )
    tab = _docs_tab(doc, where)
    index = where.get("index", 0)
    if "location" in req:
        _docs_negative(index)
    kind, body = _docs_segment(doc, tab, where)
    if "location" in req:
        _docs_past_end(index, body)
    if kind != "body":
        if what == "footnote":
            raise _EditorRefused(f"Cannot insert a footnote reference in a {kind}.")
        raise _EditorRefused(f"Cannot insert page break in a {kind}.")
    if "location" not in req:
        return tab, body, len(body.chars) - 1
    pos = body.position(index)
    if what != "footnote" and pos is not None and body.in_table(pos):
        raise _EditorRefused("Cannot insert page break in a table.")
    _docs_in_paragraph(body, index, _DOCS_OUTSIDE)
    return tab, body, _docs_cluster(body, index)


def _docs_create_footnote(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``createFootnote``: a reference in the body (`_docs_insert_element`) to a new footnote of a
    space in 10 PT (`gdocs.blank_segment`), answered with its id."""
    tab, body, pos = _docs_insert_element(doc, req, "footnote")
    footnote = doc.new_id(gdocs.segment_id)
    tab["footnotes"][footnote] = gdocs.blank_segment(footnote=True)
    body.insert_footnote(pos, footnote)
    return {"createFootnote": {"footnoteId": footnote}}, False


def _docs_insert_page_break(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``insertPageBreak``: a page break and a newline in the body (`_docs_insert_element`), which
    ends the paragraph after the break as an inserted newline does. Measured 2026-11-02: inside a
    bold run both were bold, and at the end of a heading the heading's newline was left a
    paragraph of its own."""
    _, body, pos = _docs_insert_element(doc, req, "page break")
    body.insert(pos, gdocs.PAGE_BREAK + "\n")
    return {}, False


def _docs_insert_table(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``insertTable``: a table of empty cells after a newline it inserts
    (`gdocs.Body.insert_table`), in real's order of refusals, measured 2026-11-03: no location,
    the tab, an index below zero, the segment, the index at or past the segment's end, no rows, no
    columns, a footnote, the first section break or a table's start, row, cell or end, then inside
    a grapheme cluster. No number of rows or columns is refused here; real made a table of
    100,000 rows and one of 20,000 columns, and answered one of 400,000 rows or of 1,000 by 1,000
    with a 503 and one of 2,147,483,647 rows with a 500 (measured 2026-11-05).
    `endOfSegmentLocation` is before the segment's last newline, a header's or footer's
    included."""
    where = req.get("location", req.get("end_of_segment_location"))
    if where is None:
        raise _EditorRefused("No insertion location set.")
    tab = _docs_tab(doc, where)
    index = where.get("index", 0)
    if "location" in req:
        _docs_negative(index)
    kind, body = _docs_segment(doc, tab, where)
    if "location" in req:
        _docs_past_end(index, body)
    rows, columns = req.get("rows", 0), req.get("columns", 0)
    if rows <= 0:
        raise _EditorRefused("The number of table rows should be greater than zero.")
    if columns <= 0:
        raise _EditorRefused("The number of table columns should be greater than zero.")
    if kind == "footnote":
        raise _EditorRefused("Cannot insert table in a footnote.")
    if "location" in req:
        _docs_in_paragraph(body, index, _DOCS_OUTSIDE)
        pos = _docs_cluster(body, index)
    else:
        pos = len(body.chars) - 1
    body.insert_table(pos, rows, columns)
    return {}, False


def _docs_cell(doc: gdocs.Document, req: dict, name: str) -> tuple[gdocs.Body, int, int, int]:
    """The segment, the table's position and the row and column a `tableCellLocation` names, in
    real's order of refusals, measured 2026-11-03: no cell location, no table start, the tab, an
    index below zero, the segment, the index at or past its end, an index that is not a table's
    start, the row below zero, the column below zero, then the row and the column past their
    count (row 9 and column -1 was refused for the column)."""
    cell = req.get("table_cell_location")
    if cell is None:
        raise _EditorRefused(f"{name} requests must specify a table cell location.")
    where = cell.get("table_start_location")
    if where is None:
        raise _EditorRefused("Table cell location must specify a table start location.")
    tab = _docs_tab(doc, where)
    index = where.get("index", 0)
    _docs_negative(index)
    _, body = _docs_segment(doc, tab, where)
    _docs_past_end(index, body)
    table = body.position(index)
    if table is None or not body.table_at(table):
        raise _EditorRefused(
            "Invalid table start location. Must specify the start index of the table."
        )
    grid = body.grid(table)
    axes = (
        ("row", cell.get("row_index", 0), len(grid)),
        ("column", cell.get("column_index", 0), len(grid[0]) - 1),
    )
    for named, at, _ in axes:
        if at < 0:
            raise _EditorRefused(f"The {named} index {at} should be non-negative.")
    for named, at, count in axes:
        if at >= count:
            raise _EditorRefused(
                f"The {named} index {at} should be less than the total number of {named}s {count}."
            )
    return body, table, cell.get("row_index", 0), cell.get("column_index", 0)


def _docs_insert_row(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``insertTableRow``: above the cell's row, or below it with `insertBelow`
    (`gdocs.Body.insert_row`)."""
    body, table, row, column = _docs_cell(doc, req, "Insert table row")
    body.insert_row(table, row, column, req.get("insert_below", False))
    return {}, False


def _docs_insert_column(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``insertTableColumn``: left of the cell's column, or right of it with `insertRight`
    (`gdocs.Body.insert_column`)."""
    body, table, row, column = _docs_cell(doc, req, "Insert table column")
    body.insert_column(table, row, column, req.get("insert_right", False))
    return {}, False


def _docs_delete_row(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``deleteTableRow``: the cell's row (`gdocs.Body.delete_row`)."""
    body, table, row, column = _docs_cell(doc, req, "Delete table row")
    body.delete_row(table, row, column)
    return {}, False


def _docs_delete_column(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``deleteTableColumn``: the cell's column (`gdocs.Body.delete_column`)."""
    body, table, row, column = _docs_cell(doc, req, "Delete table column")
    body.delete_column(table, row, column)
    return {}, False


# --- table styles and merged cells ----------------------------------------------------------------
#
# Measured 2026-11-09 against the live Docs v1 API, as a Workspace member on Docs it owns. How each
# request changes a table is `gdocs.Body`'s; this section is the request, its refusals and the
# values it keeps.

_PJ_TABLE_CELL_STYLE = _PJ_DOCS("docs.TableCellStyle")
_PJ_TABLE_ROW_STYLE = _PJ_DOCS("docs.TableRowStyle")
_PJ_TABLE_COLUMN = _PJ_DOCS("docs.TableColumnProperties")
_DOCS_ALIGNMENT = _docs_enum("ContentAlignment")
_DOCS_WIDTH_TYPE = _docs_enum("TableColumnProperties.WidthType")
_DOCS_NO_TABLE_START = "Table start location must be specified in the request."
# The narrowest column `updateTableColumnProperties` takes, in points.
_DOCS_MIN_COLUMN = 5


def _docs_table_tab(doc: gdocs.Document, where: dict | None) -> dict:
    """A table start location set, then its tab: measured, both ahead of a request's fields."""
    if where is None:
        raise _EditorRefused(_DOCS_NO_TABLE_START)
    return _docs_tab(doc, where)


def _docs_table_at(doc: gdocs.Document, tab: dict, where: dict) -> tuple[gdocs.Body, int]:
    """The segment and the table a table start location names, in real's order of refusals,
    measured 2026-11-09: an index below zero, the segment, the index at or past its end, then an
    index that is not a table's start (a row's among them)."""
    index = where.get("index", 0)
    _docs_negative(index)
    _, body = _docs_segment(doc, tab, where)
    _docs_past_end(index, body)
    table = body.position(index)
    if table is None or not body.table_at(table):
        raise _EditorRefused("The provided table start location is invalid.")
    return body, table


def _docs_table_range(doc: gdocs.Document, rng: dict) -> tuple[gdocs.Body, int, list[tuple]]:
    """The segment, the table and the cells a `TableRange` names, in real's order of refusals,
    measured 2026-11-09: no cell location, the tab, a row span and then a column span below one, a
    row and then a column below zero, no table start location, the rest of it (`_docs_table_at`),
    the row and then the column past their count, then a range past the table's edge."""
    cell = rng.get("table_cell_location")
    if cell is None:
        raise _EditorRefused("Table range must specify a table cell location.")
    where = cell.get("table_start_location")
    tab = _docs_tab(doc, where or {})
    for name in ("rowSpan", "columnSpan"):
        span = rng.get(_docs_snake(name), 0)
        if span <= 0:
            raise _EditorRefused(f"The {name} must be strictly positive, was: {span}")
    row, column = cell.get("row_index", 0), cell.get("column_index", 0)
    for name, at in (("rowIndex", row), ("columnIndex", column)):
        if at < 0:
            raise _EditorRefused(f"The tableCellLocation {name} must be non-negative, was: {at}")
    if where is None:
        raise _EditorRefused("Table cell location must specify a table start location.")
    body, table = _docs_table_at(doc, tab, where)
    grid = body.grid(table)
    for named, at, count in (("row", row, len(grid)), ("column", column, len(grid[0]) - 1)):
        if at >= count:
            raise _EditorRefused(
                f"The {named} index {at} should be less than the total number of {named}s {count}."
            )
    rows, columns = rng["row_span"], rng["column_span"]
    if row + rows > len(grid) or column + columns > len(grid[0]) - 1:
        raise _EditorRefused("The table range extends outside the bounds of the table.")
    cells = [(r, c) for r in range(row, row + rows) for c in range(column, column + columns)]
    return body, table, cells


def _docs_pin_rows(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``pinTableHeaderRows``: the table (`_docs_table_at`), then a count below zero or past the
    table's rows; zero unpins every row (`gdocs.Body.pin_header_rows`)."""
    where = req.get("table_start_location")
    body, table = _docs_table_at(doc, _docs_table_tab(doc, where), where)
    count = req.get("pinned_header_rows_count", 0)
    if count < 0:
        raise _EditorRefused("The number of pinned header rows cannot be negative.")
    rows = len(body.grid(table))
    if count > rows:
        raise _EditorRefused(
            f"The number of header rows to pin, {count}, exceeds the number of table rows, {rows}"
        )
    body.pin_header_rows(table, count)
    return {}, False


def _docs_specified(name: str) -> _EditorRefused:
    return _EditorRefused(f"A value for {name} must be specified in order to update it.")


def _docs_update_row_style(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``updateTableRowStyle``, in real's order of refusals, measured 2026-11-09: the table start
    location set and its tab, the fields (`tableHeader` unallowed: `pinTableHeaderRows` sets it),
    the rest of the location (`_docs_table_at`), the values, then each row index in turn. A
    minimum height named must be stated, in points and not below zero; no rows names every row,
    and a row named twice is taken."""
    where = req.get("table_start_location")
    tab = _docs_table_tab(doc, where)
    names = _docs_fields(
        req.get("fields", ""),
        _PJ_TABLE_ROW_STYLE,
        ("minRowHeight", "preventOverflow"),
        unallowed=("tableHeader",),
    )
    if names is None:
        raise gerr.internal_error()
    body, table = _docs_table_at(doc, tab, where)
    read = req.get("table_row_style", {})
    stated: dict = {}
    if "minRowHeight" in names:
        if "min_row_height" not in read:
            raise _docs_specified("min row height")
        height = _docs_dimension(read["min_row_height"])
        if height.get("magnitude", 0) < 0:
            raise _EditorRefused("The minimum row height cannot be negative.")
        stated["minRowHeight"] = height
    if "preventOverflow" in names:
        stated["preventOverflow"] = read.get("prevent_overflow") or None
    rows = len(body.grid(table))
    indices = req.get("row_indices", [])
    for at in indices:
        if at < 0:
            raise _EditorRefused(f"Invalid row index {at}. Row index must be non-negative.")
        if at >= rows:
            raise _EditorRefused(
                f"Invalid row index {at}. Row index must be less than the total number of rows "
                f"{rows}"
            )
    body.style_rows(table, indices, stated)
    return {}, False


def _docs_update_columns(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``updateTableColumnProperties``, in real's order of refusals, measured 2026-11-09: the table
    start location set and its tab, the fields, the rest of the location, the values, then each
    column index in turn. A width type named must be one, a width named needs the type named
    too, a fixed width needs one of at least 5 PT and an even one none; no columns names every
    column, and an even column loses its width."""
    where = req.get("table_start_location")
    tab = _docs_table_tab(doc, where)
    names = _docs_fields(req.get("fields", ""), _PJ_TABLE_COLUMN, gdocs.COLUMN_PROPERTIES_ORDER)
    if names is None:
        raise gerr.internal_error()
    body, table = _docs_table_at(doc, tab, where)
    read = req.get("table_column_properties", {})
    stated: dict = {}
    if "widthType" in names:
        kind = read.get("width_type", 0)
        if not 0 < kind < len(_DOCS_WIDTH_TYPE.names):
            named = _DOCS_WIDTH_TYPE.names[0] if kind == 0 else "UNRECOGNIZED"
            raise _EditorRefused(f"The column width type is invalid: {named}")
        stated["widthType"] = _DOCS_WIDTH_TYPE.names[kind]
    width = _docs_dimension(read["width"]) if "width" in names and "width" in read else None
    if "width" in names:
        if "widthType" not in stated:
            raise _EditorRefused("Width type must be provided when updating column width.")
        if stated["widthType"] == "FIXED_WIDTH" and "width" not in read:
            raise _EditorRefused(
                "Width value must be provided when updating column width to fixed width."
            )
        if stated["widthType"] == "EVENLY_DISTRIBUTED" and "width" in read:
            raise _EditorRefused(
                "Width value must not be provided when updating column width to evenly distributed."
            )
        if width is not None:
            if width.get("magnitude", 0) < _DOCS_MIN_COLUMN:
                raise _EditorRefused(
                    f"Column width must not be less than minimum column width: {_DOCS_MIN_COLUMN}."
                )
            stated["width"] = width
    if stated.get("widthType") == "EVENLY_DISTRIBUTED":
        stated["width"] = None
    columns = len(body.chars[table][1]["tableStyle"]["tableColumnProperties"])
    indices = req.get("column_indices", [])
    for at in indices:
        if not 0 <= at < columns:
            raise _EditorRefused(f"Invalid column index {at}.")
    body.style_columns(table, indices, stated)
    return {}, False


_DOCS_CELL_BORDERS = ("borderLeft", "borderRight", "borderTop", "borderBottom")
_DOCS_PADDINGS = ("paddingLeft", "paddingRight", "paddingTop", "paddingBottom")


def _docs_cell_style(read: dict, names: list[str]) -> dict:
    """The `TableCellStyle` members ``names`` names, one the request leaves unset None, refused
    as real refuses them, measured 2026-11-09 whatever order `fields` lists them in: the content
    alignment, each padding's unit, then each border's width, dash style and colour, a border
    being stated whole and never transparent. A colour channel out of bounds, an alignment
    outside its enum and a border narrower than zero are each a 500; a padding below zero is
    kept."""
    out: dict = {}
    if "contentAlignment" in names:
        value = read.get("content_alignment", 0)
        named = _docs_enum_name(_DOCS_ALIGNMENT, value)
        if value == 1:
            raise _EditorRefused(f"The alignment type is invalid: {named}")
        out["contentAlignment"] = named if value else None
    for key in _DOCS_PADDINGS:
        if key in names:
            proto = _docs_snake(key)
            out[key] = _docs_dimension(read[proto]) if proto in read else None
    for key in _DOCS_CELL_BORDERS:
        if key not in names:
            continue
        value = read.get(_docs_snake(key))
        if value is None:
            out[key] = None
            continue
        width = _docs_dimension(value.get("width", {}))
        if width.get("magnitude", 0) < 0:
            raise gerr.internal_error()
        dash = value.get("dash_style", 0)
        if not 0 < dash < len(_DOCS_DASH.names):
            named = "UNRECOGNIZED" if dash else "DASH_STYLE_UNSPECIFIED"
            raise _EditorRefused(f"The dash style is invalid: {named}")
        color = _docs_color(value.get("color", {}))
        if not color:
            raise _EditorRefused(
                "Table cell borders cannot be transparent. To hide a table cell border, make its "
                "width 0."
            )
        out[key] = {"color": color, "width": width, "dashStyle": _DOCS_DASH.names[dash]}
    if "backgroundColor" in names:
        out["backgroundColor"] = (
            _docs_color(read["background_color"]) if "background_color" in read else None
        )
    return out


def _docs_update_cell_style(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``updateTableCellStyle`` over a `tableRange`, or over every cell of the table at its
    `tableStartLocation`, in real's order of refusals, measured 2026-11-09: neither set, the
    tab they name, the fields (the spans unallowed, a border whole), the values
    (`_docs_cell_style`), then the range (`_docs_table_range`) or the table start location
    (`_docs_table_at`). `*` names every member but the spans, and takes off each one it leaves
    unset."""
    if "table_range" not in req and "table_start_location" not in req:
        raise _EditorRefused("No cells set.")
    where = req.get("table_start_location") or (
        req["table_range"].get("table_cell_location", {}).get("table_start_location", {})
    )
    _docs_tab(doc, where)
    every = tuple(k for k in gdocs.CELL_STYLE_ORDER if k not in ("rowSpan", "columnSpan"))
    names = _docs_fields(
        req.get("fields", ""),
        _PJ_TABLE_CELL_STYLE,
        every,
        _DOCS_CELL_BORDERS,
        unallowed=("rowSpan", "columnSpan"),
    )
    if names is None:
        raise gerr.internal_error()
    stated = _docs_cell_style(req.get("table_cell_style", {}), names)
    if "table_range" in req:
        body, table, cells = _docs_table_range(doc, req["table_range"])
    else:
        where = req["table_start_location"]
        body, table = _docs_table_at(doc, _docs_tab(doc, where), where)
        grid = body.grid(table)
        cells = [(r, c) for r in range(len(grid)) for c in range(len(grid[0]) - 1)]
    body.style_cells(table, cells, stated)
    return {}, False


def _docs_merge(name: str):
    """``mergeTableCells`` or ``unmergeTableCells``: a range set (`_docs_table_range`); a merge of
    cells only part of an earlier merge holds is refused. A merge of one cell, and an unmerge of
    cells no merge holds, change nothing (`gdocs.Body.merge_cells`, `gdocs.Body.unmerge_cells`)."""
    merging = name == "Merge"

    def apply(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
        rng = req.get("table_range")
        if rng is None:
            raise _EditorRefused(f"{name} table cells request must specify a table range.")
        body, table, cells = _docs_table_range(doc, rng)
        if not merging:
            body.unmerge_cells(table, cells)
            return {}, False
        inside = set(cells)
        grid = body.grid(table)
        header = {body.chars[grid[r][0]][1]["tableRowStyle"].get("tableHeader") for r, _ in cells}
        # measured 2026-11-09: a merge over a header row and a row below the header was refused
        if any(not set(body._region(table, [cell])) <= inside for cell in cells) or len(header) > 1:
            raise _EditorRefused("A non-rectangular collection of cells cannot be merged.")
        if len(cells) == 1:
            # measured: a merge of one cell kept the `revisionId`
            doc.unchanged += 1
            return {}, False
        (row, column), (last_row, last_column) = cells[0], cells[-1]
        body.merge_cells(table, row, column, last_row - row + 1, last_column - column + 1)
        return {}, False

    return apply


# --- section breaks, and section, document and named styles --------------------------------------

_PJ_SECTION_STYLE = _PJ_DOCS("docs.SectionStyle")
_PJ_DOCUMENT_STYLE = _PJ_DOCS("docs.DocumentStyle")
_PJ_NAMED_STYLE = _PJ_DOCS("docs.NamedStyle")
_DOCS_SECTION_TYPE = _docs_enum("SectionStyle.SectionType")
_DOCS_SEPARATOR = _docs_enum("SectionStyle.ColumnSeparatorStyle")
_DOCS_DIRECTION = _docs_enum("ContentDirection")
_DOCS_MODE = _docs_enum("DocumentMode")
# The header and footer ids a section or a tab's document style serves, which no request sets.
_DOCS_SEGMENT_IDS = (
    "defaultHeaderId",
    "defaultFooterId",
    "firstPageHeaderId",
    "firstPageFooterId",
    "evenPageHeaderId",
    "evenPageFooterId",
)
# The margins and what a refusal of one left unset calls it.
_DOCS_MARGINS = {
    "marginTop": "top margin",
    "marginBottom": "bottom margin",
    "marginLeft": "left margin",
    "marginRight": "right margin",
    "marginHeader": "header margin",
    "marginFooter": "footer margin",
}


def _docs_insert_section_break(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``insertSectionBreak``, in real's order of refusals, measured 2026-11-09: no location, the
    tab, an index below zero, the segment, the index at or past its end, the section type, a
    header, footer or footnote, a table's row, cell or end or a cell's text, then the first
    section break, a table's start or a section break (`_docs_in_paragraph`).
    `endOfSegmentLocation` is before the body's last newline (`gdocs.Body.insert_section_break`)."""
    where = req.get("location", req.get("end_of_segment_location"))
    if where is None:
        raise _EditorRefused("No insertion location set.")
    tab = _docs_tab(doc, where)
    index = where.get("index", 0)
    if "location" in req:
        _docs_negative(index)
    kind, body = _docs_segment(doc, tab, where)
    if "location" in req:
        _docs_past_end(index, body)
    section = req.get("section_type", 0)
    if not 0 < section < len(_DOCS_SECTION_TYPE.names):
        named = _DOCS_SECTION_TYPE.names[0] if section == 0 else "UNRECOGNIZED"
        raise _EditorRefused(f"The section type is invalid: {named}")
    if kind != "body":
        raise _EditorRefused(f"Cannot insert section break in a {kind}.")
    if "location" in req:
        pos = body.position(index)
        if pos is not None and body.in_table(pos):
            raise _EditorRefused("Cannot insert section break in a table.")
        _docs_in_paragraph(body, index, _DOCS_OUTSIDE)
        pos = _docs_cluster(body, index)
    else:
        pos = len(body.chars) - 1
    body.insert_section_break(pos, _DOCS_SECTION_TYPE.names[section])
    return {}, False


def _docs_required(read: dict, names: list[str], key: str, refusal: str) -> dict | None:
    """A margin ``names`` names, which must be stated (refused as ``refusal``), as a `Dimension`,
    or None when it is not named."""
    if key not in names:
        return None
    proto = _docs_snake(key)
    if proto not in read:
        raise _docs_specified(refusal)
    return _docs_dimension(read[proto])


def _docs_column_widths(columns: list[dict], width: float) -> list[dict]:
    """A section's `columnProperties` as `updateSectionStyle` keeps them: each column the page's
    width between the margins, less every column's padding, shared out evenly, cut after three
    decimals. Measured 2026-11-09: two columns of 10 PT and 0 padding on a 612 PT page between
    72 PT margins were 229 PT wide, and three of 5, 5 and 0 were 152.666."""
    padded = [
        _docs_dimension(c["padding_end"]) if "padding_end" in c else {"unit": "PT"} for c in columns
    ]
    share = (width - sum(p.get("magnitude", 0) for p in padded)) / len(columns)
    each = {"magnitude": _docs_number(math.floor(share * 1000) / 1000), "unit": "PT"}
    return [{"width": dict(each), "paddingEnd": p} for p in padded]


def _docs_update_section_style(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``updateSectionStyle`` on every section the range overlaps, or for an empty range the one
    it falls in, each section starting at its break, in real's order of refusals, measured
    2026-11-09: the fields (the section type and the header and footer ids unallowed), the range
    (`_docs_range`, no range refused as an index missing; one in a header, footer or footnote is a
    500), then the values. A margin, the content direction or the columns named must be stated, in
    that order whatever order `fields` names them in, `*` naming them all; every column but the
    last needs its padding, and a width stated is refused (`_docs_column_widths`); a margin below
    zero is a 500, and the margins are fitted to the page (`_docs_fit_margins`). The first
    section's page numbering and first-page flag are its tab's `documentStyle`'s, a later
    section's its own."""
    names = _docs_fields(
        req.get("fields", ""),
        _PJ_SECTION_STYLE,
        tuple(
            k for k in gdocs.SECTION_STYLE_ORDER if k not in _DOCS_SEGMENT_IDS + ("sectionType",)
        ),
        unallowed=_DOCS_SEGMENT_IDS + ("sectionType",),
    )
    if names is None:
        raise gerr.internal_error()
    tab, kind, body, start, end = _docs_range(doc, req.get("range"), _DOCS_NO_INDEX)
    if kind != "body":
        raise gerr.internal_error()
    read = req.get("section_style", {})
    stated: dict = {}
    for key, refusal in _DOCS_MARGINS.items():
        margin = _docs_required(read, names, key, refusal)
        if margin is not None:
            stated[key] = margin
    if any(m.get("magnitude", 0) < 0 for m in stated.values()):
        raise gerr.internal_error()
    if "contentDirection" in names:
        if not read.get("content_direction"):
            raise _docs_specified("content direction")
        stated["contentDirection"] = _docs_enum_name(_DOCS_DIRECTION, read["content_direction"])
    if "columnProperties" in names:
        columns = read.get("column_properties", [])
        if not columns:
            raise _EditorRefused(
                "At least one set of column properties must be specified in order to update them."
            )
        if any("padding_end" not in c for c in columns[:-1]):
            raise _EditorRefused("Column padding must be set in order to update column properties.")
        if any("width" in c for c in columns):
            raise _EditorRefused("Column widths cannot be updated.")
    if "columnSeparatorStyle" in names:
        value = read.get("column_separator_style", 0)
        stated["columnSeparatorStyle"] = _docs_enum_name(_DOCS_SEPARATOR, value) if value else None
    for key in ("flipPageOrientation", "useFirstPageHeaderFooter"):
        if key in names:
            stated[key] = read.get(_docs_snake(key)) or None
    if "pageNumberStart" in names:
        stated["pageNumberStart"] = read.get("page_number_start")
    sections = body.sections()
    starts = [at for at, _ in sections] + [body.end]
    if end <= start:
        hit = [k for k in range(len(sections)) if starts[k] <= start < starts[k + 1]]
    else:
        hit = [k for k in range(len(sections)) if starts[k] < end and starts[k + 1] > start]
    page = doc.document_style(tab)
    margins = {key: value for key, value in stated.items() if key in _DOCS_MARGINS}
    for k in hit:
        style = sections[k][1]
        own = dict(stated)
        own |= _docs_fit_margins({**style, **margins}, page, set(margins))
        if "columnProperties" in names:
            left, right = (
                own.get(key, style.get(key, page[key])).get("magnitude", 0)
                for key in ("marginLeft", "marginRight")
            )
            width = page["pageSize"]["width"]["magnitude"] - left - right
            own["columnProperties"] = _docs_column_widths(read["column_properties"], width)
        if k == 0:
            moved = {
                key: own.pop(key)
                for key in ("pageNumberStart", "useFirstPageHeaderFooter")
                if key in own
            }
            if moved:
                doc.set_document_style(tab, moved)
        gdocs.restate(style, own, gdocs.SECTION_STYLE_ORDER)
    return {}, False


def _docs_update_document_style(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``updateDocumentStyle`` on the tab `tabId` names, the first for none, in real's order of
    refusals, measured 2026-11-09: the tab, the fields (the header and footer ids and
    `useCustomHeaderFooterMargins` unallowed), then the values: the background, which must be
    stated and not transparent, the top, bottom, left and right margins, the page's height and
    width, then the header and footer margins, each stated and in points, in that order whatever
    order `fields` names them in (`*` naming them all), then a page number start below zero. A margin below zero is kept, a page size named by one of its members
    sets that one, and a document mode left unset changes nothing. Setting a margin takes the same
    margin off every section of the tab. The tab's margins, then each section's, are fitted to the
    page (`_docs_fit_margins`), and then a page near a paper size takes that size
    (`_DOCS_PAPER`)."""
    tab = _docs_tab(doc, req)
    unallowed = _DOCS_SEGMENT_IDS + ("useCustomHeaderFooterMargins",)
    fields = req.get("fields", "")
    names = _docs_fields(
        fields,
        _PJ_DOCUMENT_STYLE,
        tuple(k for k in gdocs.DOCUMENT_STYLE_ORDER if k not in unallowed),
        unallowed=unallowed,
    )
    if names is None:
        raise gerr.internal_error()
    read = req.get("document_style", {})
    stated: dict = {}
    if "background" in names:
        background = read.get("background", {})
        if "color" not in background:
            raise _docs_specified("background color")
        color = _docs_color(background["color"])
        if not color:
            raise _EditorRefused("Cannot set a transparent background color for a document.")
        stated["background"] = {"color": color}
    for key, refusal in list(_DOCS_MARGINS.items())[:4]:
        margin = _docs_required(read, names, key, refusal)
        if margin is not None:
            stated[key] = margin
    if "pageSize" in names:
        size = read.get("page_size", {})
        named = [_docs_camel(p) for p in fields.split(",")]
        paths = {p.removeprefix("pageSize.") for p in named if p.startswith("pageSize.")}
        whole = fields.strip(_DOCS_SPACE) == "*" or "pageSize" in named
        current = doc.document_style(tab)["pageSize"]
        made = dict(current)
        for key, refusal in (("height", "page height"), ("width", "page width")):
            if whole or key in paths:
                if key not in size:
                    raise _docs_specified(refusal)
                made[key] = _docs_dimension(size[key])
        stated["pageSize"] = made
    for key, refusal in list(_DOCS_MARGINS.items())[4:]:
        margin = _docs_required(read, names, key, refusal)
        if margin is not None:
            stated[key] = margin
    if "pageNumberStart" in names:
        start = read.get("page_number_start")
        if start is not None and start < 0:
            raise _EditorRefused("Page number start index cannot be negative.")
        stated["pageNumberStart"] = start
    for key in ("useFirstPageHeaderFooter", "useEvenPageHeaderFooter", "flipPageOrientation"):
        if key in names:
            stated[key] = read.get(_docs_snake(key)) or None
    if "documentFormat" in names:
        mode = read.get("document_format", {}).get("document_mode", 0)
        if mode:
            stated["documentFormat"] = {"documentMode": _docs_enum_name(_DOCS_MODE, mode)}
    doc.set_document_style(tab, stated)
    page = doc.document_style(tab)
    doc.set_document_style(tab, _docs_fit_margins(page, page, set(stated) & set(_DOCS_MARGINS)))
    page = doc.document_style(tab)
    margins = {k: None for k in stated if k in _DOCS_MARGINS}
    for _, style in doc.open(tab).sections():
        gdocs.restate(style, margins, gdocs.SECTION_STYLE_ORDER)
        fitted = _docs_fit_margins(style, page, set(style) & {"marginLeft", "marginRight"})
        gdocs.restate(style, fitted, gdocs.SECTION_STYLE_ORDER)
    if "pageSize" in stated:
        doc.set_document_style(tab, {"pageSize": _docs_paper(page["pageSize"])})
    return {}, False


# The narrowest a page's text may be between its left and right margins, in points: measured
# 2026-11-09, margins of 400 PT either side of a 612 PT page were kept as 288 PT each.
_DOCS_MIN_TEXT_WIDTH = 36


def _docs_fit_margins(style: dict, page: dict, named: set[str]) -> dict:
    """The margins ``style`` (a tab's `documentStyle` or a section's `sectionStyle`) states, fitted
    to the page of ``page``, its tab's `documentStyle`, as `gdocs.restate` takes them. Measured
    2026-11-09 on a 612 by 792 PT page:
    - A left and right margin that leave less than `_DOCS_MIN_TEXT_WIDTH` between them are cut. A
      margin a section does not state is its tab's. When ``named`` holds one of the two and the
      other leaves room, that one is cut to the room left (600 beside 72 was 504, and beside 100.5,
      475.5). Otherwise the left takes its share of the room rounded down, and the right the rest,
      neither below 0 (500 and 200 were 411 and 165, 200 and 500 were 164 and 412, and 72 and 72
      on a 30 PT page were 0 and 0).
    - A top, bottom, header or footer margin above two fifths of the page's height is cut to that
      (800 was 316.8, and 72 on a 100 PT page was 40)."""
    out: dict = {}
    size = page["pageSize"]
    left, right = (style.get(k, page[k]).get("magnitude", 0) for k in ("marginLeft", "marginRight"))
    room = size["width"].get("magnitude", 0) - _DOCS_MIN_TEXT_WIDTH
    if left + right > max(room, 0):
        cut = [k for k in ("marginLeft", "marginRight") if k in named]
        other = right if cut == ["marginLeft"] else left
        if len(cut) == 1 and room >= other:
            out[cut[0]] = room - other
        else:
            out["marginLeft"] = max(0, math.floor(room * left / (left + right)))
            out["marginRight"] = max(0, room - out["marginLeft"])
    most = size["height"].get("magnitude", 0) * 2 / 5
    for key in ("marginTop", "marginBottom", "marginHeader", "marginFooter"):
        if style.get(key, {}).get("magnitude", 0) > most:
            out[key] = most
    return {
        key: ({"magnitude": _docs_number(value)} if value else {}) | {"unit": "PT"}
        for key, value in out.items()
    }


_DOCS_MM = 72 / 25.4
# The paper sizes a page near one takes, width by height in points: measured 2026-11-09, a width
# and height each within 5 PT of one, either way round, were kept as it (611 by 792 as Letter, 596
# by 842 as A4, 1225 by 793 as Tabloid on its side), and 618 by 792 and 540 by 720 were kept as
# sent. Letter, Tabloid, Legal, Statement, Folio, Executive, then A3, A4, A5, B4 and B5.
_DOCS_PAPER = (
    (612, 792),
    (792, 1224),
    (612, 1008),
    (396, 612),
    (612, 936),
    (522, 756),
    *(
        (w * _DOCS_MM, h * _DOCS_MM)
        for w, h in ((297, 420), (210, 297), (148, 210), (250, 353), (176, 250))
    ),
)


def _docs_paper(size: dict) -> dict:
    """A `pageSize`, as the paper size it is near (`_DOCS_PAPER`) when it is near one."""
    width, height = (size[k].get("magnitude", 0) for k in ("width", "height"))
    for w, h in _DOCS_PAPER:
        for across, down in ((w, h), (h, w)):
            if abs(width - across) <= 5 and abs(height - down) <= 5:
                near = {"width": across, "height": down}
                return {k: {"magnitude": _docs_number(near[k]), "unit": "PT"} for k in size}
    return size


# The `fields` paths `updateNamedStyle` refuses as unallowed.
_DOCS_NAMED_UNALLOWED = (
    "textStyle.link",
    "paragraphStyle.headingId",
    "paragraphStyle.namedStyleType",
)


def _docs_update_named_style(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``updateNamedStyle`` on the tab `tabId` names, the first for none, in real's order of
    refusals, measured 2026-11-09: the tab, the fields (`_docs_fields`, then a link, a
    `headingId` or a `namedStyleType` of its paragraph style unallowed, in the order they are
    named), the named style's type, which `fields` must name and the request state, then the
    values (`_docs_text_style`, `_docs_paragraph_style`). Each member named is set, or taken off
    when left unset; `textStyle` or `paragraphStyle` named whole is replaced by what is stated, and
    `*` replaces both (`gdocs.Document.set_named_style`)."""
    tab = _docs_tab(doc, req)
    fields = req.get("fields", "")
    if (
        _docs_fields(fields, _PJ_NAMED_STYLE, ("namedStyleType", "textStyle", "paragraphStyle"))
        is None
    ):
        raise gerr.internal_error()
    star = fields.strip(_DOCS_SPACE) == "*"
    paths = (
        ["namedStyleType", "textStyle", "paragraphStyle"]
        if star
        else [_docs_camel(p) for p in fields.split(",") if p.strip(_DOCS_SPACE)]
    )
    for path in paths:
        if path in _DOCS_NAMED_UNALLOWED:
            raise _EditorRefused(f"Unallowed field: {path}")
    read = req.get("named_style", {})
    if "namedStyleType" not in paths or not read.get("named_style_type"):
        raise _EditorRefused("Named style type is required.")
    named = _docs_enum_name(_DOCS_NAMED_STYLE, read["named_style_type"])
    whole = {p for p in paths if "." not in p}
    text_names = [p.split(".", 1)[1] for p in paths if p.startswith("textStyle.")]
    para_names = [p.split(".", 1)[1] for p in paths if p.startswith("paragraphStyle.")]
    text_read = read.get("text_style", {})
    if "textStyle" in whole:
        text_names = [
            k for k in gdocs.TEXT_STYLE_ORDER if k != "link" and _docs_snake(k) in text_read
        ]
    para_read = read.get("paragraph_style", {})
    if "paragraphStyle" in whole:
        para_names = [
            k
            for k in _DOCS_PARAGRAPH_FIELDS
            if k != "namedStyleType" and _docs_snake(k) in para_read
        ]
    size = text_read.get("font_size", {}).get("magnitude") if "fontSize" in text_names else None
    weight = text_read.get("weighted_font_family", {}).get("weight", 0) or 400
    # measured 2026-11-09: a font size of 0 or 600 and a weight of 150 were each a 500, where
    # `updateTextStyle` keeps a size in bounds and refuses the weight
    if size is not None and not 1 <= size <= 512:
        raise gerr.internal_error()
    if "weightedFontFamily" in text_names and (weight % 100 or not 100 <= weight <= 900):
        raise gerr.internal_error()
    text = _docs_text_style(doc, tab, text_read, text_names)
    para = _docs_paragraph_style(para_read, para_names)
    if paths == ["namedStyleType"]:
        # measured: a request naming the type alone kept the `revisionId`
        doc.unchanged += 1
    doc.set_named_style(tab, named, text, para, "textStyle" in whole, "paragraphStyle" in whole)
    return {}, False


def _docs_create_segment(kind: str):
    """``createHeader`` or ``createFooter``: the tab's header or footer, one empty paragraph,
    answered with its id, its id served as the tab's `documentStyle`'s default. Measured
    2026-11-02: the tab of a `sectionBreakLocation` is refused, then no `type` or
    `HEADER_FOOTER_TYPE_UNSPECIFIED`; one at any index but the first section break's, 0, is a 500
    at this request (measured 2026-11-03), whatever segment it names, and a tab holding one already
    is refused."""
    name = kind.capitalize()

    def create(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
        where = req.get("section_break_location") or {}
        tab = _docs_tab(doc, where)
        # measured 2026-11-03: `FIRST_PAGE`, 0 and below were not specified, and 3 and above,
        # numbers the enum does not declare, unrecognized
        kind_of = req.get("type", 0)
        if kind_of < 1 or kind_of == 2:
            raise _EditorRefused(f"{name} type not specified.")
        if kind_of > 2:
            raise _EditorRefused(f"Unrecognized {kind} type.")
        if where.get("index", 0) != 0:
            raise gerr.internal_error()
        if tab[f"{kind}s"]:
            raise _EditorRefused(f"Default {kind} already exists.")
        made = doc.new_id(gdocs.segment_id)
        tab[f"{kind}s"][made] = gdocs.blank_segment()
        return {f"create{name}": {f"{kind}Id": made}}, False

    return create


def _docs_delete_segment(kind: str):
    """``deleteHeader`` or ``deleteFooter``: the header or footer the id names in the tab the
    request names, the first for none. Measured 2026-11-02: the tab is refused, then an empty id,
    then an id that is not one of that tab's: a footer's id named as a header's, a header's as a
    footer's, and another tab's header among them."""
    name = kind.capitalize()

    def delete(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
        tab = _docs_tab(doc, req)
        made = req.get(f"{kind}_id")
        if not made:
            raise _EditorRefused(f"{name} ID can not be empty.")
        if made not in tab[f"{kind}s"]:
            raise _EditorRefused(f"{name} does not exist.")
        del tab[f"{kind}s"][made]
        return {}, False

    return delete


# The longest tab title, in UTF-16 code units: measured 2026-11-02, 50 `y` or 25 `😀` were taken
# and 51 `y`, 26 `😀` or 25 `é` spelled with a combining mark and a `z` refused.
_DOCS_TAB_TITLE_UNITS = 50


def _docs_find_tab(doc: gdocs.Document, tab_id: str) -> dict | None:
    return next((t for t in doc.flat() if t["tabProperties"]["tabId"] == tab_id), None)


def _docs_shown(tab_id: str) -> str:
    """A tab id the Doc lacks as real names it in `deleteTab`'s, a parent's and a moved tab's
    refusals: measured 2026-11-02, once `t.0` was deleted each named it as nothing at all."""
    return "" if tab_id == "t.0" else tab_id


# How deep tabs nest: measured 2026-11-02, a tab under one at `nestingLevel` 2 was refused,
# added or moved there, and so was a move that took a child tab past it.
_DOCS_TAB_DEPTH = 2
_DOCS_TOO_DEEP = "Tab nesting level cannot exceed 3."


def _docs_depth(doc: gdocs.Document, tab: dict | None) -> int:
    """The `nestingLevel` a tab under ``tab`` takes, 0 at the root."""
    if tab is None:
        return 0
    return 1 + next(d for t, _, d in gdocs.walk(doc.tabs) if t is tab)


def _docs_height(tab: dict) -> int:
    """How many levels of child tabs hang under ``tab``."""
    return max((1 + _docs_height(t) for t in tab["childTabs"]), default=0)


def _docs_title(doc: gdocs.Document, title: str, own: dict | None = None) -> str | None:
    """A title as sent, refused too long or as another tab's, and then as real stores it, None
    for the default (`gdocs.tab_title`). Measured 2026-11-02: the same string is taken anywhere in
    the tree, while one differing in case, in a trailing space, in width or in normalization form
    is not; measured 2026-11-03, the length is the title's as sent, and a lone untitled first
    tab's `Tab 1` (`gdocs.new_tab`) is taken only against an update."""
    if gdocs.utf16_len(title) > _DOCS_TAB_TITLE_UNITS:
        raise _EditorRefused("The tab title cannot be longer than 50 characters.")
    for t in doc.flat():
        if t is not own and t["tabProperties"]["title"] == title:
            if own is not None or not t.get("untitled"):
                raise _EditorRefused("Tab title must be unique")
    return gdocs.tab_title(title)


def _docs_add_tab(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``addDocumentTab``: a blank tab (`gdocs.new_tab`) under the parent it names, before the
    sibling at its index, or last, answered with its properties. A title it does not state is
    `gdocs.default_title`'s, and so is one blank or taken once `_docs_title` reads it; a
    `nestingLevel` is ignored. The order of refusals, measured 2026-11-02: a `tabId` stated, the
    title's length, a title another tab has, the icon, the parent, a parent too deep
    (`_DOCS_TAB_DEPTH`), then the index."""
    props = req.get("tab_properties", {})
    if props.get("tab_id"):
        raise _EditorRefused("Tab ID should not be specified in the properties when adding a tab.")
    title = _docs_title(doc, props["title"]) if props.get("title") else None
    if props.get("icon_emoji") and not gdocs.is_icon(props["icon_emoji"]):
        raise _EditorRefused("Not a valid emoji input.")
    parent = None
    if props.get("parent_tab_id"):
        parent = _docs_find_tab(doc, props["parent_tab_id"])
        if parent is None:
            shown = _docs_shown(props["parent_tab_id"])
            raise _EditorRefused(f'A parent tab with ID "{shown}" does not exist.')
        if _docs_depth(doc, parent) > _DOCS_TAB_DEPTH:
            raise _EditorRefused(_DOCS_TOO_DEEP)
    index = props.get("index")
    if index is not None:
        _docs_tab_index(index, parent, len(doc.siblings(parent)))
    titles = [t["tabProperties"]["title"] for t in doc.flat()]
    # measured 2026-11-03: `a\nb` beside `a b`, and `Tab 1` beside an untitled first tab, took
    # the default
    if title is None or title in titles:
        title = gdocs.default_title(titles, len(titles) + 1)
    tab = gdocs.new_tab(doc.new_id(gdocs.tab_id), title)
    # measured 2026-11-03: once a Doc holds a second tab its first holds `Tab 1` as its own
    for t in doc.flat():
        t.pop("untitled", None)
    if props.get("icon_emoji"):
        tab["tabProperties"]["iconEmoji"] = props["icon_emoji"]
    doc.place(tab, parent, index, new=True)
    return {"addDocumentTab": {"tabProperties": _docs_placed(doc, tab)}}, False


def _docs_tab_index(index: int, parent: dict | None, count: int) -> None:
    if index < 0:
        raise _EditorRefused("Index must be non-negative.")
    if index > count:
        raise _EditorRefused(
            "Index must be less than or equal to the number of tabs within the specified parent "
            "tab."
            if parent is not None
            else "Index must be less than or equal to the number of unnested tabs."
        )


def _docs_placed(doc: gdocs.Document, tab: dict) -> dict:
    parent, depth = next((p, d) for t, p, d in gdocs.walk(doc.tabs) if t is tab)
    index = next(i for i, t in enumerate(doc.siblings(parent)) if t is tab)
    return _docs_tab_properties(tab, parent, index, depth)


def _docs_delete_tab(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``deleteTab``: the tab and its child tabs. Measured 2026-11-02: a Doc's only tab is refused,
    while deleting the only root tab, children and all, left a blank tab titled `Tab 1` under a new
    id."""
    named = req.get("tab_id")
    if not named:
        raise _EditorRefused("Tab ID not specified.")
    tab = _docs_find_tab(doc, named)
    if tab is None:
        raise _EditorRefused(f"A tab with ID {_docs_shown(named)} does not exist.")
    if len(doc.flat()) == 1:
        raise _EditorRefused("Cannot delete the only tab in a document.")
    doc.remove(tab)
    if not doc.tabs:
        doc.tabs.append(gdocs.new_tab(doc.new_id(gdocs.tab_id), gdocs.default_title((), 1)))
    return {}, False


# What `fields` may name of `TabProperties`: measured 2026-11-02, a `nestingLevel` or `tabId`
# named was taken and changed nothing.
_DOCS_TAB_FIELDS = ("tabId", "title", "parentTabId", "index", "nestingLevel", "iconEmoji")


# What a `fields` name is trimmed of: measured 2026-11-03, every character Python counts as white
# space but U+001C to U+001F, which were refused as part of the name.
_DOCS_SPACE = "".join(
    c for c in map(chr, range(0x3001)) if c.isspace() and c not in "\x1c\x1d\x1e\x1f"
)


def _docs_fields(
    fields: str,
    message: protojson.Message,
    every: tuple[str, ...],
    whole: tuple[str, ...] = (),
    unallowed: tuple[str, ...] = (),
) -> list[str] | None:
    """The fields of ``message`` a request's `fields` mask names, read as real reads it, or None for
    the mask real answers with a 500. Measured 2026-11-02 and 2026-11-03 on
    `updateDocumentTabProperties`, `updateTextStyle` and `updateParagraphStyle`: `*` alone names
    ``every`` field; names are split at commas, trimmed, and empty ones dropped, so `title,` and
    ` , ` are read while `,` is refused as naming no field. A name is split at dots with empty parts
    dropped (`title.` and `.title` name `title`), and one of dots alone is the 500 before any name
    is read (`nope,.`). Its first part read as `_docs_snake` spells it must be a field's
    `snake_case` name (`TITLE`, `parentTABId`, `fontSIZE` and `icon_emoji` were taken, `TItle`,
    `_title` and `PARENTTABID` refused), and is refused by that spelling (`fooBar` as `foo_bar`,
    `textStyle.bold` as `text_style`), and, in the order the mask names them, one of ``whole`` with
    a path is refused as a subfield (`borderTop.width,nope` for `borderTop`, `nope,borderTop.width`
    for `nope`). Every name is checked before any path into a field, which must name the fields of
    the messages it walks (`link.url` and `foregroundColor.color.rgbColor.red` were taken) and is
    refused by the field's name and the rest so spelt: up to the name a message or a wrapper lacks
    (`icon_emoji.x` as `iconEmoji.x`, `link.nope.z` as `link.nope`, `bold.x.Y` as `bold.x`), and
    through every name after a string, number or enum (`title.x.Y` as `title.x.y`,
    `fontSize.magnitude.x.y`). A path names its first field whole. Measured 2026-11-09 on the
    table, section and document style requests, a field of ``unallowed`` is refused as it is
    reached among the names (`tableHeader,nope` for `tableHeader`, `nope,tableHeader` for
    `nope`)."""
    if fields == "*":
        return list(every)
    parts = fields.split(",")
    if not "".join(parts):
        raise _EditorRefused(
            "At least one field must be listed in 'fields'. (Use '*' to indicate all fields.)"
        )
    paths = [
        [_docs_snake(n) for n in p.split(".") if n]
        for p in (p.strip(_DOCS_SPACE) for p in parts)
        if p
    ]
    if not all(paths):
        return None
    known = {f.name: f for f in message.fields}
    read = []
    for path in paths:
        if path[0] not in known:
            raise _EditorRefused(f"Invalid field: {path[0]}")
        if known[path[0]].json_name in unallowed:
            raise _EditorRefused(f"Unallowed field: {known[path[0]].json_name}")
        if len(path) > 1 and known[path[0]].json_name in whole:
            raise _EditorRefused(
                f"Unallowed field: cannot request subfield of {known[path[0]].json_name}"
            )
        read.append(path)
    for path in read:
        field, spelt = known[path[0]], [known[path[0]].json_name]
        for i, name in enumerate(path[1:], 1):
            if field.kind != "message" and not field.wrapper:
                raise _EditorRefused(f"Invalid field: {'.'.join([*spelt, *path[i:]])}")
            inner = {f.name: f for f in field.message.fields} if field.kind == "message" else {}
            if name not in inner:
                raise _EditorRefused(f"Invalid field: {'.'.join([*spelt, name])}")
            field = inner[name]
            spelt.append(field.json_name)
    return [known[path[0]].json_name for path in read]


def _docs_camel(path: str) -> str:
    """A `fields` path as the JSON names it spells, its empty parts dropped (`_docs_fields`)."""
    parts = [_docs_snake(n) for n in path.strip(_DOCS_SPACE).split(".") if n]
    return ".".join(re.sub(r"_([a-z0-9])", lambda m: m[1].upper(), p) for p in parts)


def _docs_snake(name: str) -> str:
    """A name as real reads one in a mask: `_` before each ASCII capital but the first, unless it
    follows `_`, or follows another capital with no small ASCII letter after it; then its ASCII
    letters in lower case (`tabProperties` as `tab_properties`, `TItle` as `t_itle`, `parentTABId`
    as `parent_tab_id`, `tab Id` as `tab _id`, `ÉCLAT` as `É_clat`)."""
    out = ""
    for i, c in enumerate(name):
        if "A" <= c <= "Z" and i and name[i - 1] != "_":
            after = name[i + 1 : i + 2]
            if not "A" <= name[i - 1] <= "Z" or "a" <= after <= "z" and after:
                out += "_"
        out += c.lower() if "A" <= c <= "Z" else c
    return out


def _docs_update_tab(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``updateDocumentTabProperties``: the properties `fields` names (`_docs_fields`), in
    real's order of refusals, measured 2026-11-02: no field, a field `TabProperties` lacks, no
    `tabId`, a tab the Doc lacks, the title (missing, too long or another tab's), the icon, a parent
    the Doc lacks, a parent too deep for the tab (`_DOCS_TAB_DEPTH`), an index below zero, then,
    measured 2026-11-03, an index past the parent's tabs, the tab itself as its parent, one of its
    own children, then a parent too deep for its children. A title is stored as `_docs_title` reads
    it.

    A parent named puts the tab last under it, the root for none, and an index stated moves it
    before the sibling there (`gdocs.Document.place`): measured, `*` with no index moved a tab to
    the root's end, and with no title was refused for the title. An empty icon removes it."""
    fields = req.get("fields", "")
    if not fields:
        raise _EditorRefused(
            "At least one field must be listed in 'fields'. (Use '*' to indicate all fields.)"
        )
    names = _docs_fields(fields, _PJ_DOCS("docs.TabProperties"), _DOCS_TAB_FIELDS)
    if names is None:
        raise gerr.internal_error()
    props = req.get("tab_properties", {})
    if not props.get("tab_id"):
        raise _EditorRefused("Tab ID must be specified in the properties.")
    tab = _docs_find_tab(doc, props["tab_id"])
    if tab is None:
        # sic: measured 2026-11-02, `At` and the id as sent unless the fields name `parentTabId`
        if "parentTabId" in names:
            raise _EditorRefused(f"A tab with ID {_docs_shown(props['tab_id'])} does not exist.")
        raise _EditorRefused(f"At tab with ID {props['tab_id']} does not exist.")
    title = tab["tabProperties"]["title"]
    if "title" in names:
        if not props.get("title"):
            raise _EditorRefused(
                "Invalid `tabProperties.title`. Must provide a valid non-empty string."
            )
        stored = _docs_title(doc, props["title"], own=tab)
        others = [t["tabProperties"]["title"] for t in doc.flat() if t is not tab]
        # measured 2026-11-03: a blank title took the default for the tabs the Doc has, and `a\nb`
        # beside `a b` left the title as it was
        if stored is None:
            title = gdocs.default_title(others, len(others) + 1)
        elif stored not in others:
            title = stored
    emoji = props.get("icon_emoji", "")
    if "iconEmoji" in names and emoji and not gdocs.is_icon(emoji):
        raise _EditorRefused("Not a valid emoji input.")
    parent = doc.parent(tab)
    if "parentTabId" in names:
        named = props.get("parent_tab_id", "")
        parent = _docs_find_tab(doc, named) if named else None
        if named and parent is None:
            raise _EditorRefused(f'A parent tab with ID "{_docs_shown(named)}" does not exist.')
        if _docs_depth(doc, parent) > _DOCS_TAB_DEPTH:
            raise _EditorRefused(_DOCS_TOO_DEEP)
    # sic: real reads the root as `t.0` for a tab that ends at the root. Measured
    # 2026-11-03: with `t.0` deleted, an update of a root tab or a move to the root was a 500
    # whatever it named, and with `t.0` under a tab, any update of that tab was refused as a move
    # under its own child; a tab under a named parent was updated
    if parent is None:
        root = _docs_find_tab(doc, "t.0")
        if root is None:
            raise gerr.internal_error()
        if any(t is root for t, *_ in gdocs.walk(tab["childTabs"])):
            raise _EditorRefused(
                "Cannot move a parent tab to be nested under one of its own child tabs."
            )
    # measured 2026-11-03: `index` named with none stated moved the tab last among its siblings
    index = props.get("index", len(doc.siblings(parent))) if "index" in names else None
    if index is not None:
        _docs_tab_index(index, parent, len(doc.siblings(parent)))
    if "parentTabId" in names:
        if parent is tab:
            raise _EditorRefused("Cannot make a tab a child of itself.")
        if parent is not None and any(t is parent for t, *_ in gdocs.walk(tab["childTabs"])):
            raise _EditorRefused(
                "Cannot move a parent tab to be nested under one of its own child tabs."
            )
    if "parentTabId" in names and _docs_depth(doc, parent) + _docs_height(tab) > _DOCS_TAB_DEPTH:
        raise _EditorRefused(
            f"Moving this tab will cause child tabs to exceed the tab nesting level. {_DOCS_TOO_DEEP}"
        )
    before = (dict(tab["tabProperties"]), _docs_placed(doc, tab))
    if title != tab["tabProperties"]["title"]:
        tab["tabProperties"]["title"] = title
        tab.pop("untitled", None)
    if "iconEmoji" in names:
        tab["tabProperties"].pop("iconEmoji", None)
        if emoji:
            tab["tabProperties"]["iconEmoji"] = emoji
    if "parentTabId" in names or index is not None:
        doc.place(tab, parent, index)
    # measured 2026-11-03: a title, an index or a parent the tab already had kept the
    # `revisionId`, and an icon it already had, or a title left as it was for being another
    # tab's once rewritten, moved it
    sent = "title" not in names or props["title"] == before[0]["title"]
    if (
        sent
        and "iconEmoji" not in names
        and before == (tab["tabProperties"], _docs_placed(doc, tab))
    ):
        doc.unchanged += 1
    return {}, False


# --- text and paragraph styles, lists and named ranges --------------------------------------------
#
# Measured against the live Docs v1 API on 2026-11-02 and 2026-11-03, as a Workspace member writing
# Docs it owns. How each request changes the document is `gdocs.Body`'s and `gdocs.Document`'s;
# this section is the request, its refusals and the values it keeps.

_PJ_TEXT_STYLE = _PJ_DOCS("docs.TextStyle")
_PJ_PARAGRAPH_STYLE = _PJ_DOCS("docs.ParagraphStyle")
_DOCS_UNIT = _docs_enum("Unit")
_DOCS_BASELINE = _docs_enum("BaselineOffset")
_DOCS_NAMED_STYLE = _docs_enum("NamedStyleType")
_DOCS_PARAGRAPH_ENUMS = {
    "alignment": _docs_enum("ParagraphStyle.Alignment"),
    "direction": _docs_enum("ContentDirection"),
    "spacingMode": _docs_enum("ParagraphStyle.SpacingMode"),
}
_DOCS_DASH = _docs_enum("DashStyle")


def _docs_enum_name(enum: protojson.Enum, value: int) -> str:
    """``value``'s name in ``enum``. The proto layer reads any number, and real answered an
    alignment, named style, direction or spacing mode of 99 or -1 with a 500, measured
    2026-11-04."""
    if not 0 <= value < len(enum.names):
        raise gerr.internal_error()
    return enum.names[value]


def _docs_float(value: float) -> float | int:
    """A float as real serves one it keeps in 32 bits: the shortest decimal that reads back as
    the same float (0.1 kept as 0.101960786 once a colour's 255ths round it), whole as an
    integer."""
    kept = struct.unpack("<f", struct.pack("<f", value))[0]
    for digits in range(1, 10):
        text = f"{kept:.{digits}g}"
        if struct.unpack("<f", struct.pack("<f", float(text)))[0] == kept:
            break
    return _docs_number(float(text))


def _docs_number(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def _docs_color(read: dict) -> dict:
    """An `OptionalColor` as real keeps it, measured 2026-11-03: each channel to the nearest
    255th (0.5 as 0.5019608, 0.001 as 0), a zero channel left out, and no `color` at all a
    transparent `{}`. A channel below 0 or above 1 is a 500 at its request."""
    if "color" not in read:
        return {}
    rgb = read["color"].get("rgb_color", {})
    out = {}
    for channel in ("red", "green", "blue"):
        value = rgb.get(channel, 0)
        if not 0 <= value <= 1:
            raise gerr.internal_error()
        if kept := math.floor(value * 255 + 0.5) / 255:
            out[channel] = _docs_float(kept)
    return {"color": {"rgbColor": out}}


def _docs_dimension(read: dict) -> dict:
    """A `Dimension`, refused by its unit unless points: measured 2026-11-03, a missing unit was
    `Unsupported dimension unit:UNIT_UNSPECIFIED`, and a magnitude of 0 is left out."""
    unit = read.get("unit", 0)
    if unit != _DOCS_UNIT.names.index("PT"):
        named = _DOCS_UNIT.names[unit] if 0 <= unit < len(_DOCS_UNIT.names) else "UNRECOGNIZED"
        raise _EditorRefused(f"Unsupported dimension unit:{named}")
    magnitude = read.get("magnitude", 0)
    return ({"magnitude": _docs_number(magnitude)} if magnitude else {}) | {"unit": "PT"}


# The font sizes a run may take: measured 2026-11-03, 0, -3 and 0.5 were kept as 1, and 513 and
# 2,000 as 512.
_DOCS_FONT_SIZES = (1, 512)


def _docs_text_style(doc: gdocs.Document, tab: dict, read: dict, names: list[str]) -> dict:
    """The `TextStyle` members ``names`` names as `gdocs.Body.style` takes them, one the request
    leaves unset None, in real's order of refusals, measured 2026-11-03 whatever order `fields`
    lists them in: a colour's channel out of bounds (`_docs_color`), the font size's unit, an empty
    font family, a link `_docs_link` refuses (of no kind, to a heading the tab it is looked for in
    lacks, to a tab the Doc lacks or to a bookmark), then a weight that is not a hundred from 100
    to 900. A baseline offset outside its enum was refused too, measured 2026-11-04 on its own, and
    is checked last. A member `fields` does not name is not read: a font size with no unit beside
    `fields=bold` was taken.

    A weight of 0 is 400, and a link to an empty URL changes nothing: measured, it left a link
    as it was."""
    named = set(names)
    out: dict = {}
    for key in ("backgroundColor", "foregroundColor"):
        if key in named:
            proto = _docs_snake(key)
            out[key] = _docs_color(read[proto]) if proto in read else None
    if "fontSize" in named:
        out["fontSize"] = None
        if "font_size" in read:
            size = _docs_dimension(read["font_size"])
            low, high = _DOCS_FONT_SIZES
            magnitude = min(max(size.get("magnitude", 0), low), high)
            out["fontSize"] = {"magnitude": magnitude, "unit": "PT"}
    family = read.get("weighted_font_family") if "weightedFontFamily" in named else None
    if family is not None and not family.get("font_family"):
        raise _EditorRefused(
            "Invalid `weighted_font_family.font_family`. Must provide a valid font family."
        )
    if "link" in named:
        link = _docs_link(doc, tab, read["link"]) if "link" in read else None
        if link != {}:
            out["link"] = link
    if "weightedFontFamily" in named:
        out["weightedFontFamily"] = None
        if family is not None:
            weight = family.get("weight", 0) or 400
            if weight % 100 or not 100 <= weight <= 900:
                raise _EditorRefused(f"Font weight {weight} is not supported.")
            out["weightedFontFamily"] = {"fontFamily": family["font_family"], "weight": weight}
    for key in ("bold", "italic", "underline", "strikethrough", "smallCaps"):
        if key in named:
            out[key] = read.get(_docs_snake(key))
    if "baselineOffset" in named:
        offset = read.get("baseline_offset", 0)
        if not 0 <= offset < len(_DOCS_BASELINE.names):
            raise _EditorRefused("The baseline offset type is invalid: null")
        out["baselineOffset"] = _DOCS_BASELINE.names[offset] if offset else None
    return {k: out[k] for k in gdocs.TEXT_STYLE_ORDER if k in out}


def _docs_link(doc: gdocs.Document, tab: dict, read: dict) -> dict:
    """A `Link` as it is kept (`gdocs._linked` serves it), or `{}` for one that changes nothing.
    Measured 2026-11-03: a link of no kind is refused; a heading is looked for in the tab its
    `heading` names, refused first as a tab when the Doc lacks it, else in the range's own tab,
    so a heading of another tab named by its id alone is refused; a bookmark (none here holds
    one) or a tab the Doc lacks is refused; and a link to an empty URL changes nothing. Measured
    2026-11-04: an empty tab id links to `t.0`, from another tab too, and is refused once `t.0` is
    deleted."""
    if not read:
        raise _EditorRefused("Links must include at least one type.")
    if "url" in read:
        return {"url": read["url"]} if read["url"] else {}
    if "heading_id" in read or "heading" in read:
        heading = read.get("heading_id", read.get("heading", {}).get("id", ""))
        named = read.get("heading", {}).get("tab_id")
        target = doc.tab(named) if named else tab
        if target is None:
            raise _EditorRefused("Cannot link to an invalid tab ID.")
        if not heading or heading not in doc.heading_ids(target):
            raise _EditorRefused("Cannot link to an invalid heading ID.")
        return {"heading": {"id": heading, "tabId": target["tabProperties"]["tabId"]}}
    if "bookmark_id" in read or "bookmark" in read:
        raise _EditorRefused("Cannot link to an invalid bookmark ID.")
    tab_id = read["tab_id"] or "t.0"
    if doc.tab(tab_id) is None:
        raise _EditorRefused("Cannot link to an invalid tab ID.")
    return {"tabId": tab_id}


def _docs_update_text_style(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``updateTextStyle``, in real's order of refusals, measured 2026-11-03: the range
    (`_docs_range`), the fields (`_docs_fields`), the values (`_docs_text_style`), an empty range,
    then a body's section break at 0. A range may start or end between the halves of a character
    outside the BMP (`gdocs.Body.cut`) and may hold the segment's last newline.

    A request taking links off, `*` with no link or `link` named with none, takes them off only
    when the range starts on linked text: measured, `*` with no link over a link's whole range or
    from its start took it off, and over a range starting before it kept it, as `link` named with
    none over a range starting before it did, measured 2026-11-04."""
    tab, _, body, start, end = _docs_range(
        doc, req.get("range"), "Update text style requests must specify an update range."
    )
    names = _docs_fields(req.get("fields", ""), _PJ_TEXT_STYLE, gdocs.TEXT_STYLE_ORDER)
    if names is None:
        raise gerr.internal_error()
    read = req.get("text_style", {})
    stated = _docs_text_style(doc, tab, read, names)
    if start == end:
        raise _EditorRefused("The range should not be empty.")
    if start < body.base:
        raise _EditorRefused("Cannot operate on the first section break in the document.")
    reach = body.reach(body.cut(start), body.cut(end))
    if reach is None:
        return {}, False
    first, last = reach
    if stated.get("link", {}) is None and "link" not in body.chars[first][1]:
        stated.pop("link")
    body.style(first, last, stated)
    return {}, False


def _docs_paragraph_style(read: dict, names: list[str]) -> dict:
    """The `ParagraphStyle` members ``names`` names as `gdocs.Body.style_paragraph` takes them,
    one the request leaves unset None, each refused as real refuses it, measured 2026-11-03 one at
    a time: a named style left unset, a member's unit (`_docs_dimension`), a border's dash style
    left unset; checked in the order `fields` names them. Measured 2026-11-04 the same way, a dash
    style outside its enum was refused too, and another member outside its enum is
    `_docs_enum_name`'s. A border is stated whole: one stating its width alone was refused for its
    padding's unit."""
    out: dict = {}
    for key in names:
        value = read.get(_docs_snake(key))
        if key == "namedStyleType":
            if not value:
                raise _EditorRefused("Named style property is not inherited and cannot be cleared.")
            out[key] = _docs_enum_name(_DOCS_NAMED_STYLE, value)
        elif value is None or key in _DOCS_PARAGRAPH_ENUMS and not value:
            out[key] = None
        elif key in _DOCS_PARAGRAPH_ENUMS:
            out[key] = _docs_enum_name(_DOCS_PARAGRAPH_ENUMS[key], value)
        elif key == "lineSpacing":
            out[key] = _docs_float(value)
        elif key in _DOCS_DIMENSIONS:
            out[key] = _docs_dimension(value)
        elif key.startswith("border"):
            border = {
                "color": _docs_color(value.get("color", {})),
                "width": _docs_dimension(value.get("width", {})),
                "padding": _docs_dimension(value.get("padding", {})),
            }
            dash = value.get("dash_style", 0)
            if not 0 < dash < len(_DOCS_DASH.names):
                named = "UNRECOGNIZED" if dash else "DASH_STYLE_UNSPECIFIED"
                raise _EditorRefused(f"The dash style is invalid: {named}")
            out[key] = border | {"dashStyle": _DOCS_DASH.names[dash]}
        elif key == "shading":
            out[key] = {"backgroundColor": _docs_color(value.get("background_color", {}))}
        else:
            out[key] = value
    return out


_DOCS_DIMENSIONS = ("spaceAbove", "spaceBelow", "indentFirstLine", "indentStart", "indentEnd")

# What `*` names of `ParagraphStyle`: measured 2026-11-03, it reached `namedStyleType` and not
# `headingId` or `tabStops`, which named are refused.
_DOCS_PARAGRAPH_FIELDS = tuple(k for k in gdocs.PARAGRAPH_STYLE_ORDER if k != "headingId")
_DOCS_READ_ONLY = ("headingId", "tabStops")
_DOCS_BORDERS = tuple(k for k in gdocs.PARAGRAPH_STYLE_ORDER if k.startswith("border"))


def _docs_update_paragraph_style(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``updateParagraphStyle`` on every paragraph the range overlaps, in real's order of refusals,
    measured 2026-11-03: the range (`_docs_range`, no range refused as an index missing), the fields
    (`_docs_fields`), a read-only one, the values (`_docs_paragraph_style`), then a body's section
    break at 0. An empty range styles the paragraph it falls in."""
    _, _, body, start, end = _docs_range(doc, req.get("range"), _DOCS_NO_INDEX)
    names = _docs_fields(
        req.get("fields", ""), _PJ_PARAGRAPH_STYLE, _DOCS_PARAGRAPH_FIELDS, _DOCS_BORDERS
    )
    if names is None:
        raise gerr.internal_error()
    for name in names:
        if name in _DOCS_READ_ONLY:
            raise _EditorRefused(f"Unallowed field: {name}")
    stated = _docs_paragraph_style(req.get("paragraph_style", {}), names)
    if start < body.base:
        raise _EditorRefused("Cannot operate on the first section break in the document.")
    for p in body.paragraphs_in(start, end):
        body.style_paragraph(p, stated)
    return {}, False


# How deep a list nests: nine levels, 0 to 8.
_DOCS_LIST_DEPTH = 8


def _docs_create_bullets(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``createParagraphBullets`` on every paragraph the range overlaps, in real's order of
    refusals, measured 2026-11-03: the range (`_docs_range`, no range refused as an index
    missing), an unset or unknown preset, then a body's section break at 0. An empty range lists
    the paragraph it falls in.

    Measured too: each paragraph not in a list loses the tabs it opens with, its level being how
    many it had, at most 8, less the fewest any such paragraph of the request had, and a list the
    request makes indents each level that many tabs further (paragraphs of seven tabs made a list
    indented 252 PT more than its preset). The paragraphs join the list of the paragraph right
    before them (`gdocs.Body.follows_paragraph`; measured 2026-11-03, two cells listed one after
    the other in one preset made two lists) when that list's glyphs are the preset's, else the
    list of the first of them already in one, which takes the preset whole, else one new list. A
    paragraph is indented as its list's level is. Measured 2026-11-04, a paragraph already in a
    list keeps its level and its tabs: one of another list joined the first one's list, and that
    other list kept its glyphs."""
    tab, _, body, start, end = _docs_range(doc, req.get("range"), _DOCS_NO_INDEX)
    preset = req.get("bullet_preset", 0)
    if not 0 < preset < len(_DOCS_BULLET_PRESET.names):
        named = _DOCS_BULLET_PRESET.names[0] if preset == 0 else "UNRECOGNIZED"
        raise _EditorRefused(f"Unknown bullet preset {named}")
    if start < body.base:
        raise _EditorRefused("Cannot operate on the first section break in the document.")
    paragraphs = body.paragraphs_in(start, end)
    owns = [body.bullet(p) for p in paragraphs]
    tabs = [
        0 if own is not None else min(body.leading_tabs(p), _DOCS_LIST_DEPTH)
        for p, own in zip(paragraphs, owns)
    ]
    fewest = min((n for n, own in zip(tabs, owns) if own is None), default=0)
    levels = _docs_shifted(_DOCS_PRESETS[_DOCS_BULLET_PRESET.names[preset]], fewest)
    before = body.bullet(paragraphs.start - 1) if body.follows_paragraph(paragraphs.start) else None
    made = next((own["listId"] for own in owns if own is not None), None)
    if made is not None:
        tab["lists"][made]["listProperties"]["nestingLevels"] = levels
    if before is not None and _docs_glyphs(_docs_levels(tab, before["listId"])) == _docs_glyphs(
        levels
    ):
        made = before["listId"]
    made = made or doc.new_list(tab, levels)
    for p, n, own in zip(paragraphs, tabs, owns):
        level = n - fewest if own is None else own.get("nestingLevel", 0)
        at = _docs_levels(tab, made)[level]
        body.set_bullet(p, made, level, (at["indentFirstLine"], at["indentStart"]))
    return {}, False


def _docs_levels(tab: dict, list_id: str) -> list[dict]:
    return tab["lists"][list_id]["listProperties"]["nestingLevels"]


def _docs_shifted(levels: list[dict], tabs: int) -> list[dict]:
    """A preset's levels, each indented ``tabs`` tabs of 36 PT further."""
    out = copy.deepcopy(levels)
    for level in out:
        for key in ("indentFirstLine", "indentStart"):
            level[key]["magnitude"] += 36 * tabs
    return out


def _docs_glyphs(levels: list[dict]) -> list[dict]:
    return [{k: v for k, v in lv.items() if not k.startswith("indent")} for lv in levels]


def _docs_delete_bullets(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``deleteParagraphBullets`` on every paragraph the range overlaps (`gdocs.Body.drop_bullet`),
    in real's order of refusals, measured 2026-11-03: the range (`_docs_range`, no range refused as
    an index missing), then a body's section break at 0. The list stays in the tab's `lists`."""
    _, _, body, start, end = _docs_range(doc, req.get("range"), _DOCS_NO_INDEX)
    if start < body.base:
        raise _EditorRefused("Cannot operate on the first section break in the document.")
    for p in body.paragraphs_in(start, end):
        body.drop_bullet(p)
    return {}, False


# The longest name a named range takes, in UTF-16 code units: measured 2026-11-02 and 2026-11-03,
# 256 `y` or 128 `😀` were taken and 257 `x` or 129 `😀` refused, as was an empty one.
_DOCS_RANGE_NAME_UNITS = 256


def _docs_create_named_range(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``createNamedRange``: a range of the tab and segment it names (`gdocs.Document.add_range`),
    answered with its id, in real's order of refusals, measured 2026-11-02 and 2026-11-03: the
    range (`_docs_range`, no range refused as an index missing), the name's length, then an empty
    range. A range may start at 0, end between the halves of a character outside the BMP and hold
    the segment's last newline."""
    tab, _, body, start, end = _docs_range(doc, req.get("range"), _DOCS_NO_INDEX)
    name = req.get("name", "")
    if not 0 < gdocs.utf16_len(name) <= _DOCS_RANGE_NAME_UNITS:
        raise _EditorRefused("Invalid name: must be at least 1 and no more than 256 characters.")
    if start == end:
        raise _EditorRefused("Invalid range: endIndex must be strictly greater than startIndex.")
    made = doc.add_range(tab, req["range"].get("segment_id", ""), name, start, end)
    return {"createNamedRange": {"namedRangeId": made}}, False


def _docs_named_tabs(doc: gdocs.Document, req: dict) -> list[dict]:
    """The tabs a named range request reaches: every tab, or those its `tabsCriteria` lists.
    Measured 2026-11-03, a `tabsCriteria` naming a tab the Doc lacks is a 500 at its request, and
    one listing none reaches none."""
    tabs = doc.flat()
    if "tabs_criteria" not in req:
        return tabs
    named = req["tabs_criteria"].get("tab_ids", [])
    if any(doc.tab(n) is None or not n for n in named):
        raise gerr.internal_error()
    return [t for t in tabs if t["tabProperties"]["tabId"] in named]


def _docs_named(tabs: list[dict], req: dict, name_key: str) -> list:
    """Each named range a request names by its id or its name, with its tab, among ``tabs``."""
    if "named_range_id" in req:
        wanted = req["named_range_id"]
        key = "namedRangeId"
    else:
        wanted, key = req[name_key], "name"
    return [(t, n) for t in tabs for n in t["namedRanges"] if n[key] == wanted]


def _docs_delete_named_range(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``deleteNamedRange`` by id, or every named range of a name, in every tab or those its
    `tabsCriteria` lists (`_docs_named_tabs`). Measured 2026-11-02 and 2026-11-03: neither set is
    refused, then the tabs, then an empty name is a 500, and an id or a name none has is refused,
    but a `tabsCriteria` listing no tab changes nothing and keeps the `revisionId`; a name in
    another tab than the ones listed is none."""
    if "named_range_id" not in req and "name" not in req:
        raise _EditorRefused("ID or name for named range must be set.")
    tabs = _docs_named_tabs(doc, req)
    if req.get("name") == "":
        raise gerr.internal_error()
    found = _docs_named(tabs, req, "name")
    if not found and "tabs_criteria" in req and not tabs:
        doc.unchanged += 1
        return {}, False
    if not found and "named_range_id" in req:
        raise _EditorRefused(f"No named range with ID: {req['named_range_id']}")
    if not found:
        raise _EditorRefused(f"No named range with name: {req['name']}")
    for tab, named in found:
        tab["namedRanges"].remove(named)
        doc.forget(tab, named)
    return {}, False


def _docs_replace_named_range(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``replaceNamedRangeContent``: the text of each named range of the id or name, in every tab
    or those its `tabsCriteria` lists (`_docs_named_tabs`), replaced by ``text``
    (`gdocs.Document.replace_range`). Measured 2026-11-02 and 2026-11-03: neither set is refused,
    then the tabs; an id none has is refused while a name none has changes nothing and keeps the
    `revisionId`, and an empty name is taken; a range holding its segment's last newline or part
    of a table (`gdocs.Body.cuts_table`) is refused naming its id, and so is one starting before
    the body's first index or with either end between the halves of a character outside the BMP,
    measured 2026-11-04; one holding a whole table replaces it; the characters `insertText` strips
    are stripped, and one in `gdocs.UNWRITABLE` fails the batch once every request is read."""
    if "named_range_id" not in req and "named_range_name" not in req:
        raise _EditorRefused("Must specify a named range reference")
    tabs = _docs_named_tabs(doc, req)
    found = _docs_named(tabs, req, "named_range_name")
    if not found and "named_range_id" in req:
        raise _EditorRefused(f"No named range with ID {req['named_range_id']}")
    if not found:
        doc.unchanged += 1
        return {}, False
    sent = req.get("text", "")
    text = gdocs.STRIPPED.sub("", sent)
    for tab, named in found:
        for r in named["ranges"]:
            body = doc.open(tab, r.get("segmentId", ""))
            ends = (r["startIndex"], r["endIndex"])
            if (
                r["endIndex"] >= body.end
                or any(i < body.base or body.position(i) is None for i in ends)
                or body.cuts_table(body.floor(r["startIndex"]), body.floor(r["endIndex"] - 1) + 1)
            ):
                raise _EditorRefused(
                    f"Named range with ID {named['namedRangeId']} contains content that cannot "
                    "be replaced."
                )
        doc.replace_range(tab, named, text, sent)
    return {}, bool(gdocs.UNWRITABLE.search(text))


# --- requests naming what a Doc here never holds --------------------------------------------------
#
# Nothing here makes a suggestion (`SUGGEST` is refused), a comment, a dropdown or an inline or
# positioned object, and a corpus states none, so every id these requests name names nothing. Each is
# checked as real checks it before its lookup, and then refused as real refuses an id naming
# nothing.


def _docs_not_found(message: str) -> gerr.GoogleError:
    """Real's answer to an id naming no suggestion or comment: a 404 that names neither the request
    nor its kind, ahead of an earlier request's deferred 500."""
    return gerr.GoogleError(404, message, reason="notFound", status="NOT_FOUND")


def _docs_suggestion(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``acceptSuggestion``, ``rejectSuggestion`` and ``deleteSuggestion``."""
    if not req.get("suggestion_id"):
        raise _EditorRefused("A suggestion ID must be specified.")
    raise _docs_not_found(f"Suggestion with ID {req['suggestion_id']} does not exist.")


def _docs_thread(req: dict) -> None:
    """The comment or suggestion thread a reply request names, refused as naming nothing."""
    if req.get("suggestion_id"):
        raise _docs_not_found(f"Suggestion with ID {req['suggestion_id']} does not exist.")
    raise _docs_not_found(f"Comment with ID {req.get('comment_id', '')} does not exist.")


def _docs_blank(text: str | None) -> bool:
    """Whether a post's content is empty as real reads it: measured 2026-11-09, a tab, a newline,
    U+001F, U+2003, U+2028 and U+3000 alone were empty, and U+00A0, U+0085 and U+200B were not, as
    Java's `Character.isWhitespace` has them."""
    return not strip_java_whitespace(text or "")


def _docs_add_comment_reply(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``addCommentReply``, in real's order of refusals, measured 2026-11-09: a thread named; on
    a comment, a `commentAction` the enum does not declare, then empty content unless the action
    resolves or reopens the thread; on a suggestion, empty content, then any `commentAction` set,
    0 included. A `suggestionAction` is not checked."""
    if not req.get("comment_id") and not req.get("suggestion_id"):
        raise _EditorRefused("Add reply requests must specify a comment ID or suggestion ID.")
    post = req.get("post", {})
    action = post.get("comment_action")
    if req.get("suggestion_id"):
        if _docs_blank(post.get("content")):
            raise _EditorRefused("Add suggestion reply requests must specify non-empty post text.")
        if action is not None:
            raise _EditorRefused("Add suggestion reply requests may not specify a comment action.")
    else:
        if action is not None and not 0 <= action <= 3:
            raise _EditorRefused("Unrecognized comment action type.")
        if _docs_blank(post.get("content")) and action not in (2, 3):
            raise _EditorRefused(
                "Add comment reply requests must specify a non-empty content if comment action is "
                "not RESOLVE or REOPEN."
            )
    _docs_thread(req)
    return {}, False


def _docs_delete_comment(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    if not req.get("comment_id"):
        raise _EditorRefused("Delete comment requests must specify a comment ID.")
    raise _docs_not_found(f"Comment thread with ID '{req['comment_id']}' was not found.")


def _docs_delete_comment_reply(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``deleteCommentReply``: a post named, which may not be the thread's own head post."""
    if not req.get("post_id"):
        raise _EditorRefused("Delete comment reply requests must specify a post ID.")
    if not req.get("comment_id") and not req.get("suggestion_id"):
        raise _EditorRefused("Delete comment reply requests must specify a thread ID.")
    if req["post_id"] == req.get("comment_id"):
        raise _EditorRefused(
            "Delete comment reply requests may not specify the head post for deletion."
        )
    _docs_thread(req)
    return {}, False


def _docs_update_comment_post(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    if not req.get("post_id"):
        raise _EditorRefused("Update comment post requests must specify a post ID.")
    if _docs_blank(req.get("content")):
        raise _EditorRefused("Update comment post requests must specify non-empty content.")
    if not req.get("comment_id") and not req.get("suggestion_id"):
        raise _EditorRefused("Update comment post requests must specify a thread ID.")
    _docs_thread(req)
    return {}, False


def _docs_dropdown_definition(name: str):
    """``deleteDropdownDefinition`` or ``updateDropdownDefinitionProperties``: the tab, a
    definition named, then looked up, ahead of the fields an update names."""

    def check(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
        _docs_tab(doc, req)
        if not req.get("dropdown_definition_id"):
            raise _EditorRefused(
                f"{name} dropdown definition requests must specify a dropdown_definition_id."
            )
        raise _EditorRefused(
            f"Dropdown definition with ID {req['dropdown_definition_id']} not found."
        )

    return check


def _docs_insert_dropdown(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``insertDropdown``: where it goes, as `insertText` reads a location up to the segment's end,
    then the definition named and looked up; an index at a section break is looked up as any."""
    where = req.get("location", req.get("end_of_segment_location"))
    if where is None:
        raise _EditorRefused("No insertion location set.")
    tab = _docs_tab(doc, where)
    index = where.get("index", 0)
    if "location" in req:
        _docs_negative(index)
    _, body = _docs_segment(doc, tab, where)
    if "location" in req:
        _docs_past_end(index, body)
    if not req.get("dropdown_definition_id"):
        raise _EditorRefused("Insert dropdown requests must specify a dropdown_definition_id.")
    raise _EditorRefused(f"Dropdown definition with ID {req['dropdown_definition_id']} not found.")


def _docs_update_dropdown(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``updateDropdownProperties``, in real's order of refusals, measured 2026-11-09: the tab,
    its fields, `*` among them naming the read-only id, the first of `displayValue` and
    `dropdownDefinitionId` named, a dropdown named, then a `selectedOptionId` named and left empty,
    then the lookup."""
    _docs_tab(doc, req)
    fields = req.get("fields", "")
    if fields.strip(_DOCS_SPACE) == "*":
        raise _EditorRefused("Invalid field mask: * includes read-only fields")
    if _docs_fields(fields, _PJ_DOCS("docs.DropdownProperties"), ()) is None:
        raise gerr.internal_error()
    paths = [_docs_snake(_docs_camel(p.strip(_DOCS_SPACE))) for p in fields.split(",")]
    for path in paths:
        if path in ("display_value", "dropdown_definition_id"):
            raise _EditorRefused(f"Invalid field mask: {path} cannot be updated")
    if not req.get("dropdown_id"):
        raise _EditorRefused("Update dropdown properties requests must specify a dropdown_id.")
    if "selected_option_id" in paths and not req.get("dropdown_properties", {}).get(
        "selected_option_id"
    ):
        raise _EditorRefused(
            "Update dropdown properties requests must specify a selected_option_id."
        )
    raise _EditorRefused(f"Dropdown with ID {req['dropdown_id']} not found.")


def _docs_replace_image(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """``replaceImage``: the tab, a URL of `http` or `https`, an image named, then looked up."""
    _docs_tab(doc, req)
    uri = req.get("uri", "")
    if not uri:
        raise _EditorRefused("The URL should not be empty.")
    if not uri.startswith(("http://", "https://")):
        raise _EditorRefused("The URL should start with http:// or https://.")
    if not req.get("image_object_id"):
        raise _EditorRefused("Must provide a non-empty object ID")
    raise _EditorRefused(f"The object with ID {req['image_object_id']} could not be found.")


def _docs_delete_positioned_object(doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    _docs_tab(doc, req)
    if not req.get("object_id"):
        raise _EditorRefused("Must provide a non-empty object ID")
    raise _EditorRefused(f"The object with ID {req['object_id']} could not be found.")


# The requests applied here, by the proto name a read body carries each under; every other kind is
# refused naming itself (`routers.google._editor_batch`).
_DOCS_APPLY = {
    "insert_text": _docs_insert_text,
    "delete_content_range": _docs_delete_content_range,
    "replace_all_text": _docs_replace_all_text,
    "create_header": _docs_create_segment("header"),
    "create_footer": _docs_create_segment("footer"),
    "create_footnote": _docs_create_footnote,
    "delete_header": _docs_delete_segment("header"),
    "delete_footer": _docs_delete_segment("footer"),
    "insert_page_break": _docs_insert_page_break,
    "insert_table": _docs_insert_table,
    "insert_table_row": _docs_insert_row,
    "insert_table_column": _docs_insert_column,
    "delete_table_row": _docs_delete_row,
    "delete_table_column": _docs_delete_column,
    "add_document_tab": _docs_add_tab,
    "delete_tab": _docs_delete_tab,
    "update_document_tab_properties": _docs_update_tab,
    "update_text_style": _docs_update_text_style,
    "update_paragraph_style": _docs_update_paragraph_style,
    "create_paragraph_bullets": _docs_create_bullets,
    "delete_paragraph_bullets": _docs_delete_bullets,
    "create_named_range": _docs_create_named_range,
    "delete_named_range": _docs_delete_named_range,
    "replace_named_range_content": _docs_replace_named_range,
    "pin_table_header_rows": _docs_pin_rows,
    "update_table_row_style": _docs_update_row_style,
    "update_table_column_properties": _docs_update_columns,
    "update_table_cell_style": _docs_update_cell_style,
    "merge_table_cells": _docs_merge("Merge"),
    "unmerge_table_cells": _docs_merge("Unmerge"),
    "accept_suggestion": _docs_suggestion,
    "reject_suggestion": _docs_suggestion,
    "delete_suggestion": _docs_suggestion,
    "add_comment_reply": _docs_add_comment_reply,
    "delete_comment": _docs_delete_comment,
    "delete_comment_reply": _docs_delete_comment_reply,
    "update_comment_post": _docs_update_comment_post,
    "delete_dropdown_definition": _docs_dropdown_definition("Delete"),
    "update_dropdown_definition_properties": _docs_dropdown_definition("Update"),
    "insert_dropdown": _docs_insert_dropdown,
    "update_dropdown_properties": _docs_update_dropdown,
    "replace_image": _docs_replace_image,
    "delete_positioned_object": _docs_delete_positioned_object,
    "insert_section_break": _docs_insert_section_break,
    "update_section_style": _docs_update_section_style,
    "update_document_style": _docs_update_document_style,
    "update_named_style": _docs_update_named_style,
}
_DOCS_KINDS = {
    f.name: f.json_name
    for f in next(f for f in _PJ_BATCH_UPDATE.fields if f.json_name == "requests").message.fields
}
# What the body's schema declares: the requests applied here, and the two revisions a write may
# be pinned to, `SUGGEST` being refused (`docs_batch_update`).
_DOCS_BATCH_KEPT = {
    "requests": {_DOCS_KINDS[kind]: None for kind in _DOCS_APPLY},
    "writeControl": {"requiredRevisionId": None, "targetRevisionId": None},
}

_P_DOCS_BATCH_UPDATE = [qp("fields")]


# The requests the next one in the batch looks past, to the one before them (`_docs_settled`,
# `gdocs.Body._reached`).
_DOCS_UNSEEN = {
    "replace_all_text",
    "create_named_range",
    "delete_named_range",
    "replace_named_range_content",
    "delete_header",
    "delete_footer",
    "add_document_tab",
    "delete_tab",
    "update_document_tab_properties",
}


def _docs_write_index(req: dict) -> int | None:
    """The index a request names: its location's, its range's start or its table's start; None for
    one at a segment's end or with no location, range or table."""
    if "location" in req:
        return req["location"].get("index", 0)
    if "range" in req:
        return req["range"].get("start_index", 0)
    where = _docs_table_where(req)
    return None if where is None else where.get("index", 0)


def _docs_table_where(req: dict) -> dict | None:
    """The table start location a request names: its own, its cell's or its table range's."""
    if "table_start_location" in req:
        return req["table_start_location"]
    cell = req.get("table_cell_location", req.get("table_range", {}).get("table_cell_location"))
    return None if cell is None else cell.get("table_start_location", {})


def _docs_write_key(doc: gdocs.Document, req: dict) -> tuple[str, str]:
    """The tab id and segment id a request's location, range, cell, table or section break names,
    or its own `tabId`, the first tab's body for one that names none of them."""
    names = ("location", "range", "end_of_segment_location", "section_break_location")
    where = next((req[k] for k in names if k in req), _docs_table_where(req)) or req
    tab = doc.tab(where.get("tab_id"))
    return tab["tabProperties"]["tabId"], where.get("segment_id", "")


def _docs_settled(kind: str, apply, doc: gdocs.Document, req: dict) -> tuple[dict, bool]:
    """One request applied, then `gdocs.Document.settle`, then kept as the request before the next
    in its tab (`gdocs.Document.note`) with the index, tab and segment it names, unless
    `_DOCS_UNSEEN` holds its kind."""
    reply, fails = apply(doc, req)
    doc.settle()
    if kind not in _DOCS_UNSEEN:
        doc.note(kind, _docs_write_index(req), _docs_write_key(doc, req))
    return reply, fails


@router.post(
    "/docs/v1/documents/{document_id}:batchUpdate",
    openapi_extra={
        "parameters": _P_DOCS_BATCH_UPDATE,
        **proto_body(_PJ_BATCH_UPDATE, keep=_DOCS_BATCH_KEPT),
    },
)
async def docs_batch_update(document_id: str, request: Request):
    """``documents.batchUpdate``: each request checked and applied in order against the document
    the ones before it left, so a later request's indices count what an earlier one inserted and a
    later request finds a tab or header an earlier one made, and all of them or none kept
    (`backlot.gdocs.Document`). A document that states no structure has its line reading stored
    by its first write.

    The order real answers in, measured 2026-10-29: the credential, the body, the `fields` mask, the
    lookup and a reader's 403, no requests or one with no kind set (`_editor_requests`), a revision
    of another Doc, a `requiredRevisionId` that is not the Doc's latest or a `targetRevisionId` that
    is no revision of it, then each request's refusal, the first one's named alone. A 500 a
    character in `gdocs.UNWRITABLE` leads to comes after every request is read; one a request raises
    comes at its own request (`_editor_batch`). Neither keeps anything. A kind other than those
    applied here is refused naming itself, and so is the `SUGGEST` write mode, which records the
    requests as suggestions.

    Every write, one that changed nothing included, moves the `revisionId`, but for a batch of
    `updateDocumentTabProperties` requests that each left the tab's title, index and parent as they
    were and named no `iconEmoji`, and `replaceAllText` requests whose `tabsCriteria` lists no tab,
    which keeps it and stores nothing. The `revisionId` is answered as
    `writeControl.requiredRevisionId` and served by the next read; real takes any recent revision as
    `targetRevisionId` and merges the write into what collaborators did since, which here is
    nothing. The Drive file's `modifiedTime` is the write's (`store.PATCHABLE`), and its `content`
    the tabs' text (`gdocs.document_text`), so search reads what the write left. Inside a batch it
    is the 501 real's batch gives."""
    _require(request)
    _, body = await _query_and_body(request, {}, _PJ_BATCH_UPDATE)
    tree, _ = _gmask_select(
        gerr.first_repeat(request.query_params, "fields"), "docs.BatchUpdateDocumentResponse"
    )
    if _BATCH_OUTER.get() is not None:
        raise gerr.unimplemented()
    row = _editor_doc(request, document_id, expect="document")
    _editor_writable(request, row)
    requests = _editor_requests(body.get("requests", []))
    control = body.get("write_control", {})
    # The number as sent: measured, a `writeMode` of 7 or -1 applied the write as `EDIT` does.
    if control.get("write_mode") == _DOCS_WRITE_MODE.names.index("SUGGEST"):
        raise gerr.invalid_argument(
            "'SUGGEST' is not served by Backlot: real Docs records each request as a suggestion, "
            "and nothing here keeps one."
        )
    conn = auth.conn(request)
    written = store.gdrive_doc_revision(conn, document_id)
    for field in ("required_revision_id", "target_revision_id"):
        stated = control.get(field, "")
        named = _docs_revision_of(stated)
        # measured 2026-10-29: `AHj4eMabc`, real's prefix on no revision, is a 500 as either
        if named is None and stated.startswith(_DOCS_REVISION_PREFIX):
            raise gerr.internal_error()
        if named is not None and named[0] != document_id:
            raise gerr.invalid_argument("The provided revision ID is invalid.")
    if "required_revision_id" in control:
        stated = control["required_revision_id"]
        if stated != _docs_revision(document_id, written):
            raise gerr.invalid_argument(
                f"The required revision ID '{stated}' does not match the latest revision."
            )
    if "target_revision_id" in control:
        stated = control["target_revision_id"]
        named = _docs_revision_of(stated)
        if named is None or named[1] > written:
            raise gerr.invalid_argument(f"Invalid revision ID: {stated}")

    doc = gdocs.Document(document_id, _docs_stored(conn, row), written + 1)
    replies = _editor_batch(
        requests,
        _DOCS_KINDS,
        {
            kind: functools.partial(_docs_settled, kind, apply, doc)
            for kind, apply in _DOCS_APPLY.items()
        },
        "Docs",
    )

    if doc.unchanged == len(requests):
        answer = {
            "replies": replies,
            "writeControl": {"requiredRevisionId": _docs_revision(document_id, written)},
            "documentId": document_id,
            "suggestionResponses": [{} for _ in requests],
            "commentUpdateState": "NO_UPDATES_REQUESTED",
        }
        return answer if tree is None else _gmask_apply(tree, answer)

    # Drive's `size` is what real stores, not the text: measured 2026-10-30, a Doc of 1024 stayed
    # 1024 through 6,000 inserted characters, a blank Doc's 1 became 1024 on its first write, and
    # of 55 Docs read, ones of 297 and 56,195 bytes of text were 2,562 and 51,253. `version` went
    # from 1 to 2 or 3 with a blank Doc's first write, and here climbs once per write.
    before = store.gdrive_doc_written(conn, document_id)
    nbytes, version = before or (
        max(store.drive_file_bytes(row["subtype"], row["content"]), store.DRIVE_NATIVE_MIN_BYTES),
        int(_drive_version(row)),
    )
    stored = doc.stored()
    store.replace_parts(
        conn,
        "google_drive",
        (document_id,),
        {
            "gdrive_doc_bodies": [
                {
                    # escaped, since a style's edge may leave half a character in a run
                    "document": json.dumps(stored),
                    "revision": written + 1,
                    "bytes": nbytes,
                    "version": version + 1,
                }
            ]
        },
        {"content": gdocs.document_text(stored), "updated_ts": int(time.time())},
    )
    answer = {
        "replies": replies,
        "writeControl": {"requiredRevisionId": _docs_revision(document_id, written + 1)},
        "documentId": document_id,
        "suggestionResponses": [{} for _ in requests],
        "commentUpdateState": "NO_UPDATES_REQUESTED",
    }
    return answer if tree is None else _gmask_apply(tree, answer)
