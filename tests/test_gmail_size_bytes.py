"""Regression for Gmail MessagePartBody.size UTF-8 byte length (#401)."""

from __future__ import annotations

import base64

import pytest

from tests._helpers import client_for, served_id, tiny_corpus


_RECORDS = [
    {
        "source_type": "gmail",
        "doc_id": "ko",
        "mailbox": "ceo",
        "title": "Non-ASCII",
        "content": "안녕하세요, 다음 주 회의 일정을 공유드립니다.",
        "author_email": "ceo@x.com",
    },
    {
        "source_type": "gmail",
        "doc_id": "att-ko",
        "mailbox": "ceo",
        "title": "Non-ASCII attachment",
        "content": "see attached",
        "author_email": "ceo@x.com",
        "attachments": [{"filename": "notes.txt", "mime": "text/plain", "content": "안녕"}],
    },
]


@pytest.fixture
def size_client(tmp_path):
    settings = tiny_corpus(tmp_path, _RECORDS)
    with client_for(settings, reload=True) as client:
        yield client, {"Authorization": f"Bearer {settings.admin_token}"}


def test_gmail_size_is_utf8_byte_length_not_character_count(size_client):
    """Real Gmail MessagePartBody.size is decoded UTF-8 bytes (discovery doc). Closes #401."""
    client, h = size_client

    mid = served_id("gmail", "ko")
    payload = client.get(
        f"/gmail/v1/users/me/messages/{mid}", headers=h, params={"format": "full"}
    ).json()["payload"]
    plain = next(
        p
        for p in payload.get("parts", [payload])
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data")
    )
    decoded = base64.urlsafe_b64decode(plain["body"]["data"])
    text = "안녕하세요, 다음 주 회의 일정을 공유드립니다."
    assert plain["body"]["size"] == len(decoded) == len(text.encode("utf-8"))
    assert plain["body"]["size"] != len(text)

    mid = served_id("gmail", "att-ko")
    payload = client.get(
        f"/gmail/v1/users/me/messages/{mid}", headers=h, params={"format": "full"}
    ).json()["payload"]
    parts = []
    stack = list(payload.get("parts") or [])
    while stack:
        p = stack.pop(0)
        stack.extend(p.get("parts") or [])
        if p.get("body", {}).get("attachmentId"):
            parts.append(p)
    assert parts
    for p in parts:
        got = client.get(
            f"/gmail/v1/users/me/messages/{mid}/attachments/{p['body']['attachmentId']}",
            headers=h,
        ).json()
        decoded = base64.urlsafe_b64decode(got["data"])
        assert got["size"] == p["body"]["size"] == len(decoded)
        assert got["size"] == len("안녕".encode("utf-8"))
        assert got["size"] != len("안녕")
