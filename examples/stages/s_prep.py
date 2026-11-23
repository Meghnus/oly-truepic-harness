"""stage 'prep': runs alone first (every other stage depends on it), leaves a file in its scratch dir."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lane import stage          # noqa: E402


def run(ctx):
    p = os.path.join(ctx.scratch, "prepared.txt")
    with open(p, "w") as f:
        f.write("prepared\n")
    ctx.sub("prepared file written", os.path.exists(p), ["wrote prepared.txt into the stage's private scratch dir"])


stage.main(run)
