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
  kikai remote finalize <project> <run>              # force-finalize: stop QC/probe backfill
  kikai remote ps <project>                          # kikai-managed containers (ssh-free docker ps)
  kikai remote bundles <project>                     # bundle list (ssh-free)
  kikai remote bundle-get <project> <bundle>         # entrypoints/argv detail
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

# single source of truth for the delivery vocabulary/counting (the server's
# /status digest calls the same function) — a second local implementation is
# exactly how the two surfaces would drift apart again.
from kikai_lab.reconcile import (
    as_mapping,
    clip_text,
    delivery_confirmed,
    delivery_outcome,
    delivery_summary,
    int_steps,
)

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
    # EVERY field below goes through as_mapping/int_steps, not `or {}`: this is
    # the command an operator runs BECAUSE a run looks wrong, so a progress.json
    # that is truncated, hand-edited or written by an older schema must cost
    # information and nothing else. `or {}` only defends against null — a list,
    # a string or a scalar sailed straight into `.items()` / `len()` and killed
    # the digest before it printed a single delivery line.
    d = as_mapping(env.get("data"))
    c = as_mapping(d.get("container"))
    m = as_mapping(d.get("latest_metrics"))
    p = as_mapping(d.get("progress"))
    loss = round(m["loss"], 3) if isinstance(m.get("loss"), (int, float)) else "-"
    print(
        f"status={d.get('derived_status')} running={c.get('running')} "
        f"step={m.get('step', '-')} loss={loss}"
    )
    # counted with the SAME helper as delivery_summary's `expected`, so
    # qc_done + probes and the delivery denominator can never disagree
    pd = as_mapping(p.get("probes_done_steps"))
    probes = ", ".join(f"{k}:{len(int_steps(v))}" for k, v in pd.items()) or "-"
    fails = as_mapping(p.get("op_fail_counts"))
    fail_text = ", ".join(f"{k}:{v}" for k, v in fails.items()) or "-"
    print(
        f"qc_done={len(int_steps(p.get('qc_done_steps')))} probes={{{probes}}} "
        f"fails={{{fail_text}}} gave_up={p.get('op_gave_up') or '-'}"
    )
    # scale FIRST, samples second: printing only the last 5 failures made a
    # 60-of-60 delivery blackout read like a couple of recent hiccups.
    summary = delivery_summary(p)
    # printed whenever ANY QC/probe op ran — suppressing on total==0 turned
    # "60 QC steps, not one delivery record" back into silence.
    if summary["expected"] or summary["total"]:
        print(
            f"delivery={summary['delivered']}/{summary['expected']} delivered"
            f" (failed={summary['failed']} skipped={summary['skipped']}"
            f" unverified={summary['unverified']} unrecorded={summary['unrecorded']}"
            f" {{{_delivery_reasons_text(summary['reasons'])}}})"
        )
    bad = _delivery_failures(p)
    if bad:
        print(f"delivery_failures: {_delivery_tail_text(bad)}")
    if p.get("last_error"):
        print(f"last_error: {p['last_error']}")
    for line in _err_lines(env):
        print(line)
    return 0


def _delivery_failures(progress: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """Non-delivered outcomes from progress['delivery'] (newest last), each with
    its explicit ``outcome`` — same classifier as the server tail."""
    out = []
    # a corrupt progress.json degrades to "no rows", never to an exception
    for key, entry in as_mapping(as_mapping(progress).get("delivery")).items():
        if not isinstance(entry, dict):
            continue
        if delivery_confirmed(entry):
            continue
        out.append({"key": key, **entry, "outcome": delivery_outcome(entry)})
    return out[-limit:]


# CLI budgets. Both are spent through `clip_text`/whole-row drops, so a cut is
# always visible as `…` — an unmarked cut reads as a complete message.
DELIVERY_REASONS_MAX = 200
DELIVERY_TAIL_MAX = 300
DELIVERY_TAIL_DETAIL_MAX = 48


def _delivery_reasons_text(reasons: dict[str, Any]) -> str:
    """The `reasons` buckets, deterministically ordered and marked when cut.

    Sorted by count DESC then key, so (a) the same summary always prints the
    same line and (b) a cut drops the SMALLEST buckets. Dict insertion order
    made "which buckets survive the cut" depend on the order records happened
    to be written — arbitrary, and different between two reads of the same run."""
    items = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    return clip_text(", ".join(f"{k}:{v}" for k, v in items), DELIVERY_REASONS_MAX) or "-"


def _delivery_tail_row(row: dict[str, Any]) -> str:
    """One failure row as ``key=outcome(status):detail``.

    WHY not `json.dumps`: the tail is printed under a fixed char budget, and a
    JSON blob spends most of it on braces, quotes and repeated field names —
    adding the `outcome` field alone cut the visible rows from 4 to 3 (1-2 for
    `post_failed` rows, whose error text is long), and the cut landed mid-object
    so the last row was unparseable AND unmarked."""
    outcome = row.get("outcome") or delivery_outcome(row)
    text = f"{row.get('key')}={outcome}"
    status = row.get("status")
    if isinstance(status, int):
        text += f"({status})"
    detail = str(row.get("skipped_reason") or "")
    # `skipped_reason` repeats the outcome for post_failed/no_delivery_event
    # records; the row already states it, so print only what it ADDS.
    #
    # Stripped only on a SEPARATOR or an exact match. A bare `startswith` cut
    # into reasons that legitimately BEGIN with the outcome word: a script
    # reporting `skipped_by_config` printed `skipped:_by_config`, and
    # `no_delivery_eventual` printed `no_delivery_event:ual` — mangled text in
    # the one field whose job is to be read literally.
    if detail == outcome:
        detail = ""
    else:
        for sep in (":", " "):
            if detail.startswith(f"{outcome}{sep}"):
                detail = detail[len(outcome) + len(sep) :]
                break
    if detail:
        text += f":{clip_text(detail, DELIVERY_TAIL_DETAIL_MAX)}"
    return text


def _delivery_tail_line(parts: list[str], dropped: int) -> str:
    """The tail exactly as printed: drop-marker first, then the kept rows.

    The marker is rendered HERE rather than prepended after the accounting, so
    it is charged against the same budget as everything else — it used to be
    free, and a "300-char" line measured 313."""
    marker = f"…(+{dropped} older)" if dropped else ""
    return " ".join([part for part in [marker, *parts] if part])


def _delivery_tail_text(rows: list[dict[str, Any]]) -> str:
    """The failure tail under DELIVERY_TAIL_MAX chars, cut on ROW boundaries.

    Newest rows are kept (they are the tail's whole point) and a drop is stated
    with a count — the reader never has to guess whether the list is complete."""
    parts = [_delivery_tail_row(r) for r in rows]
    if not parts:
        return ""
    # at least one row ALWAYS survives; whether it fits is decided below
    kept = parts[-1:]
    for start in range(len(parts) - 2, -1, -1):
        if len(_delivery_tail_line(parts[start:], start)) > DELIVERY_TAIL_MAX:
            break
        kept = parts[start:]
    dropped = len(parts) - len(kept)
    line = _delivery_tail_line(kept, dropped)
    if len(line) <= DELIVERY_TAIL_MAX:
        return line
    # The newest row alone is wider than the whole budget (a long probe id plus
    # a long detail). Clip the ROW with the shared marker instead of dropping
    # it: `delivery_failures: …(+1 older)` announces a failure and then says
    # nothing about it, which is the silence this whole change set removes.
    marker = f"…(+{dropped} older) " if dropped else ""
    return marker + clip_text(kept[0], max(DELIVERY_TAIL_MAX - len(marker), 1))


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


def cmd_finalize(args: argparse.Namespace) -> int:
    query = "?cancel=true" if getattr(args, "cancel", False) else ""
    env = _http(
        "POST",
        f"{_base_url(args)}/v1/projects/{args.project}/runs/{args.run}/finalize{query}",
        {},
    )
    if getattr(args, "json", False):
        return _print_json(env)
    d = env.get("data") or {}
    stopped = d.get("stopped_containers") or []
    print(
        f"ok={env.get('ok')} requested={d.get('finalize_requested')} "
        f"already={d.get('already_finalized')} "
        f"stopped={','.join(stopped) if stopped else '-'}"
    )
    for line in _err_lines(env):
        print(line)
    return 0 if env.get("ok") else 1


def cmd_ps(args: argparse.Namespace) -> int:
    env = _http("GET", f"{_base_url(args)}/v1/projects/{args.project}/docker/ps")
    if getattr(args, "json", False):
        return _print_json(env)
    rows = (env.get("data") or {}).get("containers") or []
    for row in rows:
        origin = row.get("origin") or {}
        kind = origin.get("kind", "container")
        detail = origin.get("probe_id") or origin.get("eval_id") or origin.get("suffix") or ""
        step = origin.get("step")
        tag = kind + (f":{detail}" if detail else "")
        if step is not None:
            tag += f"@{step}"
        print(
            f"{row.get('state', '?'):8} {row.get('name')}  "
            f"[{origin.get('container_id')}] {tag}  {row.get('running_for', '')}"
        )
    print(f"count={len(rows)}")
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


def cmd_bundles(args: argparse.Namespace) -> int:
    env = _http("GET", f"{_base_url(args)}/v1/projects/{args.project}/bundles", None)
    if args.json:
        return _print_json(env)
    d = env.get("data") or {}
    for b in d.get("bundles") or []:
        print(
            f"{b.get('bundle_id')} files={b.get('file_count')} "
            f"entrypoints={','.join(b.get('entrypoints') or [])}"
        )
    print(f"total={d.get('total')}")
    for line in _err_lines(env):
        print(line)
    return 0 if env.get("ok") else 1


def cmd_bundle_get(args: argparse.Namespace) -> int:
    env = _http(
        "GET",
        f"{_base_url(args)}/v1/projects/{args.project}/bundles/{args.bundle_id}",
        None,
    )
    if args.json:
        return _print_json(env)
    d = env.get("data") or {}
    manifest = d.get("bundle") or {}
    eps = manifest.get("entrypoints") or {}
    print(f"bundle={args.bundle_id} files={len(manifest.get('files') or [])}")
    for name, ep in sorted(eps.items()):
        argv = ep.get("argv") if isinstance(ep, dict) else ep
        print(f"  {name}: {' '.join(argv) if isinstance(argv, list) else argv}")
    for line in _err_lines(env):
        print(line)
    return 0 if env.get("ok") else 1


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

    s = sub.add_parser("finalize")
    s.add_argument("project")
    s.add_argument("run")
    s.add_argument("--cancel", action="store_true", help="withdraw a pending force-finalize")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_finalize)

    s = sub.add_parser("ps")
    s.add_argument("project")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ps)

    s = sub.add_parser("bundles")
    s.add_argument("project")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_bundles)

    s = sub.add_parser("bundle-get")
    s.add_argument("project")
    s.add_argument("bundle_id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_bundle_get)

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
