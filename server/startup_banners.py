from __future__ import annotations

import textwrap
import threading
from typing import Literal

from loguru import logger


def log_dev_startup_banner(mode: Literal["verify", "exec"], port: int) -> None:
    if mode == "exec":
        command = (
            "curl --request POST \\\n"
            f"  --url http://localhost:{port}/exec/create_states \\\n"
            "  --header 'Content-Type: application/json' \\\n"
            "  --data '{"
            '"env_profile":"lean_init_test",'
            '"items":[{'
            '"item_id":"exec-smoke",'
            '"code":"theorem t : True := by\\n  trivial"'
            "}]}' | jq\n"
        )
    else:
        command = (
            "curl --request POST \\\n"
            f"  --url http://localhost:{port}/api/check \\\n"
            "  --header 'Content-Type: application/json' \\\n"
            "  --data '{"
            '"snippets":[{"id":"check-nat-test","code":"#check Nat"}]'
            "}' | jq\n"
        )
    threading.Timer(
        0.1,
        lambda: logger.info("Try me with:\n" + textwrap.indent(command, "  ")),
    ).start()
