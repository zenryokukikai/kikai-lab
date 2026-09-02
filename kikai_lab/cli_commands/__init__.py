from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import NamedTuple

from kikai_lab.cli_commands import data, operations, project, remote


class Command(NamedTuple):
    configure: Callable[[argparse.ArgumentParser], None]
    handler: Callable[[argparse.Namespace], int]


COMMANDS: dict[str, Command] = {
    "validate": Command(
        project.configure_project_state_parser, project.command_validate
    ),
    "current": Command(
        project.configure_project_state_parser, project.command_current
    ),
    "show": Command(project.configure_show_parser, project.command_show),
    "next": Command(project.configure_project_state_parser, project.command_next),
    "script-bundle": Command(
        operations.configure_script_bundle_parser, operations.command_script_bundle
    ),
    "decision": Command(
        operations.configure_decision_parser, operations.command_decision
    ),
    "report": Command(operations.configure_report_parser, operations.command_report),
    "remote-launch": Command(
        remote.configure_remote_launch_parser, remote.command_remote_launch
    ),
    "template": Command(
        operations.configure_template_parser, operations.command_template
    ),
    "source-snapshot": Command(
        data.configure_source_snapshot_parser, data.command_source_snapshot
    ),
    "data-source": Command(
        data.configure_data_source_parser, data.command_data_source
    ),
    "server": Command(remote.configure_server_parser, remote.command_server),
    "tensorboard": Command(
        remote.configure_tensorboard_parser, remote.command_tensorboard
    ),
    "publish": Command(data.configure_publish_parser, data.command_publish),
    "reconcile": Command(remote.configure_reconcile_parser, remote.command_reconcile),
    "serve": Command(remote.configure_serve_parser, remote.command_serve),
}
