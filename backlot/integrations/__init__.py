"""Point third-party clients at a Backlot server.

Most official clients take a base-URL argument and need nothing from here — pass
``f"{m.base_url}/slack/api/"`` and you are done; see the README. These modules exist for the ones
that DO NOT: a client that hardcodes its vendor host can only be redirected by rebinding a module
attribute, and getting that right differs per client. Each function's docstring names the seam it
uses and why that one.

Every patcher is idempotent and fails loudly if its seam disappears in a client upgrade, rather
than silently letting a run that was supposed to hit the mock reach the real vendor.
"""
