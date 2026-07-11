"""``kikai remote`` — terse CLI client for the kikai server HTTP API.

WHY: driving the server from an agent/terminal used to mean ``curl … | python -c``
with an ad-hoc JSON-parsing snippet per call — dozens of lines of boilerplate per
interaction and a full JSON envelope echoed every time. This client prints ONLY
the decision-relevant fields (1-3 lines); ``--json`` restores the raw envelope
when the details are actually needed.

Server URL resolution: ``--base-url`` flag, else ``KIKAI_SERVER_URL`` env.

Subcommands::

  kikai remote daemon <project>                      # heartbeat one-liner
  kikai remote run <project> <run>                   # status + progress digest
  kikai remote metrics <project> <run> --keys a,b    # first/quartile/last trend
  kikai remote artifacts <project> <run> [--path d]  # run_dir listing (ssh-free)
  kikai remote artifacts <project> <run> --file f    # small text file content
  kikai remote op <project> --file req.json          # run op; script events auto-extracted
  kikai remote submit-from <project> <run> <parent> --overrides-file f.json
  kikai remote stop <project> <run>
  kikai remote bundle-put <project> <bundle> --dir d # tar a dir -> PUT bundle
  kikai remote container-put <project> <id> --file f # PUT container record (json/yaml)
  kikai remote qc-config <project> <run> --file f    # live probes/qc_op update
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

EVENT_RE = re.compile(r'\{"event"[^\n]+')


def _base_url(args: argparse.Namespace) -> str:
    url = getattr(args, "base_url", None) or os.environ.get("KIKAI_SERVER_URL")
    if not url:
        raise SystemExit("set --base-url or KIKAI_SERVER_URL")
    return str(url).rstrip("/")


def _http(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    timeout: int = 600,
    *,
    raw: bytes | None = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    """JSON envelope round-trip. ``raw`` sends a pre-encoded byte body instead
    (e.g. a tar upload); ``content_type`` then names its media type."""
    if raw is not None:
        data: bytes | None = raw
    else:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        content_type = "application/json"
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": content_type}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # envelope errors still carry JSON; a non-JSON body degrades to a
        # synthetic envelope instead of a traceback.
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {
                "ok": False,
                "errors": [{"code": f"http.{exc.code}", "message": str(exc)}],
            }


def _err_lines(env: dict[str, Any]) -> list[str]:
    out = []
    for e in env.get("errors") or []:
        det = e.get("details") or {}
        line = f"ERR {e.get('code')}"
        tail = det.get("stderr") or det.get("stdout_tail") or e.get("message") or ""
        if tail:
            line += f" :: {str(tail)[-300:]}"
        out.append(line)
    return out


def _print_json(env: dict[str, Any]) -> int:
    print(json.dumps(env, ensure_ascii=False, indent=1))
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    env = _http("GET", f"{_base_url(args)}/v1/projects/{args.project}/daemon")
    if args.json:
        return _print_json(env)
    st = (env.get("data") or {}).get("state") or {}
    lp = st.get("last_pass") or {}
    n_err = (
        len(lp.get("errors") or [])
        + len(lp.get("qc_errors") or {})
        + len(lp.get("probe_errors") or {})
    )
    since = round((env.get("data") or {}).get("seconds_since_update", -1))
    print(
        f"phase={st.get('phase', '-')} current={st.get('current_run_id') or '-'} "
        f"since={since}s runs={lp.get('managed_runs', '-')} errors={n_err}"
    )
    if n_err:
        print("qc_errors:", json.dumps(lp.get("qc_errors"), ensure_ascii=False)[:200])
        print("probe_errors:", json.dumps(lp.get("probe_errors"), ensure_ascii=False)[:200])
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    env = _http("GET", f"{_base_url(args)}/v1/projects/{args.project}/runs/{args.run}")
    if args.json:
        return _print_json(env)
    d = env.get("data") or {}
    c = d.get("container") or {}
    m = d.get("latest_metrics") or {}
    p = d.get("progress") or {}
    loss = round(m["loss"], 3) if "loss" in m else "-"
    print(
        f"status={d.get('derived_status')} running={c.get('running')} "
        f"step={m.get('step', '-')} loss={loss}"
    )
    pd = p.get("probes_done_steps") or {}
    probes = ", ".join(f"{k}:{len(v)}" for k, v in pd.items()) or "-"
    fails = p.get("op_fail_counts") or {}
    fail_text = ", ".join(f"{k}:{v}" for k, v in fails.items()) or "-"
    print(
        f"qc_done={len(p.get('qc_done_steps') or [])} probes={{{probes}}} "
        f"fails={{{fail_text}}} gave_up={p.get('op_gave_up') or '-'}"
    )
    bad = _delivery_failures(p)
    if bad:
        print(f"delivery_failures: {json.dumps(bad, ensure_ascii=False)[:300]}")
    if p.get("last_error"):
        print(f"last_error: {p['last_error']}")
    for line in _err_lines(env):
        print(line)
    return 0


def _delivery_failures(progress: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """Non-2xx delivery outcomes from progress['delivery'] (newest last)."""
    out = []
    for key, entry in (progress.get("delivery") or {}).items():
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if isinstance(status, int) and 200 <= status < 300:
            continue
        out.append({"key": key, **entry})
    return out[-limit:]


def cmd_artifacts(args: argparse.Namespace) -> int:
    base = f"{_base_url(args)}/v1/projects/{args.project}/runs/{args.run}/artifacts"
    if args.file:
        query = urllib.parse.urlencode(
            {
                "path": args.file,
                "max_bytes": args.max_bytes,
                "tail": "true" if args.tail else "false",
            }
        )
        env = _http("GET", f"{base}/file?{query}")
        if args.json:
            return _print_json(env)
        d = env.get("data") or {}
        if env.get("ok") and not d.get("binary"):
            if d.get("truncated"):
                which = "last" if d.get("tail") else "first"
                print(f"# truncated: {which} {args.max_bytes} of {d.get('size')} bytes")
            sys.stdout.write(d.get("content") or "")
        elif env.get("ok"):
            print(f"binary size={d.get('size')} (metadata only; no content served)")
        for line in _err_lines(env):
            print(line)
        return 0 if env.get("ok") else 1
    query = urllib.parse.urlencode({"path": args.path, "depth": args.depth})
    env = _http("GET", f"{base}?{query}")
    if args.json:
        return _print_json(env)
    d = env.get("data") or {}
    for e in d.get("entries") or []:
        kind = "d" if e.get("is_dir") else "f"
        size = "-" if e.get("size") is None else e["size"]
        print(f"{kind} {size:>12} {e.get('path')}")
    total = d.get("total", 0)
    if env.get("ok"):
        print(f"total={total}" + (" (truncated)" if d.get("truncated") else ""))
    for line in _err_lines(env):
        print(line)
    return 0 if env.get("ok") else 1


def cmd_metrics(args: argparse.Namespace) -> int:
    env = _http(
        "GET",
        f"{_base_url(args)}/v1/projects/{args.project}/runs/{args.run}"
        f"/metrics?keys={args.keys}&max_points={args.max_points}",
    )
    if args.json:
        return _print_json(env)
    d = env.get("data") or {}
    steps = d.get("step") or []
    if not steps:
        print("no metrics")
        return 0
    for k, v in (d.get("series") or {}).items():
        if not v:
            continue
        n = len(v)
        idx = sorted({0, n // 4, n // 2, 3 * n // 4, n - 1})
        print(f"{k:>18s}  " + "  ".join(f"{steps[i]}:{v[i]:.4g}" for i in idx))
    return 0


def cmd_op(args: argparse.Namespace) -> int:
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            body = json.load(f)
    elif args.body:
        body = json.loads(args.body)
    else:
        raise SystemExit("pass --file or --body")
    env = _http(
        "POST",
        f"{_base_url(args)}/v1/projects/{args.project}/operations",
        body,
        timeout=args.timeout,
    )
    if args.json:
        return _print_json(env)
    r = (env.get("data") or {}).get("result") or {}
    print(f"ok={env.get('ok')} execution={r.get('execution_status') or '-'}")
    for ev in EVENT_RE.findall(r.get("stdout") or "")[-args.events:]:
        print(" ", ev[:200])
    for line in _err_lines(env):
        print(line)
    return 0 if env.get("ok") else 1


def cmd_submit_from(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.overrides_file:
        with open(args.overrides_file, encoding="utf-8") as f:
            body["overrides"] = json.load(f)
    if args.dry_run:
        body["dry_run"] = True
    env = _http(
        "POST",
        f"{_base_url(args)}/v1/projects/{args.project}/runs/{args.run}"
        f"/submit-from/{args.parent}",
        body,
    )
    if args.json:
        return _print_json(env)
    d = env.get("data") or {}
    print(
        f"ok={env.get('ok')} submitted={d.get('submitted')} "
        f"status={d.get('derived_status') or '-'}"
    )
    for line in _err_lines(env):
        print(line)
    return 0 if env.get("ok") else 1


def cmd_stop(args: argparse.Namespace) -> int:
    env = _http(
        "POST", f"{_base_url(args)}/v1/projects/{args.project}/runs/{args.run}/stop", {}
    )
    print(f"ok={env.get('ok')}")
    for line in _err_lines(env):
        print(line)
    return 0 if env.get("ok") else 1


BUNDLE_MANIFEST_NAME = "kikai_bundle.json"
# macOS junk that hand-rolled `tar` on a Mac smuggles into uploads: AppleDouble
# resource forks (``._*``), Finder metadata, and the zip-era ``__MACOSX`` dir.
_MACOS_JUNK_NAMES = {".DS_Store"}


def _is_macos_junk(relative: Path) -> bool:
    return (
        "__MACOSX" in relative.parts
        or relative.name.startswith("._")
        or relative.name in _MACOS_JUNK_NAMES
    )


def _build_bundle_tar(directory: Path) -> tuple[bytes, int]:
    """Tar every regular file under ``directory`` (paths relative to it),
    excluding macOS junk. tarfile — not the ``tar`` binary — so no AppleDouble
    members and no shell-quoting hazards."""
    buf = io.BytesIO()
    count = 0
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in sorted(directory.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            relative = p.relative_to(directory)
            if _is_macos_junk(relative):
                continue
            tar.add(p, arcname=relative.as_posix(), recursive=False)
            count += 1
    return buf.getvalue(), count


def cmd_bundle_put(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    if not directory.is_dir():
        raise SystemExit(f"not a directory: {directory}")
    if not (directory / BUNDLE_MANIFEST_NAME).is_file():
        raise SystemExit(
            f"missing {BUNDLE_MANIFEST_NAME} at bundle root: {directory} "
            '(e.g. {"entrypoints": {"train": {"argv": ["python", "train.py"]}}})'
        )
    body, n_files = _build_bundle_tar(directory)
    if not n_files:
        raise SystemExit(f"no files to upload under: {directory}")
    env = _http(
        "PUT",
        f"{_base_url(args)}/v1/projects/{args.project}/bundles/{args.bundle_id}",
        raw=body,
        content_type="application/x-tar",
        timeout=args.timeout,
    )
    if args.json:
        return _print_json(env)
    d = env.get("data") or {}
    entrypoints = ",".join(sorted(d.get("entrypoints") or {})) or "-"
    print(
        f"ok={env.get('ok')} created={bool(d.get('created'))} "
        f"files={d.get('file_count', '-')} entrypoints={entrypoints}"
    )
    if d.get("already_exists"):
        print("already_exists=True (identical content; bundles are immutable)")
    for line in _err_lines(env):
        print(line)
    return 0 if env.get("ok") else 1


def _load_record_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        import yaml

        record = yaml.safe_load(text)
    else:
        try:
            record = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}: not parseable JSON ({exc})") from exc
    if not isinstance(record, dict):
        raise SystemExit(f"{path}: expected a JSON/YAML object at top level")
    return record


def cmd_container_put(args: argparse.Namespace) -> int:
    record = _load_record_file(args.file)
    env = _http(
        "PUT",
        f"{_base_url(args)}/v1/projects/{args.project}/containers/{args.container_id}",
        record,
    )
    if args.json:
        return _print_json(env)
    d = env.get("data") or {}
    outcome = next(
        (k for k in ("created", "updated", "already_exists") if d.get(k)), "-"
    )
    print(f"ok={env.get('ok')} outcome={outcome}")
    for line in _err_lines(env):
        print(line)
    return 0 if env.get("ok") else 1


def cmd_qc_config(args: argparse.Namespace) -> int:
    with open(args.file, encoding="utf-8") as f:
        try:
            body = json.load(f)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{args.file}: not parseable JSON ({exc})") from exc
    env = _http(
        "POST",
        f"{_base_url(args)}/v1/projects/{args.project}/runs/{args.run}/qc-config",
        body,
    )
    if args.json:
        return _print_json(env)
    d = env.get("data") or {}
    updated = ",".join(d.get("updated") or []) or "-"
    removed = ",".join(d.get("removed") or [])
    warnings = ",".join(str(w.get("code")) for w in env.get("warnings") or []) or "-"
    summary = f"ok={env.get('ok')} updated={updated}"
    if removed:
        summary += f" removed={removed}"
    print(f"{summary} warnings={warnings}")
    for line in _err_lines(env):
        print(line)
    return 0 if env.get("ok") else 1


def command_remote(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="kikai remote")
    p.add_argument("--base-url", default=None)
    sub = p.add_subparsers(dest="sub", required=True)

    s = sub.add_parser("daemon")
    s.add_argument("project")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_daemon)

    s = sub.add_parser("run")
    s.add_argument("project")
    s.add_argument("run")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("metrics")
    s.add_argument("project")
    s.add_argument("run")
    s.add_argument("--keys", default="loss")
    s.add_argument("--max-points", type=int, default=40)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_metrics)

    s = sub.add_parser("artifacts")
    s.add_argument("project")
    s.add_argument("run")
    s.add_argument("--path", default="", help="relative dir inside the run_dir")
    s.add_argument("--depth", type=int, default=1)
    s.add_argument("--file", default="", help="fetch this relative file instead")
    s.add_argument("--max-bytes", type=int, default=65536)
    s.add_argument("--tail", action="store_true", help="last max-bytes of --file")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_artifacts)

    s = sub.add_parser("op")
    s.add_argument("project")
    s.add_argument("--file", default="")
    s.add_argument("--body", default="")
    s.add_argument("--timeout", type=int, default=600)
    s.add_argument("--events", type=int, default=4)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_op)

    s = sub.add_parser("submit-from")
    s.add_argument("project")
    s.add_argument("run")
    s.add_argument("parent")
    s.add_argument("--overrides-file", default="")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_submit_from)

    s = sub.add_parser("stop")
    s.add_argument("project")
    s.add_argument("run")
    s.set_defaults(fn=cmd_stop)

    s = sub.add_parser("bundle-put")
    s.add_argument("project")
    s.add_argument("bundle_id")
    s.add_argument("--dir", required=True)
    s.add_argument("--timeout", type=int, default=600)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_bundle_put)

    s = sub.add_parser("container-put")
    s.add_argument("project")
    s.add_argument("container_id")
    s.add_argument("--file", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_container_put)

    s = sub.add_parser("qc-config")
    s.add_argument("project")
    s.add_argument("run")
    s.add_argument("--file", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_qc_config)

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(command_remote(sys.argv[1:]))
