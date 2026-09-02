"""Run 単位の purge: kikai 自身が自分の記録と整合させながら run_dir を空ける。

WHY このドメイン操作なのか: ディスクを空ける手段が「外から生の move/delete を叩く」
形だと、kikai が台帳・retention・QC キューで守っているファイルを kikai の知らない
ところで壊せてしまう(そのため生 API 案は却下された)。ここでは逆に、削除の可否を
kikai 自身の記録から判定し、消した事実を台帳と journal に残す。

契約:

- 既定は ``dry_run=true``。消える対象とバイト数を先に見せ、``dry_run=false`` を
  明示したときだけ実際に消す。
- ``keep`` (既定 ``metrics.jsonl`` / ``tensorboard``) は run_dir 直下の名前。証跡
  ——学習曲線と TensorBoard ログ——は既定で残る。巨大なのは checkpoint と QC 動画で、
  証跡ではない。
- daemon がまだ所有している run (非終端状態 / QC・probe が未実行) は拒否する。
  retention が QC 待ち checkpoint を守るのと同じ判定 (``pending_qc_steps``) を
  共用するので、「retention は守るのに purge は消す」という食い違いは起きない。
- 削除対象は run_dir 直下のエントリを個別に実体解決して封じ込めを確認する。外を
  指す symlink は削除せずスキップし、レスポンスで報告する (消したつもりで run_dir
  の外を消すのが最悪の事故なので、黙って追随しない)。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from kikai_lab.envelope import error
from kikai_lab.operation import OperationError, resolve_metrics_path
from kikai_lab.reconcile import load_progress, pending_qc_steps, read_terminal_event
from kikai_lab.server.app import envelope_response
from kikai_lab.server.registry import (
    WRITE_LOCK,
    ServerConfig,
    append_journal,
    require_project,
    require_safe_id,
    utc_now_text,
)
from kikai_lab.server.run_files import require_run_dir, resolve_in_run_dir
from kikai_lab.server.runs import (
    derive_status,
    inspect_training_container,
    load_managed_run_optional,
    require_run_record,
)

# 証跡は既定で残す: metrics.jsonl は run の一次記録、tensorboard は人間が読む唯一の
# 履歴。どちらも容量問題の原因ではない (太るのは checkpoints/ と qc/)。
DEFAULT_KEEP: tuple[str, ...] = ("metrics.jsonl", "tensorboard")

# daemon がまだ書き込む/後処理する状態。ここで purge すると trainer や reconciler と
# 競走する (finalize 前の run は retention・QC・成果物記録がまだ走る)。
ACTIVE_STATUSES = ("submitting", "submitted", "running", "exited_pending_finalize")

# 台帳は 1 行 1 イベントの追記専用。60 checkpoint 分のパスを 1 行に流し込むと台帳が
# 読めなくなるので、行に載せるパスは上限を切り、件数と総バイト数は必ず正確に残す。
MAX_LEDGER_PATHS = 200


def normalized_keep(raw: Any) -> list[str]:
    """``keep`` を run_dir 直下の名前として検証する。

    パス区切りや ``..`` を許すと keep がミニ経路言語になり、「消さない範囲」の判定が
    削除側の封じ込めと別実装になる。直下の名前だけに限れば、判定は集合の一致で済む。"""
    if raw is None:
        return list(DEFAULT_KEEP)
    if not isinstance(raw, list) or any(not isinstance(name, str) for name in raw):
        raise OperationError(
            "run.purge_keep_invalid",
            "keep must be a list of names directly under the run_dir",
            {"keep": raw},
        )
    keep: list[str] = []
    for name in raw:
        text = name.strip()
        if not text or text in (".", "..") or Path(text).name != text:
            raise OperationError(
                "run.purge_keep_invalid",
                "each keep entry must be a single name directly under the run_dir",
                {"name": name},
            )
        if text not in keep:
            keep.append(text)
    return keep


def entry_usage(path: Path) -> tuple[int, int]:
    """(bytes, file_count) — symlink を一切たどらずに数える。

    たどると「run_dir の外にある 400GB」を purge の見積もりに計上してしまい、実際に
    消える量と表示が食い違う。symlink 自体は 1 ファイルとして数え、その先は数えない。"""
    try:
        stat = path.lstat()
    except OSError:
        return (0, 0)
    if path.is_symlink() or not path.is_dir():
        return (stat.st_size, 1)
    total = 0
    files = 0
    # followlinks=False: symlink されたディレクトリには降りない (os.walk の既定)
    for root, _dirs, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
            files += 1
    return (total, files)


def purge_plan(run_dir: Path, keep: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """run_dir 直下を (削除対象, スキップ) に振り分ける。

    エントリごとに ``resolve_in_run_dir`` で実体解決するので、外を指す symlink は
    削除対象に入らない。ディレクトリ内部の symlink は ``shutil.rmtree`` が link の
    まま unlink する (追随しない) ため、ここでの直下チェックで封じ込めは足りる。"""
    targets: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        children = sorted(run_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise OperationError(
            "run.run_dir_missing",
            "run_dir became unreadable while planning the purge",
            {"run_dir": str(run_dir)},
        ) from exc
    for child in children:
        if child.name in keep:
            continue
        try:
            resolve_in_run_dir(run_dir, child.name)
        except OperationError as exc:
            skipped.append({"path": child.name, "reason": exc.code})
            continue
        size, files = entry_usage(child)
        targets.append(
            {
                "path": child.name,
                "bytes": size,
                "is_dir": child.is_dir() and not child.is_symlink(),
                "file_count": files,
            }
        )
    return targets, skipped


def require_purgeable(project_root: Path, run_name: str, run_dir: Path) -> str:
    """purge してよい run か kikai の記録から判定し、derived status を返す。

    status は保存値ではなく毎回導出する (runs.py の契約と同じ): 宣言レコードだけ見て
    「completed だから消してよい」と判断すると、再投入されて走っている run を消す。
    QC 待ちの判定は retention が checkpoint を守るのに使う ``pending_qc_steps`` を
    そのまま共用する——二重実装すると「retention は守るのに purge は消す」が起きる。"""
    record = require_run_record(project_root, run_name)
    managed = load_managed_run_optional(project_root, run_name)
    progress = load_progress(project_root, run_name)
    container = inspect_training_container(
        project_root, managed, (record.get("submission") or {}).get("container_id")
    )
    status = derive_status(
        declared=record.get("status"),
        container=container,
        progress=progress,
        terminal_event=read_terminal_event(resolve_metrics_path(run_dir)),
    )
    if status in ACTIVE_STATUSES:
        raise OperationError(
            "run.purge_active_refused",
            f"run is still owned by the daemon (derived_status={status}); "
            "stop and finalize it first",
            {"run_name": run_name, "derived_status": status},
        )
    # managed でない run には QC キューが存在しない (レコードの status しか根拠が
    # なく、それは上の derive_status が既に見ている)。
    pending = pending_qc_steps(managed, run_dir, progress) if managed else []
    if pending:
        raise OperationError(
            "run.purge_qc_pending_refused",
            "checkpoints still have queued qc_op/probe work; "
            "finalize the run or let the QC queue drain first",
            {"run_name": run_name, "pending_qc_steps": pending[:50]},
        )
    return status


def delete_targets(
    run_dir: Path, targets: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """計画を実行する。1 件の失敗で全体を落とさず、消えた分と落ちた分を分けて返す。

    削除直前にもう一度実体解決する: 計画から実行までの間に外向き symlink に差し替え
    られる余地を残さない(planning と deletion の間の TOCTOU)。"""
    deleted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for target in targets:
        name = str(target["path"])
        try:
            resolve_in_run_dir(run_dir, name)
        except OperationError as exc:
            failed.append({"path": name, "reason": exc.code})
            continue
        path = run_dir / name
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                # rmtree は symlink されたディレクトリに降りず link を外すだけ
                shutil.rmtree(path)
        except OSError as exc:
            failed.append({"path": name, "reason": type(exc).__name__})
            continue
        deleted.append(target)
    return deleted, failed


def record_purge(
    project_root: Path,
    run_name: str,
    deleted: list[dict[str, Any]],
    total_bytes: int,
) -> str | None:
    """台帳に purge イベント行を追記し、その artifact_id を返す。

    ``artifacts/<run>.jsonl`` は追記専用なので、消えた成果物の行は書き換えない——
    「あの qc 動画の行が台帳にあるのに実体がない」を後から説明できるよう、削除は
    削除として 1 行足す。既存行を消して回ると、台帳が「何が存在したか」の記録では
    なくなる。

    何も消えなかった purge は記録しない (None を返す)。空振りを行にすると、purge を
    再確認のたびに叩く運用で台帳が中身のない行で埋まる。"""
    if not deleted:
        return None
    stamp = utc_now_text().replace("-", "").replace(":", "")
    artifact_id = f"{run_name[:40]}_purge_{stamp}"
    row = {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "run_name": run_name,
        "kind": "purge",
        "artifact_class": "run_dir_purge",
        "at": utc_now_text(),
        "purged_paths": [str(e["path"]) for e in deleted[:MAX_LEDGER_PATHS]],
        "purged_paths_truncated": len(deleted) > MAX_LEDGER_PATHS,
        "purged_count": len(deleted),
        "total_bytes": total_bytes,
        "locations": [],
    }
    ledger = project_root / "artifacts" / f"{run_name}.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return artifact_id


def build_run_purge_router(config: ServerConfig) -> APIRouter:
    router = APIRouter(tags=["run-files"])

    @router.post("/projects/{project_id}/runs/{run_name}/artifacts/purge")
    def run_artifacts_purge(
        project_id: str,
        run_name: str,
        body: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> JSONResponse:
        if body is not None and not isinstance(body, dict):
            raise OperationError(
                "run.purge_body_invalid", "body must be a JSON object", {}
            )
        body = body or {}
        # 既定は dry run: 「消す API を叩いたら消えた」ではなく「見てから消す」。
        dry_run = body.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise OperationError(
                "run.purge_body_invalid", "dry_run must be a boolean", {"dry_run": dry_run}
            )
        keep = normalized_keep(body.get("keep"))
        project_root = require_project(config, project_id)
        run_name = require_safe_id(run_name, kind="run")
        run_dir = require_run_dir(config, project_root, run_name)
        derived_status = require_purgeable(project_root, run_name, run_dir)

        data: dict[str, Any] = {
            "run_name": run_name,
            "dry_run": dry_run,
            "keep": keep,
            "derived_status": derived_status,
        }
        if dry_run:
            targets, skipped = purge_plan(run_dir, keep)
            data.update(
                {
                    "would_delete": targets,
                    "total_bytes": sum(int(t["bytes"]) for t in targets),
                    "skipped": skipped,
                }
            )
            return envelope_response(ok=True, data=data)

        # 計画と削除を同じロックの中で: 並行 purge が同じエントリを二重に数え、
        # 片方が「消した」と台帳に書いて実際には相手が消した、を防ぐ。
        with WRITE_LOCK:
            # ロック取得までに daemon が走り出していないか、もう一度判定する
            derived_status = require_purgeable(project_root, run_name, run_dir)
            targets, skipped = purge_plan(run_dir, keep)
            deleted, failed = delete_targets(run_dir, targets)
            total_bytes = sum(int(e["bytes"]) for e in deleted)
            artifact_id = record_purge(project_root, run_name, deleted, total_bytes)
        if deleted:
            append_journal(
                project_root,
                "run_artifacts_purged",
                {
                    "run_name": run_name,
                    "artifact_id": artifact_id,
                    "purged_count": len(deleted),
                    "total_bytes": total_bytes,
                    "keep": keep,
                },
            )
        data.update(
            {
                "derived_status": derived_status,
                "deleted": deleted,
                "total_bytes": total_bytes,
                "skipped": skipped,
                "failed": failed,
                "artifact_id": artifact_id,
            }
        )
        warnings = None
        if failed:
            # ok=True のまま黙って一部残す方が危険: 空けたつもりの容量が空いていない
            warnings = [
                error(
                    "run.purge_partial",
                    f"{len(failed)} entries could not be deleted",
                    blocking=False,
                    details={"failed": failed[:20]},
                )
            ]
        return envelope_response(ok=True, data=data, warnings=warnings)

    return router
