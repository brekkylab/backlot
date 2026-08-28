"""Read a Backlot server through fsspec, the filesystem interface pandas, pyarrow, dask and
LlamaIndex all consume.

S3 needs nothing from here: ``s3fs`` takes the endpoint as a constructor argument, so
``S3FileSystem(client_kwargs={"endpoint_url": f"{base_url}/s3"})`` is the whole redirect (see
``examples/using-fsspec/s3.py``). Its one wrinkle is an upstream ``walk`` bug, worked around by
``integrations.llamaindex.patch_s3fs_walk`` — the same shim, whichever entry point reached it.

Drive and GitHub are the two that need a module, both because their implementation hardcodes the
vendor host and takes no endpoint argument. Neither can be redirected by rebinding a constant the
way the rest of ``backlot.integrations`` does — the hosts they name are built inside method bodies
— so each is a subclass that replaces those methods:

- ``drive_filesystem_at`` — ``gdrive_fsspec`` (the ``gdrive://`` implementation). Supplies the
  endpoint, and works around three of its defects that have nothing to do with Backlot: all three
  reproduce against real Google Drive, and each override names the one it fixes.
- ``github_filesystem_at`` — fsspec's own ``GithubFileSystem``. Supplies the endpoint for all six
  of the api.github.com URLs it builds, closes the one host it takes from the SERVER's response
  rather than building, and swaps HTTP Basic for the bearer scheme Backlot answers.
"""

from __future__ import annotations

import io

__all__ = ["drive_filesystem_at", "github_filesystem_at"]

# What a Google-native file exports as. A Workspace file has no binary content, so `alt=media` —
# the only download gdrive_fsspec knows — answers 403 fileNotDownloadable for these three.
_EXPORT_FORMATS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

_DRIVE_FS_CLASS = None


def _drive_fs_class():
    """The subclass, built on first use so importing this module needs no optional dependency."""
    global _DRIVE_FS_CLASS
    if _DRIVE_FS_CLASS is not None:
        return _DRIVE_FS_CLASS

    from gdrive_fsspec import GoogleDriveFileSystem
    from google.api_core.client_options import ClientOptions
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    class _BacklotDriveFileSystem(GoogleDriveFileSystem):
        """``gdrive_fsspec`` pointed at a Backlot server instead of Google."""

        def __init__(self, base_url: str, token: str, **kwargs):
            self._base_url = base_url.rstrip("/")
            self._token = token
            self._export_cache: dict[str, bytes] = {}
            # "anon" is the one connect() method that runs no OAuth flow; the override below
            # replaces the service it would have built anyway.
            super().__init__(token="anon", **kwargs)

        def connect(self, method=None):
            """The redirect. Drive's discovery document already carries the `/drive/v3` service
            path in its rootUrl, so the replacement endpoint has to carry it too — the same rule
            `integrations.llamaindex.point_drive_at` documents for the LlamaIndex reader."""
            srv = build(
                "drive",
                "v3",
                credentials=Credentials(token=self._token),
                client_options=ClientOptions(api_endpoint=f"{self._base_url}/drive/v3"),
            )
            self.srv = srv
            self.files = srv.files()

        @classmethod
        def _strip_protocol(cls, path):
            """gdrive_fsspec's paths are relative and it leaves a leading "/" in place, so
            `ls("/folder")` raises FileNotFoundError. Anything that roots paths at "/" — a URL
            whose path is host-less, fsspec.fuse, plain habit — trips over it."""
            return super()._strip_protocol(path).lstrip("/")

        def ls(self, path, detail=False, trashed=False):
            """gdrive_fsspec's `ls` consults `_ls_from_cache` first, which will answer a directory
            listing out of the cached PARENT listing — with that directory's own entry rather than
            its children. Dropping the parent entry forces the slow path, which re-fetches and
            re-caches the parent on its way to the children."""
            path = self._strip_protocol(path)
            if path and path not in self.dircache:
                self.dircache.pop(self._parent(path), None)
            return super().ls(path, detail=detail, trashed=trashed)

        def _export(self, path) -> bytes | None:
            """The bytes a read of `path` returns, when Drive will only give them up as an export.
            None for an ordinary binary file, which `alt=media` downloads normally."""
            info = super().info(path)
            mime = _EXPORT_FORMATS.get(info.get("mimeType"))
            if mime is None:
                return None
            if info["id"] not in self._export_cache:
                self._export_cache[info["id"]] = self.files.export(
                    fileId=info["id"], mimeType=mime
                ).execute()
            return self._export_cache[info["id"]]

        def info(self, path, trashed=False):
            """`size` on a native file is the length of the stored content, but a read returns the
            EXPORT of it, which is longer. Anything that trusts `size` to size a read — fsspec.fuse,
            a range request — truncates the file unless the two agree.

            Only here, not in `ls(detail=True)`: reconciling a whole listing would mean exporting
            every native file in the directory just to measure it. A bulk listing keeps Drive's own
            number, so `ls` and `info` can disagree on a Doc, Sheet or Slide deck — `info` is the
            one that matches what a read returns.
            """
            info = super().info(path, trashed=trashed)
            body = self._export(path) if info.get("type") == "file" else None
            return info if body is None else {**info, "size": len(body)}

        def cat_file(self, path, start=None, end=None, **kwargs):
            body = self._export(path)
            if body is None:
                return super().cat_file(path, start=start, end=end, **kwargs)
            return body[start:end]

        def _open(self, path, mode="rb", **kwargs):
            body = self._export(path) if mode == "rb" else None
            if body is None:
                return super()._open(path, mode=mode, **kwargs)
            return io.BytesIO(body)

    _DRIVE_FS_CLASS = _BacklotDriveFileSystem
    return _DRIVE_FS_CLASS


def drive_filesystem_at(base_url: str, token: str, **kwargs):
    """An fsspec filesystem reading Backlot's Drive API at `base_url`, authenticated by `token`.

    Paths are relative to My Drive: `"folder"`, `"folder/file"`, `""` for the root.
    """
    return _drive_fs_class()(base_url=base_url, token=token, **kwargs)


_GITHUB_FS_CLASSES: dict[str, type] = {}


def _github_fs_class(api: str) -> type:
    """A subclass bound to one API root.

    fsspec's `GithubFileSystem` names api.github.com in six places. Two are class attributes
    (`url`, `content_url`); the other four are built inline inside method bodies, so there is no
    constant to rebind and the methods have to be replaced outright — which is why this is a
    subclass rather than a patcher like the rest of ``backlot.integrations``. The API root is baked
    into the class because the two seams that matter are class attributes, not instance state.

    A seventh host does not appear in that count and is the one worth knowing about: `_open` can
    abandon the contents response for `download_url`, which is a real raw.githubusercontent.com URL
    the SERVER supplies. Counting what the client builds cannot find it; see the `_open` override.
    """
    if api in _GITHUB_FS_CLASSES:
        return _GITHUB_FS_CLASSES[api]

    import base64

    import requests
    from fsspec.implementations.github import GithubFileSystem
    from fsspec.implementations.memory import MemoryFile

    class _BacklotGithubFileSystem(GithubFileSystem):
        """``GithubFileSystem`` pointed at a Backlot server instead of GitHub."""

        url = api + "/repos/{org}/{repo}/git/trees/{sha}"
        content_url = api + "/repos/{org}/{repo}/contents/{path}?ref={sha}"

        def __init__(self, org, repo, token=None, sha=None, timeout=None, **kwargs):
            # Replaces, rather than extends, the default-branch lookup in the parent's __init__:
            # that one builds its URL as a local variable, so passing `sha` is the only way to stop
            # it reaching api.github.com. `timeout` is applied HERE as well as passed down — the
            # parent sets it partway through its own __init__, which is after this request.
            self._token = token
            if timeout is not None:
                self.timeout = timeout
            if sha is None:
                r = requests.get(f"{api}/repos/{org}/{repo}", timeout=self.timeout, **self.kw)
                r.raise_for_status()
                sha = r.json()["default_branch"]
            super().__init__(org=org, repo=repo, sha=sha, timeout=timeout, **kwargs)

        @property
        def kw(self):
            """GitHub's own client sends the token as HTTP Basic (username + PAT); Backlot answers
            the `Authorization: Bearer <t>` / `token <t>` schemes instead."""
            return {"headers": {"Authorization": f"Bearer {self._token}"}} if self._token else {}

        def _refs(self, kind: str) -> list[str]:
            r = requests.get(
                f"{api}/repos/{self.org}/{self.repo}/{kind}", timeout=self.timeout, **self.kw
            )
            r.raise_for_status()
            return [t["name"] for t in r.json()]

        @property
        def branches(self):
            return self._refs("branches")

        @property
        def tags(self):
            return self._refs("tags")

        def _open(self, path, mode="rb", **kwargs):
            """The seventh host, and the only one that is not a URL the client builds.

            Upstream reads the contents response and then abandons it — falling through to
            `http_fs.open(download_url)` — when the decoded bytes open with git-LFS's pointer
            marker. Backlot reports `download_url` as the real
            `https://raw.githubusercontent.com/...`, exactly as GitHub does, so that fallthrough
            leaves Backlot and reads from GitHub's CDN. It also goes out through `http_fs`
            (aiohttp) rather than `requests`, which is why counting the client's own URLs missed it.

            There is nothing to fall through TO here: Backlot has no git-LFS and no >1MB spill, so
            `content` is always populated and a corpus that states a pointer as a file's body means
            those are the bytes.
            """
            if mode != "rb":
                raise NotImplementedError
            url = self.content_url.format(
                org=self.org, repo=self.repo, path=path, sha=kwargs.get("sha") or self.root
            )
            r = requests.get(url, timeout=self.timeout, **self.kw)
            if r.status_code == 404:
                raise FileNotFoundError(path)
            r.raise_for_status()
            return MemoryFile(None, None, base64.b64decode(r.json()["content"]))

        @classmethod
        def repos(cls, org_or_user, is_org=True):
            r = requests.get(
                f"{api}/{['users', 'orgs'][is_org]}/{org_or_user}/repos", timeout=cls.timeout
            )
            r.raise_for_status()
            return [repo["name"] for repo in r.json()]

    _GITHUB_FS_CLASSES[api] = _BacklotGithubFileSystem
    return _BacklotGithubFileSystem


def github_filesystem_at(base_url: str, token: str, org: str, repo: str, **kwargs):
    """An fsspec filesystem over one repo of Backlot's GitHub API, authenticated by `token`.

    Paths are relative to the repo root: `"src"`, `"src/main.py"`, `""` for the top level.
    """
    api = f"{base_url.rstrip('/')}/github"
    return _github_fs_class(api)(org=org, repo=repo, token=token, **kwargs)
