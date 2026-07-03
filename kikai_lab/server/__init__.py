"""kikai server — HTTP API + dashboard over a projects root of file registries.

See docs/plans/2026-07-02-kikai-http-server.md. The server is a trusted in-process
caller (same precedent as the reconcile daemon): typed API requests are turned into
operation requests by the server itself; the guard-receipt dance stays with
human-edited operation files.
"""
