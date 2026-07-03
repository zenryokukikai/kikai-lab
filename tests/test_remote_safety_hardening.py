"""Review fixes for the remote ssh/docker execution layer (PR #3):

- safety regexes anchor with \\Z, not $, so a trailing newline can't slip a value into
  the remote shell string;
- _SAFE_DOCKER_GPUS rejects a lone (unbalanced) double quote;
- docker_container_restart rejects a traversal container_id;
- the HTML report escapes project_id (stored-XSS sink).
"""

from kikai_lab.operation import (
    _SAFE_CONTAINER_NAME,
    _SAFE_DOCKER_GPUS,
    _SAFE_DOCKER_NETWORK,
    _SAFE_DOCKER_PATH,
    _SAFE_IMAGE_TAG,
    _SAFE_SSH_HOST,
    OperationError,
    execute_docker_container_restart_operation,
)
from kikai_lab.report import render_report_html


def test_safety_regexes_reject_trailing_newline():
    cases = [
        (_SAFE_CONTAINER_NAME, "run1"),
        (_SAFE_IMAGE_TAG, "img:tag"),
        (_SAFE_DOCKER_NETWORK, "host"),
        (_SAFE_DOCKER_PATH, "/work/dir"),
        (_SAFE_SSH_HOST, "host.example"),
    ]
    for rx, good in cases:
        assert rx.match(good), good                 # good value accepted
        assert rx.match(good + "\n") is None, good  # trailing newline rejected (\Z, not $)


def test_gpus_regex_rejects_unbalanced_quote():
    assert _SAFE_DOCKER_GPUS.match("all")
    assert _SAFE_DOCKER_GPUS.match("none")
    assert _SAFE_DOCKER_GPUS.match("2")
    assert _SAFE_DOCKER_GPUS.match("device=0,1")
    assert _SAFE_DOCKER_GPUS.match('"device=0,1"')       # balanced quotes ok
    assert _SAFE_DOCKER_GPUS.match('device=0"') is None  # lone trailing quote rejected
    assert _SAFE_DOCKER_GPUS.match('"device=0') is None   # lone leading quote rejected


def test_docker_container_restart_rejects_traversal_container_id(tmp_path):
    (tmp_path / "containers").mkdir()
    for bad in ["../evil", "a/b", "..", "/etc/x", "sub/../../x"]:
        try:
            execute_docker_container_restart_operation(
                {"project_root": str(tmp_path), "container_id": bad, "mode": "teardown"}
            )
            raise AssertionError(f"expected rejection for {bad!r}")
        except OperationError as exc:
            assert exc.code == "operation.docker_container_restart_invalid", bad


def test_report_html_escapes_project_id():
    report = {"project": {"project_id": "</title><script>alert(1)</script>"}}
    out = render_report_html(report)
    # the raw HTML-breakout sequence (title close + script open) must not appear -- in the
    # <title> project_id is html-escaped, and in the inlined JSON payload "</" is "<\\/".
    assert "</title><script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out  # escaped into <title>
