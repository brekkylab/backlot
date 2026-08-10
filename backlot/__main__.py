"""``python -m backlot`` — the same CLI as the ``backlot`` console script.

Worth having for the case where the script is not on PATH: a venv that has not been activated, or
`pipx run`. It is also the spelling `python -m backlot.main` looked like it should be but never
was — that module only defines the ASGI app, so running it imported everything and exited.
"""

from backlot.cli import main

raise SystemExit(main())
