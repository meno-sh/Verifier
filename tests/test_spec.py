#!/usr/bin/env python3
"""Assert that the class blocks of SPEC.md match the code.

docs/spec_signatures.py writes the blocks. This test runs it in check mode, so a change
to a major class must also change SPEC.md. Returns 1 when a block differs, and names the
classes.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "docs"))
import spec_signatures  # noqa: E402

if __name__ == "__main__":
    sys.exit(spec_signatures.main(["--check"]))
