# TRT cache guard adapter plan

## Goal

Make Kikai fail closed when a checkpoint QC pipeline does not explicitly require TRT compile-cache-backed inference. This supports the dogfood Go condition that preview/diagnostic artifacts must be generated through the planned TRT/cache path, not an accidental no-cache/disabled path.

This adapter is a metadata/config guard. It does not inspect remote/container filesystem contents; a bundled container preflight should verify actual cache directory existence and runtime availability.

## Scope

Implement `adapter: trt_cache_guard`.

In scope:

- read `<project-root>/current.json`;
- require `model_arch` to match `current_model_arch`;
- require non-empty `trt_cache_dir`;
- require non-empty `compile_mode`;
- require `require_compile_cache: true`;
- reject compile modes that explicitly disable cache use: `disabled`, `none`, `no_cache`, `off`;
- write `<project-root>/guard_records/<guard_id>.json` only on success;
- fail closed without records when cache use is not explicit or model arch is wrong.

Out of scope:

- checking cache directory existence in container;
- running TensorRT/Torch-TensorRT;
- measuring compile vs steady-state runtime;
- preview/diagnostic generation.

## Operation shape

```json
{
  "schema_version": 1,
  "kind": "kikai_operation",
  "request": {
    "operation": "trt_cache_guard",
    "project_root": "examples/example_project",
    "adapter": "trt_cache_guard",
    "guard_id": "example_run_step003000_trt_cache_guard",
    "model_arch": "example_arch_v1",
    "trt_cache_dir": "/workspace/trt_cache/example_run",
    "compile_mode": "reuse_cache",
    "require_compile_cache": true
  }
}
```

## Acceptance

- Matching model arch + explicit cache requirement passes and writes a guard record.
- `require_compile_cache: false` fails without record.
- Wrong model arch fails without record.
- Disabled/no-cache compile mode fails without record.
- Full pytest, ruff, and example validation pass locally and on training-host.example.
