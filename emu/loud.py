"""emu.loud: the one way the toolkit refuses to continue.

fatal() prints the reason and ends the process with exit code 86 through os._exit, so no `except Exception`, atexit hook or `finally` in
calling code can turn a broken setup into a green run.  86 is reserved: a lane driver must treat it as FAILED and never as 'inconclusive'
or 'skipped' (lane.driver does).
"""
import os
import sys
import traceback

EXIT_LOUD = 86


def fatal(what, env_name="OMH_RUST"):
    try:
        sys.stderr.write("omh FATAL (%s=%s): %s\n" % (env_name, os.environ.get(env_name), what))
        traceback.print_exc()
        sys.stderr.flush()
    finally:
        os._exit(EXIT_LOUD)
