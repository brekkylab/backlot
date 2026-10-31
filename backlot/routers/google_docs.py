"""Google Docs v1 (``/docs/v1``): ``documents.get``, ``documents.create`` and
``documents.batchUpdate``.

The routes are registered on `routers.google.router` as `routers.google_sheets` describes, and a
``fields`` mask is read as the Sheets routes read theirs (`routers.google._gmask_select`).

The answers below were measured on docs.googleapis.com between 2026-10-09 and 2026-10-29, each
comment dating its own, by a Google Workspace member on a one-tab Doc it owns and by a second
member it was shared with as a reader.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import time
from pathlib import Path

from fastapi import Request

from backlot import auth, gdocs, protojson, store
from backlot.errors import google as gerr
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

# A corpus states no tabs, so a Doc is the one tab every Doc starts with: measured 2026-10-09 on a
# one-tab Doc and 2026-10-26 on one `documents.create` made.
_DOCS_TAB = {"tabId": "t.0", "title": "Tab 1", "index": 0}

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


def _docs_content(body: list[dict]) -> dict:
    """What a Doc and its one tab both hold: ``body`` (`backlot.gdocs`) and the default styles."""
    return {
        "body": {"content": gdocs.indexed(body)},
        "documentStyle": gdocs.document_style(),
        "namedStyles": gdocs.named_styles(),
    }


def _docs_tabs(content: dict) -> list[dict]:
    return [{"tabProperties": dict(_DOCS_TAB), "documentTab": content}]


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
    """The document as Docs structural elements: its stored body, or one ``paragraph`` per line of
    its text when it states none (`backlot.gdocs`), flat or as its one tab.

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
    body = store.gdrive_doc_body_for(conn, document_id)
    content = _docs_content(body if body is not None else gdocs.from_text(row["content"] or ""))
    written = store.gdrive_doc_revision(conn, document_id)
    revision = {"revisionId": _docs_revision(document_id, written)} if owned else {}
    # Real's orders, measured 2026-10-16 on twelve Docs and 2026-10-26 on the tabbed answer, the
    # same under each mode and whatever order a mask names the members in. Real also serves
    # `headers` and `footers` after `body`, `lists` after `namedStyles` and `inlineObjects` before
    # `documentId` when a Doc has them; this server serves none of the four.
    if tabs:
        doc = {
            "title": row["title"],
            **revision,
            "suggestionsViewMode": suggestions,
            "documentId": document_id,
            "tabs": _docs_tabs(content),
            "commentsViewMode": view,
        }
    else:
        doc = {
            "title": row["title"],
            **content,
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
    ("docs.WriteControl", "requiredRevisionId"): "control",
    ("docs.WriteControl", "targetRevisionId"): "control",
    ("docs.ReplaceAllTextRequest", "containsText"): "criteria",
}.items():
    _DOCS_SHAPES[_key] = {"oneof": _group}
# A request is declared inside the body's message: `"requests": "x"` is refused naming
# `….BatchUpdateDocumentRequest.Request`.
_PJ_DOCS = _pj_messages(
    _DOCS_VIEW,
    {("docs.WriteControl", "writeMode"): _DOCS_WRITE_MODE},
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
    content = _docs_content(gdocs.from_text(""))
    doc = {
        "title": title,
        **content,
        "revisionId": _docs_revision(file_id),
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
        "documentId": file_id,
        "tabs": _docs_tabs(content),
    }
    return doc if tree is None else _gmask_apply(tree, doc)


# --- `documents.batchUpdate` ----------------------------------------------------------------------
#
# Measured against the live Docs v1 API on 2026-10-29, as a Workspace member writing a Doc it owns
# and as a second member it was shared with as a reader. How each request changes the body is
# `gdocs.Body`'s; this section is the request, its refusals and the answer.

_PJ_BATCH_UPDATE = _PJ_DOCS("docs.BatchUpdateDocumentRequest")


def _docs_target(where: dict) -> None:
    """Refuse a location or range in a tab or segment the Doc does not have. A Doc here is its one
    tab, `t.0`, with no header, footer or footnote; measured, the tab is checked before the
    segment, and both before anything else the request states."""
    if where.get("tab_id") not in (None, "", _DOCS_TAB["tabId"]):
        raise _EditorRefused("Cannot apply request to an invalid tab ID.")
    segment = where.get("segment_id")
    if segment:
        raise _EditorRefused(
            f"Segment with ID {segment} was not found. If a segment ID is provided, it must be a "
            "header, footer or footnote ID. Use an empty segment ID to reference the body."
        )


def _docs_negative(index: int) -> None:
    if index < 0:
        raise _EditorRefused("Index must be greater than or equal to 0.")


def _docs_past_end(index: int, body: gdocs.Body) -> None:
    if index >= body.end:
        raise _EditorRefused(
            f"Index {index} must be less than the end index of the referenced segment, {body.end}."
        )


def _docs_insert_text(body: gdocs.Body, req: dict) -> tuple[dict, bool]:
    """``insertText``, in real's order of refusals, measured: no location, the tab, the segment, no
    text, then the index -- below zero, at or past the body's end, the section break at 0, inside a
    grapheme cluster. `endOfSegmentLocation` inserts before the body's last newline. The characters
    `insertText` strips are dropped, and text that was nothing else inserts nothing and is taken.
    Real fails on one that holds a character in `gdocs.UNWRITABLE` once every request is read."""
    where = req.get("location", req.get("end_of_segment_location"))
    if where is None:
        raise _EditorRefused("No insertion location set.")
    _docs_target(where)
    text = req.get("text", "")
    if not text:
        raise _EditorRefused("Insert text requests must specify text to insert.")
    if "location" in req:
        index = where.get("index", 0)
        _docs_negative(index)
        _docs_past_end(index, body)
        if index == 0:
            raise _EditorRefused(
                "The insertion index must be inside the bounds of an existing paragraph. You can "
                "still create new paragraphs by inserting newlines."
            )
        pos = body.position(index)
        if pos is None or body.splits_cluster(pos):
            raise _EditorRefused("The insertion index cannot be within a grapheme cluster.")
    else:
        pos = len(body.chars) - 1
    kept = gdocs.STRIPPED.sub("", text)
    if kept:
        body.insert(pos, kept)
    return {}, bool(gdocs.UNWRITABLE.search(kept))


def _docs_delete_content_range(body: gdocs.Body, req: dict) -> tuple[dict, bool]:
    """``deleteContentRange``, in real's order of refusals, measured: no range, the tab, the
    segment, an index missing (a `null` one included), the end before the start, an index below
    zero, the start and then the end's last index at or past the body's end, an empty range, the
    section break at 0, the body's last newline, and either end inside a surrogate pair. Deleting a
    combining mark without the letter it sits on is taken."""
    rng = req.get("range")
    if rng is None:
        raise _EditorRefused("Delete content range requests must include a range to delete.")
    _docs_target(rng)
    if "start_index" not in rng or "end_index" not in rng:
        raise _EditorRefused("Invalid range: must contain a start and end index")
    start, end = rng["start_index"], rng["end_index"]
    if end < start:
        raise _EditorRefused("Invalid range: end index cannot be less than start index")
    _docs_negative(start)
    _docs_past_end(start, body)
    _docs_past_end(end - 1, body)
    if start == end:
        raise _EditorRefused("The range should not be empty.")
    if start == 0:
        raise _EditorRefused("Cannot operate on the first section break in the document.")
    if end == body.end:
        raise _EditorRefused(
            "The range cannot include the newline character at the end of the segment."
        )
    first, last = body.position(start), body.position(end)
    if first is None or last is None:
        raise _EditorRefused("Invalid deletion range. Cannot delete the requested range.")
    body.delete(first, last)
    return {}, False


def _docs_replace_all_text(body: gdocs.Body, req: dict) -> tuple[dict, bool]:
    """``replaceAllText``: real fails on a replacement holding a character in `gdocs.UNWRITABLE`
    that matched, and on a `tabsCriteria` naming a tab the Doc does not have, once every request
    is read; measured, a zero-match replace by U+FDD0 was a 200. What matches is
    `gdocs.Body.find`'s, and an empty or missing `replaceText` deletes it; the characters
    `insertText` strips are stripped from it too. A `searchByRegex` replacement is the text as
    sent: `$1` is not a group."""
    criteria = req.get("contains_text") or {}
    if not criteria.get("text"):
        raise _EditorRefused("The substring match text should not be empty")
    tabs = req.get("tabs_criteria", {}).get("tab_ids", [])
    text = gdocs.STRIPPED.sub("", req.get("replace_text", ""))
    matches = body.find(
        criteria["text"],
        match_case=criteria.get("match_case", False),
        regex=criteria.get("search_by_regex", False),
    )
    body.replace(matches, text)
    fails = any(t != _DOCS_TAB["tabId"] for t in tabs) or bool(
        matches and gdocs.UNWRITABLE.search(text)
    )
    changed = {"occurrencesChanged": len(matches)} if matches else {}
    return {"replaceAllText": changed}, fails


# The requests applied here, by the proto name a read body carries each under; every other kind is
# refused naming itself (`routers.google._editor_batch`).
_DOCS_APPLY = {
    "insert_text": _docs_insert_text,
    "delete_content_range": _docs_delete_content_range,
    "replace_all_text": _docs_replace_all_text,
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


@router.post(
    "/docs/v1/documents/{document_id}:batchUpdate",
    openapi_extra={
        "parameters": _P_DOCS_BATCH_UPDATE,
        **proto_body(_PJ_BATCH_UPDATE, keep=_DOCS_BATCH_KEPT),
    },
)
async def docs_batch_update(document_id: str, request: Request):
    """``documents.batchUpdate``: each request checked and applied in order against the body the
    ones before it left, so a later request's indices count what an earlier one inserted, and all
    of them or none kept (`backlot.gdocs.Body`). A document that states no structure has its line
    reading stored by its first write.

    The order real answers in, measured 2026-10-29: the credential, the body, the `fields` mask,
    the lookup and a reader's 403, no requests or one with no kind set (`_editor_requests`), a
    revision of another Doc, a `requiredRevisionId` that is not the Doc's latest or a
    `targetRevisionId` that is no revision of it, then each request's refusal, the first one's
    named alone. A 500 a request leads to (`gdocs.UNWRITABLE`, an unknown tab in `tabsCriteria`)
    comes after every request is read, and keeps nothing. A kind other than the three applied here
    is refused naming itself, and so is the `SUGGEST` write mode, which records the requests as
    suggestions.

    Every write, one that changed nothing included, moves the `revisionId`, answered as
    `writeControl.requiredRevisionId` and served by the next read; real takes any recent revision as
    `targetRevisionId` and merges the write into what collaborators did since, which here is
    nothing. The Drive file's `modifiedTime` is the write's (`store.PATCHABLE`), and its `content`
    the body's text, so search and export read what the write left. Inside a batch it is the 501
    real's batch gives."""
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

    stored = store.gdrive_doc_body_for(conn, document_id)
    doc = gdocs.Body(
        document_id,
        stored if stored is not None else gdocs.from_text(row["content"] or ""),
        written + 1,
    )
    replies = _editor_batch(
        requests,
        _DOCS_KINDS,
        {kind: functools.partial(apply, doc) for kind, apply in _DOCS_APPLY.items()},
        "Docs",
    )

    # Drive's `size` is what real stores, not the text: measured 2026-10-30, a Doc of 1024 stayed
    # 1024 through 6,000 inserted characters, a blank Doc's 1 became 1024 on its first write, and
    # of 55 Docs read, ones of 297 and 56,195 bytes of text were 2,562 and 51,253. `version` went
    # from 1 to 2 with a blank Doc's first write, and climbs once per write here.
    before = store.gdrive_doc_written(conn, document_id)
    nbytes, version = before or (
        max(store.drive_file_bytes(row["subtype"], row["content"]), store.DRIVE_NATIVE_MIN_BYTES),
        int(_drive_version(row)),
    )
    store.replace_parts(
        conn,
        "google_drive",
        (document_id,),
        {
            "gdrive_doc_bodies": [
                {
                    "body": json.dumps(doc.content(), ensure_ascii=False),
                    "revision": written + 1,
                    "bytes": nbytes,
                    "version": version + 1,
                }
            ]
        },
        {"content": doc.text(), "updated_ts": int(time.time())},
    )
    answer = {
        "replies": replies,
        "writeControl": {"requiredRevisionId": _docs_revision(document_id, written + 1)},
        "documentId": document_id,
        "suggestionResponses": [{} for _ in requests],
        "commentUpdateState": "NO_UPDATES_REQUESTED",
    }
    return answer if tree is None else _gmask_apply(tree, answer)
