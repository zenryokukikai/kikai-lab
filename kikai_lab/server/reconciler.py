"""Embedded reconciler: one background thread looping reconcile_once over every
active project in the projects root.

Opt-in via ``kikai server start --with-reconciler``. One reconciler per registry is a
hard rule — do not run this alongside an external ``kikai serve`` on the same project
(the progress.json idempotency makes double-QC unlikely but retention/finalize races
are not worth it). The thread reports its liveness through ``/healthz``.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from kikai_lab.reconcile import reconcile_once
from kikai_lab.server.registry import ServerConfig, is_project_dir, project_status

logger = logging.getLogger("kikai_lab.server.reconciler")


def active_project_paths(config: ServerConfig):
    root = config.projects_root
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not is_project_dir(child):
            continue
        try:
            from kikai_lab.server.registry import load_project_yaml

            if project_status(load_project_yaml(child)) == "archived":
                continue
        except Exception:  # unreadable project.yaml -> skip, never kill the loop
            continue
        yield child


def reconcile_all(config: ServerConfig, *, once_fn: Callable = reconcile_once) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for project_path in active_project_paths(config):
        try:
            results[project_path.name] = once_fn(project_path)
        except Exception as exc:  # one project never kills the pass
            logger.exception("reconcile pass failed for %s", project_path.name)
            results[project_path.name] = {"error": type(exc).__name__}
    return results


class BackgroundReconciler:
    def __init__(self, config: ServerConfig, *, once_fn: Callable = reconcile_once):
        self._config = config
        self._once_fn = once_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick_at: float | None = None
        self.last_results: dict[str, Any] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="kikai-reconciler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def tick(self) -> None:
        self.last_results = reconcile_all(self._config, once_fn=self._once_fn)
        self.last_tick_at = time.time()

    @property
    def last_errors(self) -> dict[str, str]:
        return {
            name: result["error"]
            for name, result in self.last_results.items()
            if isinstance(result, dict) and "error" in result
        }

    def _loop(self) -> None:
        interval = max(1, int(self._config.reconcile_interval))
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # defensive: the loop itself must never die
                logger.exception("reconciler tick crashed")
            self._stop.wait(interval)
