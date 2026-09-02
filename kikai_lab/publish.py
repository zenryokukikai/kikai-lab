"""``kikai publish`` — 登録済みの op 列を kikai 非依存の自己完結パッケージへ書き出す。

翻訳は純関数に閉じてある: ``env:NAME`` / ``${NAME}`` は **解決せず**シェル変数として持ち回る
ので、``~/.kikai/secrets.json`` の値がパッケージへ漏れることがない (設計 §5-2)。
パッケージの字面と書き出しは ``kikai_lab.publish_package`` の側にある。

設計: docs/design/publish_design.md
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kikai_lab.operation import (
    ENV_PLACEHOLDER_RE,
    SAFE_CONTAINER_NAME,
    OperationError,
    docker_attribution_labels,
    kikai_identity_env,
    load_container_record,
    load_script_bundle,
    require_string,
    script_bundle_entrypoint_argv,
    validate_script_bundle_files,
)
from kikai_lab.publish_package import (
    CONTAINER_BUNDLE_DIR,
    CONTAINER_PROJECT_DIR,
    PACKAGE_BUNDLE_DIR,
    MountRow,
    PublishPlan,
    Seg,
    TranslatedOp,
    VarSpec,
    Word,
    join_words,
    literal,
    word_vars,
    write_package,
)

# docker CLI へ 1:1 で落ちる adapter。ここに無いものは除外して README に書く (設計 §4.2)。
TRANSLATABLE_ADAPTERS = frozenset({"script_bundle_run"})

# ref の NAME は run.sh で ${NAME} になる。シェル識別子でない名前 (例: DATA-DIR) は
# bash のデフォルト展開として黙って誤動作するので、翻訳せず publish 自体を拒否する。
_SHELL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# --------------------------------------------------------------------------- refs


def parse_ref(value: str) -> Word:
    """``env:NAME`` / ``${NAME}`` を **解決せず** シェル語へ落とす。

    kikai の ``resolve_text_ref`` は ``env:`` を剥がしてから ``${}`` を展開するが、publish は
    ``env:NAME`` の中身を知らないので、そこで展開は打ち切る (README に明記する)。
    """
    if value.startswith("env:"):
        name = value.removeprefix("env:")
        if not _SHELL_IDENTIFIER.fullmatch(name):
            raise OperationError(
                "publish.env_ref_invalid",
                "env: reference name must be a shell identifier "
                "([A-Za-z_][A-Za-z0-9_]*)",
                {"value": value},
            )
        return (Seg(name, is_var=True),)
    segments: list[Seg] = []
    position = 0
    for match in ENV_PLACEHOLDER_RE.finditer(value):
        if match.start() > position:
            segments.append(Seg(value[position : match.start()]))
        segments.append(Seg(match.group(1), is_var=True))
        position = match.end()
    if position < len(value):
        segments.append(Seg(value[position:]))
    return tuple(segments) if segments else (Seg(""),)




# --------------------------------------------------------------- variable naming


def _slug(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", value)).strip("_").upper()


class VarTable:
    """変数名の台帳。同じ literal 値には同じ変数を割り当て、衝突は連番で分ける。"""

    def __init__(self) -> None:
        self.specs: list[VarSpec] = []
        self._by_name: dict[str, VarSpec] = {}

    def declare_ref(self, name: str, origin: str) -> None:
        """既に ``env:`` で名前が付いているものは、その名前をそのまま使う。"""
        existing = self._by_name.get(name)
        if existing is None:
            spec = VarSpec(name=name, origin=origin)
            self.specs.append(spec)
            self._by_name[name] = spec
        elif origin not in existing.origin:
            existing.origin = f"{existing.origin}; {origin}"

    def declare_literal(self, preferred: str, value: str, origin: str) -> str:
        """literal のホスト固有値へ変数を割り当てて、その変数名を返す。"""
        base = preferred or "VALUE"
        candidate = base
        suffix = 2
        while True:
            existing = self._by_name.get(candidate)
            if existing is None:
                spec = VarSpec(name=candidate, origin=origin, example=value)
                self.specs.append(spec)
                self._by_name[candidate] = spec
                return candidate
            if existing.example == value:
                if origin not in existing.origin:
                    existing.origin = f"{existing.origin}; {origin}"
                return candidate
            candidate = f"{base}_{suffix}"
            suffix += 1


# ------------------------------------------------------------------- translation


def _container_value(container: dict[str, Any], key: str) -> str | None:
    value = container.get(key)
    return value if isinstance(value, str) and value else None


def _docker_meta(container: dict[str, Any]) -> dict[str, Any]:
    docker = container.get("docker")
    return docker if isinstance(docker, dict) else {}


def _composed_name_word(
    container: dict[str, Any], container_id: str, request: dict[str, Any]
) -> Word | None:
    """``--name`` に渡す語。``_composed_docker_name`` と同じ合成規則を、ref を解決せずに行う。"""
    docker = _docker_meta(container)
    declared = docker.get("name")
    if not (isinstance(declared, str) and declared):
        return None
    ephemeral = bool(docker.get("ephemeral"))
    suffix = request.get("container_name_suffix")
    if isinstance(suffix, str) and suffix and not ephemeral:
        raise OperationError(
            "operation.container_name_suffix_not_ephemeral",
            "container_name_suffix requires container.docker.ephemeral=true",
            {"container_id": container_id, "suffix": suffix},
        )
    word = parse_ref(declared)
    if not (ephemeral and isinstance(suffix, str) and suffix):
        return word
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]", "_", suffix)[:50]
    if word_vars(word):
        # 名前が ref のときは 63 字の切り詰めを publish 時に決められない。切らずに繋ぎ、
        # README で断る (設計 §4.1)。
        return join_words(word, literal(safe_suffix), separator="__")
    base = word[0].text[: max(1, 63 - len(safe_suffix) - 2)]
    return literal(f"{base}__{safe_suffix}")


def _mount_words(
    *,
    container: dict[str, Any],
    container_id: str,
    variables: VarTable,
    mounts: list[MountRow],
) -> list[Word]:
    """``-v`` 引数の語列。source だけを変数化し、target と mode は literal のまま残す。"""
    raw_mounts = container.get("mounts") or []
    if not isinstance(raw_mounts, list):
        raise OperationError(
            "operation.container_mounts_invalid",
            "container mounts must be a list",
            {"container_id": container_id},
        )
    words: list[Word] = []
    for index, mount in enumerate(raw_mounts):
        if not isinstance(mount, dict):
            raise OperationError(
                "operation.container_mount_invalid",
                "container mount entries must be objects",
                {"container_id": container_id, "index": index},
            )
        if mount.get("source_kind") == "kikai_managed_source_snapshot":
            raise _Untranslatable(
                "mount uses a kikai-managed source snapshot; publish does not export "
                "source snapshots yet"
            )
        source_raw = require_string(
            mount.get("source"),
            "operation.container_mount_invalid",
            "container mount source is required",
        )
        target_raw = require_string(
            mount.get("target"),
            "operation.container_mount_invalid",
            "container mount target is required",
        )
        mode = mount.get("mode")
        if mode is not None and mode != "":
            mode_text = require_string(
                mode,
                "operation.container_mount_invalid",
                "container mount mode must be a string",
            )
        else:
            mode_text = ""
        target_word = parse_ref(target_raw)
        for name in word_vars(target_word):
            variables.declare_ref(name, f"mount target of {container_id}")
        source_word = parse_ref(source_raw)
        source_example: str | None = None
        if word_vars(source_word):
            for name in word_vars(source_word):
                variables.declare_ref(name, f"mount source for {target_raw}")
            variable_label = ",".join(word_vars(source_word))
        else:
            source_example = source_word[0].text
            variable_label = variables.declare_literal(
                f"SRC_{_slug(target_raw)}",
                source_example,
                f"mount source for {target_raw}",
            )
            source_word = (Seg(variable_label, is_var=True),)
        pieces = [source_word, target_word]
        if mode_text:
            pieces.append(literal(mode_text))
        words.append(join_words(*pieces, separator=":"))
        mounts.append(
            MountRow(
                variable=variable_label,
                target=target_raw,
                mode=mode_text or "rw (docker default)",
                source_example=source_example,
            )
        )
    return words


class _Untranslatable(Exception):
    """この op は docker CLI へ落とせない、という内部シグナル。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _image_word(
    container: dict[str, Any], container_id: str, variables: VarTable, plan_images: list[str]
) -> Word:
    image_raw = _docker_meta(container).get("image")
    if not (isinstance(image_raw, str) and image_raw):
        raise OperationError(
            "operation.container_docker_image_missing",
            "docker run container definition must define docker.image",
            {"container_id": container_id},
        )
    word = parse_ref(image_raw)
    if word_vars(word):
        for name in word_vars(word):
            variables.declare_ref(name, f"image for {container_id}")
        names = word_vars(word)
    else:
        name = variables.declare_literal(
            f"IMAGE_{_slug(container_id)}", word[0].text, f"image for {container_id}"
        )
        word = (Seg(name, is_var=True),)
        names = [name]
    for name in names:
        if name not in plan_images:
            plan_images.append(name)
    return word


def _env_words(request: dict[str, Any], container_id: str, variables: VarTable) -> list[Word]:
    """``-e K=V`` の語列。``request.env`` と kikai が自動注入する run 識別子の両方。"""
    env = request.get("env") or {}
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise OperationError(
            "operation.env_invalid",
            "docker request.env must be an object of string values",
            {"container_id": container_id},
        )
    words: list[Word] = []
    for key, value in env.items():
        value_word = parse_ref(value)
        for name in word_vars(value_word):
            variables.declare_ref(name, f"value of -e {key}")
        words.append(join_words(literal(f"{key}="), value_word, separator=""))
    # 自動注入する run 識別子は本体の規則ごと呼ぶ (ref を解決しない純関数なので写しは不要)。
    identity_args = kikai_identity_env(request, container_id)
    words.extend(literal(pair) for pair in identity_args[1::2])
    return words


def translate_operation(
    *,
    index: int,
    operation_doc: dict[str, Any],
    project_root: Path,
    variables: VarTable,
    plan: PublishPlan,
) -> TranslatedOp:
    """1 つの op を ``docker run`` の語列にする。落とせないものは skipped で返す。"""
    request = operation_doc.get("request")
    if not isinstance(request, dict):
        raise OperationError(
            "operation.request_missing",
            "operation must contain a request object",
            {"index": index},
        )
    adapter = request.get("adapter") if isinstance(request.get("adapter"), str) else "(none)"
    op_name = request.get("operation") if isinstance(request.get("operation"), str) else "(unnamed)"
    translated = TranslatedOp(index=index, operation=op_name, adapter=adapter)
    if adapter not in TRANSLATABLE_ADAPTERS:
        translated.skipped_reason = (
            f"adapter '{adapter}' has no 1:1 docker CLI form "
            f"(publish translates: {', '.join(sorted(TRANSLATABLE_ADAPTERS))})"
        )
        return translated
    if "argv" in request:
        raise OperationError(
            "operation.script_bundle_raw_argv_forbidden",
            "script_bundle_run operations must use bundle entrypoints, not raw request argv",
            {"index": index, "operation": op_name},
        )
    container_id = require_string(
        request.get("container_id"),
        "operation.container_id_missing",
        "script_bundle_run request.container_id is required",
    )
    bundle_id = require_string(
        request.get("bundle_id"),
        "operation.script_bundle_missing",
        "script_bundle_run request.bundle_id is required",
    )
    entrypoint_name = require_string(
        request.get("entrypoint"),
        "operation.script_bundle_entrypoint_missing",
        "script_bundle_run request.entrypoint is required",
    )
    args = request.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise OperationError(
            "operation.script_bundle_args_invalid",
            "script_bundle_run request.args must be a list of strings",
            {"bundle_id": bundle_id},
        )
    detach = request.get("detach", False)
    if not isinstance(detach, bool):
        raise OperationError(
            "operation.script_bundle_run_detach_invalid",
            "script_bundle_run request.detach must be a boolean",
            {"bundle_id": bundle_id},
        )
    container = load_container_record(project_root, container_id)
    bundle, bundle_root = load_script_bundle(project_root, bundle_id)
    validate_script_bundle_files(bundle, bundle_root, bundle_id)
    argv = [*script_bundle_entrypoint_argv(bundle, bundle_id, entrypoint_name), *args]

    name_word = _composed_name_word(container, container_id, request)
    try:
        mount_words = _mount_words(
            container=container,
            container_id=container_id,
            variables=variables,
            mounts=plan.mounts,
        )
    except _Untranslatable as exc:
        translated.skipped_reason = exc.reason
        return translated

    command: list[Word] = [literal("docker"), literal("run")]
    if detach:
        if name_word is None:
            raise OperationError(
                "operation.script_bundle_run_detach_requires_name",
                "detached script_bundle_run requires the container to define docker.name",
                {"container_id": container_id},
            )
        if not word_vars(name_word) and not SAFE_CONTAINER_NAME.match(name_word[0].text):
            # 本体は unsafe な名前の detach を submit 時に拒否する。publish も対応を保つ。
            raise OperationError(
                "operation.script_bundle_run_detach_requires_name",
                "detached script_bundle_run docker.name is not a safe container name",
                {"container_id": container_id, "container_name": name_word[0].text},
            )
        command += [literal("-d"), literal("--name"), name_word]
    else:
        command.append(literal("--rm"))
        if name_word is not None and (
            word_vars(name_word) or SAFE_CONTAINER_NAME.match(name_word[0].text)
        ):
            command += [literal("--name"), name_word]
    for key, flag in (("gpus", "--gpus"), ("network_mode", "--network"), ("ipc_mode", "--ipc")):
        value = _container_value(container, key)
        if value:
            word = parse_ref(value)
            for var in word_vars(word):
                variables.declare_ref(var, f"{flag} for {container_id}")
            command += [literal(flag), word]
    shm_size = _container_value(container, "shm_size")
    if shm_size:
        word = parse_ref(shm_size)
        for var in word_vars(word):
            variables.declare_ref(var, f"--shm-size for {container_id}")
        command += [literal("--shm-size"), word]
    workdir_raw = request.get("workdir") or container.get("workdir") or CONTAINER_PROJECT_DIR
    workdir_word = parse_ref(
        require_string(workdir_raw, "operation.workdir_invalid", "workdir must be a string")
    )
    for var in word_vars(workdir_word):
        variables.declare_ref(var, f"--workdir for {container_id}")
    command += [literal("--workdir"), workdir_word]
    for word in _env_words(request, container_id, variables):
        command += [literal("-e"), word]
    # kikai では project_root 全体を ro で被せるが、published 版はパッケージ内の bundle だけを
    # 見せる (レジストリの他の記録を配らない)。entrypoint argv は workdir 相対なので、
    # script_bundles/ の位置に被せれば argv はそのままで通る。
    command += [
        literal("-v"),
        join_words(
            (Seg("PUBLISH_ROOT", is_var=True), Seg(f"/{PACKAGE_BUNDLE_DIR}")),
            literal(CONTAINER_BUNDLE_DIR),
            literal("ro"),
            separator=":",
        ),
    ]
    for word in mount_words:
        command += [literal("-v"), word]
    command.append(_image_word(container, container_id, variables, plan.images))
    command += [parse_ref(item) for item in argv]
    for item in argv:
        for var in word_vars(parse_ref(item)):
            variables.declare_ref(var, "argv value")

    translated.command = command
    translated.bundle_id = bundle_id
    translated.entrypoint = entrypoint_name
    plan.bundle_roots[bundle_id] = bundle_root
    _record_dropped(plan, request, container_id, name_word)
    return translated


def _record_dropped(
    plan: PublishPlan, request: dict[str, Any], container_id: str, name_word: Word | None
) -> None:
    """kikai 側だけの取り決めで、published 版から落としたもの。README に必ず出す。"""
    notes = plan.dropped_notes
    if request.get("docker_host") is not None:
        note = (
            f"{container_id}: request.docker_host は落とした "
            "(どの docker daemon に話すかは kikai 側の都合。パッケージは実行するホストの "
            "docker をそのまま使う)"
        )
        if note not in notes:
            notes.append(note)
    if request.get("timeout_sec") is not None:
        note = (
            f"{container_id}: request.timeout_sec={request['timeout_sec']} は落とした "
            "(kikai の reconcile daemon を守るための監視で、docker CLI に等価物が無い)"
        )
        if note not in notes:
            notes.append(note)
    labels = docker_attribution_labels({}, container_id)
    note = f"{container_id}: kikai の帳簿ラベル ({' '.join(labels[1::2])}) は落とした"
    if note not in notes:
        notes.append(note)
    if name_word is not None and word_vars(name_word) and request.get("container_name_suffix"):
        # literal 名は publish 時に本体と同じ切り詰めを済ませてあるので、断りは要らない。
        note = (
            f"{container_id}: --name は 63 字への切り詰めをしていない "
            "(名前が変数なので publish 時に長さが決まらない)"
        )
        if note not in notes:
            notes.append(note)


def build_plan(
    *, name: str, project_root: Path, operations: list[dict[str, Any]]
) -> PublishPlan:
    """op 列を **並べ替えずに** 翻訳する。並び順がそのまま依存順 (設計 §5-4)。"""
    plan = PublishPlan(name=name)
    variables = VarTable()
    for index, operation_doc in enumerate(operations, start=1):
        plan.ops.append(
            translate_operation(
                index=index,
                operation_doc=operation_doc,
                project_root=project_root,
                variables=variables,
                plan=plan,
            )
        )
    plan.variables = variables.specs
    return plan



def publish_operations(
    *, name: str, project_root: Path, operation_paths: list[Path], out_dir: Path
) -> tuple[dict[str, Any], PublishPlan]:
    from kikai_lab.operation import load_operation

    if not operation_paths:
        raise OperationError(
            "publish.ops_missing",
            "publish requires at least one --ops operation file",
            {},
        )
    operations = [load_operation(path) for path in operation_paths]
    plan = build_plan(name=name, project_root=project_root, operations=operations)
    result = write_package(plan, out_dir)
    result["operation_files"] = [str(path) for path in operation_paths]
    return result, plan
