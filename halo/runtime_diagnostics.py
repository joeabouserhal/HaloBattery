"""Local process diagnostics for the windowless Windows build."""
from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import threading


def install(logger, data_dir):
    """Keep otherwise invisible exceptions and native fault stacks on disk."""
    fault_file = None
    try:
        fault_file = open(os.path.join(data_dir, "crash.log"), "a", encoding="utf-8")
        faulthandler.enable(file=fault_file, all_threads=True)
    except Exception as exc:
        if fault_file is not None:
            fault_file.close()
            fault_file = None
        logger.warning("fatal-crash logging unavailable: %s", exc)

    previous_hook = sys.excepthook

    def main_error(exc_type, value, traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            previous_hook(exc_type, value, traceback)
            return
        logger.critical("unhandled main-thread exception",
                        exc_info=(exc_type, value, traceback))

    def thread_error(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "unknown"
        logger.critical("unhandled exception in thread %s", name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def shutdown():
        logger.info("interpreter shutdown pid=%d", os.getpid())
        if fault_file is not None:
            faulthandler.disable()
            fault_file.close()

    sys.excepthook = main_error
    threading.excepthook = thread_error
    atexit.register(shutdown)
    logger.info("runtime diagnostics enabled pid=%d executable=%s", os.getpid(), sys.executable)
