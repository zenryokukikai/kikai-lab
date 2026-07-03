# Security model

Read this before exposing a kikai server on any network.

## What kikai is

kikai launches and manages **training containers** on the host it runs on. The
submission and operations endpoints can therefore start Docker containers, mount
host directories, and run arbitrary commands inside them. **Anyone who can reach
the API can run code on the host.** Treat the API surface exactly as you would a
Docker socket.

## Defaults are safe; exposure is opt-in

- The server **binds `127.0.0.1` by default.** Reaching it from another machine
  requires an explicit `--host 0.0.0.0` (or a proxy). Localhost-only,
  single-operator use needs no further configuration.
- There is **no built-in user management.** The one built-in gate is an optional
  **shared bearer token**:

  ```bash
  kikai server start --projects-root /srv/kikai --host 0.0.0.0 \
    --auth-token "$(openssl rand -hex 32)"
  # or: export KIKAI_AUTH_TOKEN=...   (keeps the secret out of the process list)
  ```

  With a token set, every request except `GET /healthz` must send
  `Authorization: Bearer <token>` or receive `401`. The comparison is
  constant-time. This is a single shared secret, not per-user auth.

## If you expose it beyond a trusted network

The bearer token is the floor, not the ceiling. For anything past a private VLAN:

1. **Terminate TLS at a reverse proxy** (nginx/Caddy) — the token travels in a
   header and must not cross the wire in cleartext.
2. **Set `--run-dir-root` / `--content-root`** to contain filesystem reads.
   Without them, `run_dir`-based reads and artifact content serving are not
   path-restricted (acceptable only for single-operator localhost).
3. **Run the server as an unprivileged user** in the `docker` group rather than
   root, and scope which images/mounts its container profiles allow.
4. Consider per-user auth, rate limiting, and audit logging at the proxy layer —
   kikai does not provide them.

## Reporting a vulnerability

Please report security issues privately to the maintainers (see the repository
contact) rather than opening a public issue. We will acknowledge within a
reasonable window and coordinate a fix and disclosure.
