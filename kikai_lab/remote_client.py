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
  kikai remote op <project> --file req.json          # run op; script events auto-extracted
  kikai remote submit-from <project> <run> <parent> --overrides-file f.json
  kikai remote stop <project> <run>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

EVENT_RE = re.compile(r'\{"event"[^\n]+')


def _base_url(args: argparse.Namespace) -> str:
    url = getattr(args, "base_url", None) or os.environ.get("KIKAI_SERVER_URL")
    if not url:
        raise SystemExit("set --base-url or KIKAI_SERVER_URL")
    return str(url).rstrip("/")


def _http(
    method: str, url: str, body: dict[str, Any] | None = None, timeout: int = 600
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
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
    print(
        f"qc_done={len(p.get('qc_done_steps') or [])} probes={{{probes}}} "
        f"gave_up={p.get('op_gave_up') or '-'}"
    )
    if p.get("last_error"):
        print(f"last_error: {p['last_error']}")
    for line in _err_lines(env):
        print(line)
    return 0


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

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(command_remote(sys.argv[1:]))
