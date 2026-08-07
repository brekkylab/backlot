"""Plumbing shared by the example directories that is NOT package API — see ``google_creds``
(mock-specific OAuth glue) and ``syspath`` (keep a same-named script from shadowing the
third-party package it imports). The mock-spinup/`--url` logic these used to also carry lives in
``backlot`` itself now (``backlot.mock_server`` / ``backlot.serve_or_connect``)."""
