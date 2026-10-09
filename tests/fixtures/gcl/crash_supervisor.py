#!/usr/bin/env python3
"""SIGKILL the supervisor immediately after one durable rollover transition."""
import os
import signal
import sys

from gcl.runtime.files import home_path
from gcl.supervisor import Supervisor


home, mission, target = sys.argv[1:]
controller = Supervisor(home_path(home), mission)


def wrap(name):
    original = getattr(controller.store, name)

    def call(*args, **kwargs):
        value = original(*args, **kwargs)
        rid = value if name == "request_rollover" else args[0]
        if controller.store.rollover_state(rid) == target:
            os.kill(os.getpid(), signal.SIGKILL)
        return value

    setattr(controller.store, name, call)


for method in ("request_rollover", "advance", "save_checkpoint", "transfer_ownership"):
    wrap(method)
controller.run()
