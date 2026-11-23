"""Loaded through PYTHONPATH into every python process of a lane stage (also the subprocesses a stage spawns): installs ofh.lane.overlay when the
driver set OMH_SCRATCH, so scratch redirection and the I/O audit cover scripts the stage merely runs."""
import os
import sys

if os.environ.get("OMH_SCRATCH") or os.environ.get("OMH_IOLOG"):
    _root = os.environ.get("OMH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    sys.path.insert(0, _root)
    try:
        from ofh.lane import overlay
        overlay.install()
    finally:
        sys.path.remove(_root)
