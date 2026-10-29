"""Google Docs v1 (``/docs/v1``): ``documents.get``.

The routes are registered on `routers.google.router` as `routers.google_sheets` describes, and a
``fields`` mask is read as the Sheets routes read theirs (`routers.google._gmask_select`).

The answers below were measured on docs.googleapis.com between 2026-10-09 and 2026-10-28, each
comment dating its own, by a Google Workspace member on a one-tab Doc it owns and by a second
member it was shared with as a reader.
"""

from __future__ import annotations

from fastapi import Request

from backlot import auth, gdocs, protojson, store, synth
from backlot.errors import google as gerr
from backlot.openapi import qp
from backlot.routers.google import (
    _GOOGLE_FIELDS,
    _drive_owned_by,
    _editor_doc,
    _gmask_apply,
    _gmask_select,
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
_DOCS_SUGGESTIONS_VIEW = protojson.Enum(
    f"{_DOCS_VIEW}.SuggestionsViewMode",
    (
        "DEFAULT_FOR_CURRENT_ACCESS",
        "SUGGESTIONS_INLINE",
        "PREVIEW_SUGGESTIONS_ACCEPTED",
        "PREVIEW_WITHOUT_SUGGESTIONS",
    ),
)
_DOCS_COMMENTS_VIEW = protojson.Enum(
    f"{_DOCS_VIEW}.CommentsViewMode",
    (
        "COMMENTS_VIEW_MODE_UNSPECIFIED",
        "COMMENTS_VIEW_MODE_DEFAULT_FOR_CURRENT_ACCESS",
        "COMMENTS_VIEW_MODE_OMITTED",
        "COMMENTS_VIEW_MODE_INCLUDED",
    ),
)
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

    body = store.gdrive_doc_body_for(auth.conn(request), document_id)
    if body is None:
        body = gdocs.from_text(row["content"] or "")
    content = {
        "body": {"content": gdocs.indexed(body)},
        "documentStyle": gdocs.document_style(),
        "namedStyles": gdocs.named_styles(),
    }
    revision = {"revisionId": synth._digest(document_id)[:24]} if owned else {}
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
            "tabs": [{"tabProperties": dict(_DOCS_TAB), "documentTab": content}],
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
