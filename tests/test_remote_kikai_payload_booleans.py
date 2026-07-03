"""Regression: remote_kikai_python_payload must embed booleans/null safely.

The remote bootstrap builds a Python program with the payload assigned to
PAYLOAD. Embedding raw json.dumps (true/false/null) as a bare literal raised
`NameError: name 'true' is not defined` on line 1 the instant any request
carried a boolean (e.g. detach: true). The payload must round-trip as real
Python via json.loads.
"""
import kikai_lab.operation as op


def _exec_payload_assignment(script: str) -> dict:
    # The PAYLOAD assignment is the head of the script (import + assignment).
    head = "\n".join(script.splitlines()[:2])
    ns: dict = {}
    exec(head, ns)  # noqa: S102 - testing generated code is intentional
    return ns["PAYLOAD"]


def test_payload_with_boolean_and_null_is_valid_python():
    payload = {"detach": True, "flag": False, "missing": None,
               "nested": {"on": True}, "list": [True, None, "true"]}
    script = op.remote_kikai_python_payload(payload)
    result = _exec_payload_assignment(script)
    assert result["detach"] is True
    assert result["flag"] is False
    assert result["missing"] is None
    assert result["nested"]["on"] is True
    assert result["list"] == [True, None, "true"]


def test_payload_with_quotes_and_unicode_round_trips():
    payload = {"s": 'a"b\\c', "u": "日本語", "detach": True}
    result = _exec_payload_assignment(op.remote_kikai_python_payload(payload))
    assert result["s"] == 'a"b\\c'
    assert result["u"] == "日本語"
    assert result["detach"] is True


def test_whole_generated_remote_script_parses():
    # Not just the PAYLOAD head: the ENTIRE generated remote program (payload
    # assignment + REMOTE_KIKAI_SCRIPT) must be valid Python so a boolean/null in
    # the request never breaks the remote bootstrap at parse time.
    payload = {"detach": True, "flag": False, "missing": None,
               "nested": {"on": True}, "list": [True, None, "true"]}
    script = op.remote_kikai_python_payload(payload)
    compile(script, "<remote>", "exec")
