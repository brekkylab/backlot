"""A spreadsheet's cells: normalising what a corpus stated, and serialising it the way Drive's
export does.

Shared by the importer -- which derives ``gdrive_files.content`` from the first sheet -- and the
export route, which serves TSV from the grid. Keeping both on one module is what stops the Sheets
API and Drive from describing one document two ways.
"""

Cell = str | int | float | bool | None


def _scalar(v):
    """One cell as it is stored. An integral float collapses to an int: measured, writing 3.0 to a
    real sheet stores ``numberValue: 3`` and formats as ``"3"``, so keeping the float would serve a
    value the real API cannot hold.

    ``bool`` is tested first because it is a subclass of ``int`` in Python -- an unguarded numeric
    branch would take ``True`` and hand back 1."""
    if isinstance(v, bool) or not isinstance(v, float):
        return v
    return int(v) if v.is_integer() else v


def normalise_grid(grid: list[list]) -> list[list[Cell]]:
    """A rectangular grid: short rows padded with empty cells to the width of the widest row.

    Padding rather than refusing a ragged grid: a table whose last row is short is ordinary, and a
    corpus author should not have to pad a sixteen-column one by hand. Everything downstream may
    assume rectangularity, which is what lets the serving layer index a row by column without a
    bounds check on every cell."""
    width = max((len(r) for r in grid), default=0)
    return [[_scalar(r[i]) if i < len(r) else None for i in range(width)] for r in grid]


def formatted(cell: Cell) -> str:
    """A cell's ``formattedValue`` -- its display string, which is also what export serialises.

    Measured: a boolean displays as TRUE/FALSE, an integral number without a decimal point, and an
    empty cell as the empty string."""
    if cell is None:
        return ""
    if isinstance(cell, bool):
        return "TRUE" if cell else "FALSE"
    if isinstance(cell, str):
        return cell
    return str(cell)


def used_extent(grid: list[list[Cell]]) -> tuple[int, int]:
    """``(rows, cols)`` counting only as far as the last row and column holding anything.

    Measured: export pads its rows out to the sheet's last USED column, so an all-empty trailing
    column contributes no field at all -- a three-column grid whose third column is empty exports
    two fields per row, not three."""
    rows = cols = 0
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if formatted(cell) != "":
                rows = r + 1
                cols = max(cols, c + 1)
    return rows, cols


def _rows_used(grid: list[list[Cell]]) -> list[list[str]]:
    rows, cols = used_extent(grid)
    return [[formatted(grid[r][c]) for c in range(cols)] for r in range(rows)]


def _csv_field(v: str) -> str:
    if any(ch in v for ch in (",", '"', "\n")):
        return '"' + v.replace('"', '""') + '"'
    return v


def to_csv(grid: list[list[Cell]]) -> str:
    """The grid as ``files.export?mimeType=text/csv`` serialises it.

    Measured, and hand-rolled rather than handed to :mod:`csv` because the stdlib cannot produce
    this exact dialect: a field is quoted for a comma, a double quote or a newline but NOT for a
    tab; an embedded newline stays a bare ``\\n`` inside the quotes while rows are separated by
    ``\\r\\n``; leading and trailing spaces are preserved unquoted; and there is no trailing
    newline. ``csv.writer`` with ``lineterminator="\\r\\n"`` emits that trailing newline and quotes
    on a bare ``\\r`` as well.

    Rows are rectangular to the last used column, unlike ``values.get``, which trims each row
    independently and so answers ragged."""
    return "\r\n".join(",".join(_csv_field(v) for v in row) for row in _rows_used(grid))


def to_tsv(grid: list[list[Cell]]) -> str:
    """The grid as ``files.export?mimeType=text/tab-separated-values`` serialises it.

    Measured: TSV has NO quoting mechanism. An embedded newline and an embedded tab each collapse
    to a single space, and a double quote passes through bare. The conversion is lossy, and
    agreeing with the real API means reproducing that loss rather than escaping around it."""
    return "\r\n".join(
        "\t".join(v.replace("\n", " ").replace("\t", " ") for v in row) for row in _rows_used(grid)
    )
