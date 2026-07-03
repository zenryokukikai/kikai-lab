from __future__ import annotations

import json

from kikai_lab.training_control import (
    clear_stop_request,
    read_stop_request,
    request_stop,
    should_stop,
)


def test_training_control_stop_request_round_trip(tmp_path):
    control_file = tmp_path / "stop.json"

    assert should_stop(control_file) is False
    payload = request_stop(
        control_file,
        reason="train loss plateau",
        source="test",
        step=123,
        extra={"patience": 10},
    )

    assert payload["kind"] == "kikai_training_stop_request"
    assert payload["step"] == 123
    assert should_stop(control_file) is True
    stored = read_stop_request(control_file)
    assert stored is not None
    assert stored["reason"] == "train loss plateau"
    assert stored["extra"] == {"patience": 10}
    assert json.loads(control_file.read_text())["source"] == "test"
    assert clear_stop_request(control_file) is True
    assert should_stop(control_file) is False
