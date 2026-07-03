# Kikai Lab Data Source Registry Implementation Plan

> **For Hermes:** Use TDD. Implement only after this design has been reviewed. Use subagent-driven-development task-by-task if implementation is requested later.

**Goal:** Add an explicit `data_source` concept to Kikai Lab so training/evaluation/QC runs can declare every non-code input by stable id instead of relying on implicit paths, nearby manifests, caches, media files, or checkpoint conventions.

**Architecture:** Introduce a first-class registry object under `data_sources/<data_source_id>.yaml`. Runs and operations reference those records through `data_source_refs`, and validation resolves each referenced data source before launch. Source code remains handled by `source_snapshots` and executable code by `script_bundles`; `data_sources` covers datasets, manifests, caches, media, checkpoints, model artifacts, external datasets, and other non-code inputs.

**Tech Stack:** Python 3.11+, YAML registry records, JSON Schema, existing Kikai validation/envelope patterns, pytest, ruff.

---

## Problem

Kikai Lab currently has durable concepts for:

- source code snapshots: `source_snapshots/<id>/snapshot.json`
- immutable executable bundles: `script_bundles/<id>/bundle.json`
- run records: `runs/<run_name>.yaml`
- container records: `containers/<container_id>.yaml`

But there is no first-class concept for the actual **data inputs** that make a run reproducible.

Current records can mention paths such as:

```yaml
checkpoint:
  latest: ${CONTAINER_TRAINING_RUNS_ROOT}/.../checkpoint_step_040000.pt
metrics:
  metrics_jsonl: ${CONTAINER_TRAINING_RUNS_ROOT}/.../metrics.jsonl
```

and operation/script argv may pass manifests, caches, audio, video, or checkpoints directly. Those are just strings. Kikai cannot answer:

- Which manifest/cache/audio/checkpoint did this run use?
- Was the path intended as training input, preview input, resume checkpoint, or output artifact?
- Is the input immutable, append-only, generated, or mutable live state?
- Which upstream data or source snapshot produced it?
- Is it safe to mount or pass this path to a container?
- Did an operation silently fall back to a nearby or environment-derived data path?

This gap is different from `source_snapshot`: source snapshots solve code immutability. They do not model datasets, media, cache directories, checkpoints, or generated artifacts used as model/data inputs.

## Decision

Add `data_source` as a first-class Kikai registry object.

A data source is a named non-code input or reusable non-code input collection with explicit identity, role contract, storage location, mutability/integrity policy, provenance, and optional domain metadata.

Data sources must be referenced by id from runs and operations. An operation that needs data must not rely on an unregistered manifest/cache/media/checkpoint path.

Important distinctions:

| Concept | Purpose | Example |
| --- | --- | --- |
| `source_snapshot` | Immutable source code mount | Python package / launcher code |
| `script_bundle` | Immutable executable script/config bundle | watcher entrypoint + config |
| `data_source` | Non-code input lineage and access contract | train manifest, face cache, source video/audio, TTS corpus, resume checkpoint |
| `artifact` | Produced output that may later become a data source | checkpoint, preview video, diagnostic summary |

A generated artifact may be promoted to a data source only through an explicit record. Promotion must record provenance instead of treating output paths as implicit inputs.

## Non-goals

- Do not replace `source_snapshot` for code.
- Do not make every output artifact a data source automatically.
- Do not implement object storage or content-addressed large-file storage in this step.
- Do not infer missing data sources from argv/path names.
- Do not support silent fallback from a missing data source to environment variables or nearby conventional paths.

## Registry layout

Use one YAML file per data source:

```text
examples/example_project/
  data_sources/
    example_pose_manifest_v1.yaml
    example_face_cache_v1.yaml
    example_source_audio_v1.yaml
```

The single-file layout matches `runs/<id>.yaml`, `containers/<id>.yaml`, and `experiments/<id>.yaml`, while avoiding large copied data in the public framework repo.

Large data remains at external paths or Kikai-managed runtime/workspace locations. The registry record stores identity, contract, provenance, access paths, and integrity metadata when available.

## Data source record schema

Create `schemas/data_source.schema.json` with this required minimum shape:

```yaml
schema_version: 1
kind: kikai_data_source
data_source_id: example_pose_manifest_v1
status: active
summary: Pose training manifest for the example project fixture.
source_type: dataset_manifest
immutability:
  mode: immutable
  verified_at: "2026-06-28T00:00:00Z"
storage:
  storage_kind: host_path
  host_ref: training_host_primary
  path: env:EXAMPLE_POSE_MANIFEST_PATH
  container_mount_path: env:CONTAINER_EXAMPLE_POSE_MANIFEST_PATH
integrity:
  strategy: file_sha256
  sha256: "<Kikai-calculated 64 lowercase hex chars>"
  calculated_by: kikai_lab.data-source.create-file
  calculated_at: "2026-06-28T00:00:00Z"
  verification: preflight_required
contract:
  role_compatibility:
    - train_manifest
    - eval_manifest
  media_type: application/x-yaml
  sample_unit: frame_window
  required_fields:
    - frame_path
    - audio_path
    - face_cache_key
provenance:
  created_by: kikai data-source create-file
  upstream_data_source_ids: []
  upstream_source_snapshot_ids:
    - example_project_fixture
notes: []
```

### Required fields

- `schema_version`: integer, initially `1`.
- `kind`: must be `kikai_data_source`.
- `data_source_id`: single registry id, must match filename stem.
- `source_type`: one of the supported source types.
- `status`: `active`, `deprecated`, or `blocked`.
- `storage`: how Kikai or a container can locate the data.
- `immutability`: whether the data is immutable, append-only, or mutable live state.

### Status semantics

- `active`: may be used by runs and operations if all validation checks pass.
- `deprecated`: existing historical run records may reference it, but new launch-like operations should warn and should require an explicit adapter allowance before use.
- `blocked`: must fail for both run validation and operation preflight. Use this for known-bad or forbidden lineage.

### Supported `source_type` values for v1

Start with the types needed by Kikai/lipsync workflows, but keep them generic:

- `dataset_manifest`
- `dataset_directory`
- `cache_directory`
- `media_file`
- `media_directory`
- `checkpoint_file`
- `model_artifact`
- `metrics_log`
- `external_dataset`
- `opaque_input`

`opaque_input` is allowed only when the operation explicitly does not need domain semantics. It still needs storage and mutability metadata.

### Storage shape

Supported `storage.storage_kind` values:

- `host_path`: path on a named host.
- `container_path`: path already meaningful inside the execution container.
- `object_uri`: URI such as `s3://...`, `gs://...`, `hf://...`, or HTTPS.
- `kikai_runtime_path`: Kikai-managed workspace/state path outside the public framework repo.
- `artifact_ref`: points at a registered Kikai artifact that has been promoted as input.

Rules:

- `host_path` must include `host_ref` and `path`. Relative `host_path.path` values are resolved only against `project_root`; they are never resolved against the caller's current working directory. If the same data should be passed into a container, use optional `container_mount_path` for the path visible inside that container. Do not call this field `container_path`; `container_path` is reserved for the storage kind below.
- `container_path` storage must include `path` and should include `container_id` or `container_role` when scoped. This means the data is already addressed by an in-container path, not that a host path has a mount target.
- `object_uri` must include `uri`.
- `artifact_ref` must include `artifact_id`.
- Environment refs like `env:NAME` are allowed as path tokens, but validation must preserve them as explicit declarations; they must not be fallback sources. A changed environment value must be caught by preflight integrity verification for launch-like operations, not treated as the same immutable input.

### Immutability shape

Supported modes:

- `immutable`: content is fixed for run reproducibility.
- `append_only`: content may append but existing entries are not rewritten, e.g. metrics logs.
- `mutable_live`: current live state, allowed only for status/monitoring operations unless explicitly accepted by the operation adapter.

Rules:

- Training launch, resume, evaluation, and QC generation data inputs should require `immutable` unless the adapter explicitly documents why `append_only` is safe.
- `mutable_live` data sources are blocking for launch-like operations.
- `immutable` file-like data should include an integrity strategy generated by Kikai Lab, not a caller-supplied hash.
- Directory-like data may use a generated manifest/digest in a later phase; v1 may allow `integrity.strategy: not_available` only when the operation treats the data source as external and read-only, and the record states why hashing is unavailable.
- For launch-like operations, immutable file-like data with `file_sha256` must have been registered through `kikai data-source create-file`, which resolves the declared path and calculates sha256 itself. Preflight must re-verify that Kikai-calculated hash in the same context that will read the data, or by a target-side Kikai verifier that runs before the main command. If Kikai cannot resolve the declared path and recompute the hash, the operation must fail closed with an integrity/preflight error.
- `append_only` data sources are not subject to integrity re-verification in v1. Launch-like operations should normally avoid them as inputs; if an adapter explicitly allows an `append_only` input such as `metrics_log`, preflight validates identity, status, role compatibility, storage shape, and adapter allowance, but does not require re-hashing the growing log.

### Integrity shape

Supported v1 strategies:

- `file_sha256`
- `directory_manifest_sha256`
- `object_etag`
- `artifact_digest`
- `not_available`

`not_available` is not a silent pass. It must include `reason`, and adapters may reject it for reproducibility-critical roles.

`file_sha256` is intentionally not caller-authored. The registration path must be `kikai data-source create-file`, which accepts a path/env ref and calculates the hash inside Kikai Lab. There is no `--sha256` input flag. Validation rejects `file_sha256` records that lack `calculated_by: kikai_lab.data-source.create-file`.

`directory_manifest_sha256` is generated by `kikai data-source create-directory`. The generator sorts paths lexicographically by POSIX relative path, records directories plus regular-file size and sha256 entries, computes a canonical manifest digest internally, and rejects symlinks or special files. Launch-like operation preflight re-computes the directory manifest digest in the same path-resolution context and fails closed on mismatch; caller-supplied directory digests remain invalid.

`object_etag` is only a weak integrity hint for object stores. It is not equivalent to content SHA-256 for all providers, especially S3 multipart uploads. Launch-like operations that require strong reproducibility should prefer `file_sha256`, `artifact_digest`, or a generated directory manifest.

## Role vocabulary

`role` is not a free-form prose label. It must be validated against a canonical Kikai role vocabulary. v1 implements canonical roles only: the schema should use a strict enum and the validator should share the same single role catalog. `role_namespace` / `custom_roles` are explicitly out of scope for v1 and reserved for a future design revision; do not add loose string acceptance as a placeholder. Unknown roles are blocking for launch-like operations and at least warnings for historical read-only validation.

Canonical v1 roles:

- `train_manifest`
- `eval_manifest`
- `face_cache`
- `source_audio`
- `source_video`
- `tts_corpus`
- `initial_checkpoint`
- `resume_checkpoint`
- `teacher_checkpoint`
- `reference_media`
- `preview_audio`
- `metrics_log`
- `status_input`

`contract.role_compatibility` on a data source and `role` on a run/operation ref must use this same vocabulary. If a data source declares `contract.role_compatibility`, the requested role must be present exactly. This is what makes `data_source.role_incompatible` meaningful and catches typos such as `training_manifest` vs `train_manifest`.

## Artifact promotion and lineage loops

`storage_kind: artifact_ref` is allowed only after an explicit promotion decision. Promotion must not silently upgrade mutability: if the source artifact is mutable or lacks a digest, the promoted data source must be `blocked` or must include a newly computed strong digest before it can be used by launch-like operations.

`provenance.upstream_data_source_ids` must be acyclic. Validation should reject direct self-references immediately and should include a graph-cycle check before data sources are used for new operations. v1 may implement the cycle check as part of `validate_data_sources(project_root)` rather than in every adapter.

## Run record integration

Extend run records with `data_source_refs`:

```yaml
run_name: example_run
experiment_id: example_experiment
status: completed
fresh_no_resume: true
source_snapshot_refs:
  - role: training_code
    source_snapshot_id: example_engine_fixture
data_source_refs:
  - role: train_manifest
    data_source_id: example_pose_manifest_v1
    required: true
  - role: face_cache
    data_source_id: example_face_cache_v1
    required: true
  - role: source_audio
    data_source_id: example_source_audio_v1
    required: true
  - role: initial_checkpoint
    data_source_id: null
    required: false
```

Rules:

- `role` is the semantic role inside the run, not the source type.
- `data_source_id` must resolve to `data_sources/<id>.yaml` when `required: true`.
- `required: false` with `data_source_id: null` is the explicit way to say “no resume checkpoint” or “no optional input”.
- Existing direct fields such as `checkpoint.latest` may remain as output/status summaries, but inputs must be declared through `data_source_refs`.
- If a run claims `fresh_no_resume: true`, it must not declare a required `initial_checkpoint` data source. This negative constraint must be covered by tests so a no-resume run cannot accidentally gain a resume input.

## Operation integration

Operations that launch or evaluate work should include `data_source_refs` in the request:

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "example_train",
    "project_root": "examples/example_project",
    "target_id": "example_train",
    "adapter": "script_bundle_run",
    "bundle_id": "example_train_bundle",
    "entrypoint": "train",
    "data_source_refs": [
      {"role": "train_manifest", "data_source_id": "example_pose_manifest_v1"},
      {"role": "face_cache", "data_source_id": "example_face_cache_v1"}
    ]
  }
}
```

Execution preflight must:

1. Resolve each `data_source_id` under `project_root/data_sources/`.
2. Validate `kind`, id/filename match, status, storage shape, and immutability policy.
3. Verify role compatibility against the canonical role vocabulary when `contract.role_compatibility` is present.
4. For launch-like operations, re-verify immutable file-like integrity (`file_sha256`) after resolving env/path tokens in the target execution context, or run an equivalent target-side Kikai verifier before the main command.
5. For `append_only` inputs explicitly allowed by the adapter, skip integrity re-verification and record the mode as `append_only_not_rehashed` in the result/receipt context.
6. Reject missing required data source refs before Docker/container execution.
7. Include resolved data source ids, roles, and integrity verification status in the JSON result envelope.

Adapters must not discover data sources from argv or environment when `data_source_refs` are missing. If an adapter still accepts raw argv paths, it must treat those paths as execution arguments only after the operation has declared the corresponding data source ids.

## Validation behavior

Add validation in `kikai_lab/validation.py` and/or a focused module.

`kikai validate --project-root ... --json` should check:

- every `data_sources/*.yaml` file is valid;
- `kind == kikai_data_source`;
- filename stem matches `data_source_id`;
- `source_type`, `status`, `storage.storage_kind`, and `immutability.mode` are supported;
- immutable file-like records with `file_sha256` have lowercase 64-hex hashes;
- launch-like preflight re-hashes immutable file-like data or fails closed when it cannot verify;
- launch-like preflight does not require integrity re-verification for explicitly allowed `append_only` inputs;
- `directory_manifest_sha256` is rejected for launch-like operations unless a Kikai-generated manifest/verifier is present;
- roles are known canonical roles from the v1 enum;
- `blocked` data sources are rejected;
- `data_source_refs` in run records resolve;
- operation fixtures with `request.data_source_refs` resolve when validated/dry-run;
- `required: true` refs cannot use missing/null ids;
- `role` is present and non-empty;
- `mutable_live` inputs are rejected for launch-like adapter roles unless explicitly allowed.

Expected error codes:

```text
data_source.missing
data_source.invalid
data_source.id_mismatch
data_source.kind_invalid
data_source.status_invalid
data_source.source_type_invalid
data_source.storage_invalid
data_source.immutability_invalid
data_source.integrity_invalid
data_source.role_missing
data_source.role_unknown
data_source.role_incompatible
data_source.required_missing
data_source.mutable_live_forbidden
data_source.integrity_unverified
data_source.directory_manifest_unverified
data_source.lineage_cycle
run.data_source_ref_invalid
operation.data_source_ref_invalid
```

Error ownership:

| Layer | Error examples | Meaning |
| --- | --- | --- |
| data-source record validation | `data_source.kind_invalid`, `data_source.id_mismatch`, `data_source.storage_invalid`, `data_source.integrity_invalid` | The record itself is malformed. |
| data-source registry graph validation | `data_source.lineage_cycle` | Data source provenance graph is cyclic. |
| run validation | `run.data_source_ref_invalid`, `data_source.required_missing`, `data_source.role_unknown`, `data_source.role_incompatible` | A run record references data incorrectly. |
| operation preflight | `operation.data_source_ref_invalid`, `data_source.mutable_live_forbidden`, `data_source.integrity_unverified`, `data_source.directory_manifest_unverified` | A side-effecting operation cannot safely use the declared input. |

## CLI additions

Add top-level command support without breaking existing help:

```bash
kikai data-source show <data_source_id> --project-root <registry> --json
kikai data-source validate <data_source_id> --project-root <registry> --json
kikai data-source create-file <data_source_id> \
  --project-root <registry> \
  --source-type dataset_manifest \
  --host-ref training_host_primary \
  --path env:EXAMPLE_POSE_MANIFEST_PATH \
  --container-mount-path env:CONTAINER_EXAMPLE_POSE_MANIFEST_PATH \
  --role train_manifest \
  --json

kikai data-source create-directory <data_source_id> \
  --project-root <registry> \
  --source-type cache_directory \
  --host-ref training_host_primary \
  --path env:EXAMPLE_CACHE_ROOT \
  --container-mount-path env:CONTAINER_EXAMPLE_CACHE_ROOT \
  --role face_cache \
  --json
```

`create-file` and `create-directory` intentionally accept path/env refs but no hash argument. Kikai Lab resolves the data path and calculates the integrity digest itself at registration time. Directory manifests are generated from lexicographically sorted POSIX relative paths and regular-file sha256 values; symlinks and special files are rejected.

## Public/private hygiene

Kikai Lab is publishable framework code. Data source examples checked into this repo must use generic fixture names and environment refs, not private run names, personal mount paths, real source media paths, real webhook URLs, or private checkpoint paths.

Private data source records for real experiments belong in the private registry/workspace, not public Kikai Lab examples.

## Design gates

Implementation must stop and return to this design if any of these occur:

- A new source type is needed that is not listed above.
- A new role is needed that is not in the canonical role vocabulary. v1 has no project-local `role_namespace` / `custom_roles` extension path.
- An adapter needs mutable live data for a launch/eval/QC operation.
- A launch-like operation cannot re-verify immutable file-like integrity in the target read context.
- A data source must be inferred from argv or environment because the operation lacks refs.
- The implementation would copy large data into the public framework repo.
- The implementation would conflate source code snapshots with data sources.
- The implementation would make data-source validation optional for launch-like operations.
- Artifact promotion would point at a mutable or digest-less artifact without blocking or computing a strong digest.

Before launching any real private training/probe/QC run using this capability, the final run plan must post the exact data source ids, roles, storage, immutability, and forbidden fallbacks to Omoikane.

## TDD implementation tasks

### Task 1: Add schema and example fixture

**Objective:** Define the `kikai_data_source` registry shape and add one generic example fixture.

**Files:**
- Create: `schemas/data_source.schema.json`
- Create: `examples/example_project/data_sources/example_pose_manifest_v1.yaml`
- Modify: `README.md` only if a short registry-object list exists there

**Steps:**

1. Add `schemas/data_source.schema.json` with required fields and enum values from this plan, including the canonical role vocabulary and status semantics. For v1, `role` and `contract.role_compatibility[]` must be strict canonical enums; do not add `role_namespace`, `custom_roles`, or free-form role strings.
2. Add a generic `example_pose_manifest_v1.yaml` fixture using env refs and fake hash values only where schema permits.
3. Use `container_mount_path` for host-path mount targets; reserve `storage_kind: container_path` for data that is already in-container addressed.
4. Run formatting/lint checks that apply to JSON/YAML files.
5. Do not add private paths or real experiment ids.

**Verification:**

Run:

```bash
uv run python -m json.tool schemas/data_source.schema.json >/tmp/kikai-data-source-schema.json
```

Expected: exit code 0.

### Task 2: Add data source loader and validator

**Objective:** Let Kikai load and validate `data_sources/<id>.yaml` records with fail-closed errors.

**Files:**
- Modify: `kikai_lab/validation.py`
- Create or modify: `tests/test_data_source_registry.py`

**Steps:**

1. Write failing tests for valid fixture, missing file, id mismatch, invalid kind, invalid status, invalid storage, invalid immutability, invalid sha256, unknown role, rejected `role_namespace`/`custom_roles`, blocked status, artifact self-reference/cycle, and mutable-live-forbidden helper behavior.
2. Verify RED.
3. Implement `load_data_source(project_root, data_source_id)`.
4. Implement `validate_data_source_record(project_root, data_source_id, record, *, role=None, launch_like=False)`.
5. Implement the canonical role vocabulary in one place and use it for both refs and `contract.role_compatibility`; keep project-local role extension out of v1.
6. Implement `validate_data_sources(project_root)` and call it from `validate_registry_links` or the top-level registry validation path.
7. Verify GREEN.

**Verification:**

Run:

```bash
uv run pytest tests/test_data_source_registry.py -v
```

Expected: all tests pass.

### Task 3: Validate run `data_source_refs`

**Objective:** Make run records explicitly link to data sources and reject missing required inputs.

**Files:**
- Modify: `schemas/run.schema.json`
- Modify: `examples/example_project/runs/example_run.yaml`
- Modify: `kikai_lab/validation.py`
- Modify: `tests/test_data_source_registry.py`

**Steps:**

1. Add failing tests for run refs resolving, missing required id, missing role, unknown role, incompatible role, and `fresh_no_resume: true` rejecting a required `initial_checkpoint`.
2. Verify RED.
3. Add permissive schema support for `data_source_refs` while keeping existing records compatible.
4. Add validation code that walks run refs.
5. Update the example run with generic data source refs, including `fresh_no_resume: true` and `initial_checkpoint: null, required: false`.
6. Verify GREEN.

**Verification:**

Run:

```bash
uv run pytest tests/test_data_source_registry.py -v
uv run kikai validate --project-root examples/example_project --json
```

Expected: tests pass and validation returns `ok: true`.

### Task 4: Add `kikai data-source show/validate`

**Objective:** Make data source records discoverable without requiring users to inspect YAML manually.

**Files:**
- Modify: `kikai_lab/cli.py`
- Modify: `tests/test_data_source_registry.py`

**Steps:**

1. Add failing CLI tests for `kikai data-source show` and `kikai data-source validate`.
2. Verify RED.
3. Extend the parser choices while preserving `kikai --help` and command help.
4. Implement JSON envelope output using existing `envelope`, `emit`, and `error` patterns.
5. Verify GREEN.

**Verification:**

Run:

```bash
uv run kikai data-source show example_pose_manifest_v1 --project-root examples/example_project --json
uv run kikai data-source validate example_pose_manifest_v1 --project-root examples/example_project --json
```

Expected: both return `ok: true` and include `data_source_id`.

### Task 5: Validate operation request `data_source_refs`

**Objective:** Ensure side-effecting launch-like operations fail before execution if their declared data sources are missing or unsafe.

**Files:**
- Modify: `kikai_lab/operation.py`
- Modify: `tests/test_side_effect_single_json.py` or create `tests/test_operation_data_source_refs.py`

**Steps:**

1. Add a failing dry-run test where an operation references a missing data source.
2. Add a failing dry-run test where a launch-like operation references `immutability.mode: mutable_live`.
3. Add a failing dry-run test where a launch-like operation references immutable `file_sha256` data but preflight cannot verify the resolved file hash.
4. Add a passing dry-run test where an adapter explicitly allows an `append_only` `metrics_log` input and the receipt records `append_only_not_rehashed` instead of trying to re-hash it.
5. Add a failing dry-run test where `directory_manifest_sha256` lacks Kikai-generated verifier provenance.
6. Add a passing dry-run test where refs resolve, file hash verification succeeds, and verification status is returned in the operation result/receipt context.
7. Verify RED.
8. Implement operation request validation before adapter execution.
9. Verify GREEN.

**Verification:**

Run:

```bash
uv run pytest tests/test_operation_data_source_refs.py -v
```

Expected: all tests pass.

### Task 6: Documentation and full targeted validation

**Objective:** Document the new registry concept and prove it does not break existing Kikai workflows.

**Files:**
- Modify: `README.md`
- Possibly modify: `docs/plans/2026-06-28-data-source-registry.md` only if implementation reveals necessary design corrections

**Steps:**

1. Add a concise README section distinguishing source snapshots, script bundles, data sources, and artifacts.
2. Run data-source tests.
3. Run existing source snapshot/script bundle tests.
4. Run Kikai validation on the example project.
5. Run public hygiene guard before any push/sync.

**Verification:**

Run:

```bash
uv run pytest tests/test_data_source_registry.py tests/test_source_snapshot_management.py -v
uv run kikai validate --project-root examples/example_project --json
uv run python scripts/check_public_hygiene.py
```

Expected: all pass. If public hygiene guard reports private strings, stop and remove them before continuing.

## Acceptance criteria

- `data_sources/<id>.yaml` is a documented, validated registry object.
- Role vocabulary is canonical-only in v1, shared by run refs and data-source `contract.role_compatibility`, and unknown/project-local extension roles are detected rather than accepted.
- Runs can declare `data_source_refs` and validation resolves them.
- Launch-like operations can declare `request.data_source_refs` and fail closed before side effects when refs are missing, unsafe, or not integrity-verified.
- Code inputs remain represented by `source_snapshot`; executable script/config inputs remain represented by `script_bundle`.
- No public example contains private paths, private run names, real media paths, webhook URLs, or private checkpoint paths.
- `kikai --help`, `kikai data-source --help`, and existing commands continue to work.
- The example project validates successfully.
