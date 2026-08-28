# Using fsspec against Backlot

[fsspec](https://filesystem-spec.readthedocs.io/) is the filesystem interface the Python data stack
reads through: `pandas`, `pyarrow`, `dask`, `Ray` and LlamaIndex all resolve a URL like
`s3://bucket/key` by handing it to fsspec, which hands it to whichever implementation is registered
for that scheme. Point the implementation at Backlot and everything above it follows — a
`read_csv("s3://…")` in code you did not write now reads your corpus.

```bash
pip install -e ".[fsspec]"
python examples/using-fsspec/s3.py                                            # local throwaway server
python examples/using-fsspec/s3.py --url http://localhost:8000 --access-key <AKIA...> --secret-key <secret>
python examples/using-fsspec/gdrive.py --url http://localhost:8000 --token <usr-token>
python examples/using-fsspec/github.py --url http://localhost:8000 --token <usr-token> --repo pipeline
```

Each script spins up its own throwaway mock on a tiny in-code corpus, reads it through fsspec, and
finishes by loading a table into pandas. Pass `--url` to drive an already-running mock instead. All
reads are ACL-scoped by the credential you pass, exactly as against the real API.

| Source | fsspec implementation | How it's pointed at Backlot |
|--------|----------------------|------------------------------|
| S3 | [`s3fs`](https://s3fs.readthedocs.io/) (`s3://`) | `client_kwargs={"endpoint_url": f"{base_url}/s3"}` — an ordinary constructor argument |
| Google Drive | [`gdrive-fsspec`](https://github.com/fsspec/gdrive-fsspec) (`gdrive://`) | [`drive_filesystem_at()`](../../backlot/integrations/fsspec.py) — it has no endpoint argument |
| GitHub | `fsspec.implementations.github` (`github://`) | [`github_filesystem_at()`](../../backlot/integrations/fsspec.py) — it has no endpoint argument either |

## Per-source notes

- **S3** (`s3.py`): the easy one. s3fs takes the endpoint as a constructor argument and SigV4-signs
  against it, so there is no shim — `storage_options` in the script is the entire redirect, and the
  same dict works for `fsspec.open`, `fsspec.filesystem` and `pandas.read_csv`. The script does call
  [`patch_s3fs_walk()`](../../backlot/integrations/llamaindex.py), which is **not** a mock concern:
  s3fs's async `_walk` passes `topdown` down to an `_ls` that does not accept it, so `fs.find()` and
  `fs.walk()` raise against real AWS too. The shim strips the kwarg and no-ops itself once upstream
  accepts it. It lives in the `llamaindex` module because that is where the bug was first hit; it is
  the same shim wherever you reach it from.

- **Google Drive** (`gdrive.py`): the one that needs
  [`backlot.integrations.fsspec`](../../backlot/integrations/fsspec.py). `gdrive_fsspec` builds its
  Google Drive service with a bare `build("drive", "v3")` — no endpoint argument anywhere — so
  `drive_filesystem_at()` supplies one, and works around three of its defects along the way. **None
  of the three is about Backlot; all three reproduce against real Google Drive:**

  | Defect | What you see | Why |
  |---|---|---|
  | Cached parent listing | `ls("folder")` returns `["folder"]` instead of its children, so every recursive walk stops one level in | `ls` consults `_ls_from_cache`, which answers out of the cached *parent* listing with that directory's own entry |
  | Leading slash | `ls("/folder")` raises `FileNotFoundError` | its paths are relative and `_strip_protocol` leaves the `/` on |
  | No export | reading a Doc, Sheet or Slide deck fails with 403 `fileNotDownloadable` | a Google-native file stores no bytes; it only ever calls `alt=media`, never `files.export` |

  The third is why `gdrive.py` can turn a Google Sheet into a DataFrame at all: Google Drive
  exports Sheets as CSV, and the filesystem falls back to `files.export` when a file has no binary
  content. One consequence worth knowing — a listing carries Google Drive's own `size` (the stored
  content), while a read returns the longer export. `fs.info(path)["size"]` is reconciled with what
  a read returns;
  `fs.ls(..., detail=True)` is not, because reconciling a listing would mean exporting every native
  file in it just to measure.

- **GitHub** (`github.py`): a repo's file tree as a filesystem, over the Contents and Git Trees
  APIs. `GithubFileSystem` names `api.github.com` in six places and only two — the class attributes
  `url` and `content_url` — can be rebound; the other four are inline f-strings inside `__init__`,
  `repos`, `tags` and `branches`, so `github_filesystem_at` is a **subclass** that replaces those
  methods rather than a patcher like everything else in
  [`backlot.integrations`](../../backlot/integrations/). Missing even one would leave a "mock" run
  quietly reading the real GitHub, which is why a test asserts that no URL the filesystem requests
  names `github.com`. It also swaps HTTP Basic (what GitHub's own clients send) for the bearer
  scheme Backlot answers.

  A seventh host does not show up in that count of six, because the client does not build it: a
  file whose bytes open with git-LFS's pointer marker makes upstream's `_open` abandon the contents
  response and fetch `download_url`, which Backlot reports — faithfully — as the real
  `raw.githubusercontent.com`. `_open` is replaced too. Backlot has no LFS and no >1MB spill, so
  the contents response always carries the bytes and there is nothing to fall through to.

  Two caveats: `fs.branches` and `fs.tags` are pointed at Backlot but Backlot serves no
  `/branches` or `/tags` **listing** yet — only `/branches/{branch}` — so they 404 rather than
  reaching GitHub ([#92](https://github.com/brekkylab/backlot/issues/92)). And walking a repo works
  only because `git/trees/{ref}` resolves a **subtree** sha: a client descends by the sha it read
  from the parent listing, and answering the repo root for every ref makes `ls("src")` report
  `src/src` and `src/config` and a walk recurse until it runs out of stack.

- **Everything else Backlot serves** — Slack, Gmail, Notion, Jira, Confluence, Linear, HubSpot,
  Fireflies — has no fsspec implementation at all, from anyone. They are not filesystem-shaped, and
  fsspec's registry has no entry for them. Read those through their own SDKs
  ([`examples/using-official-sdk/`](../using-official-sdk/)) or LlamaIndex
  ([`examples/using-llamaindex-readers/`](../using-llamaindex-readers/)).

## Mounting it as a real filesystem

fsspec has a FUSE bridge (`fsspec.fuse`), but if you want Backlot as a directory you can `ls` and
`grep` from any process, [`examples/using-mirage/`](../using-mirage/) already does it better: a
`--fuse` flag on every script, six sources rather than two, and a single mountpoint that serves all
of them at once. This directory is about the *interface* the data stack reads through, not the
kernel mount.
