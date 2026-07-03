#!/usr/bin/env python3
"""Fix the two remaining test/config bugs (run inside ~/exitmgr-app)."""

# 1) test_partial_fill.py: add the missing mock import
fn = "tests/test_partial_fill.py"
t = open(fn).read()
if "from unittest.mock import" not in t:
    lines = t.split("\n")
    idx = 0
    for i, ln in enumerate(lines):
        if ln.startswith("import ") or ln.startswith("from "):
            idx = i + 1
    lines.insert(idx, "from unittest.mock import MagicMock, AsyncMock, patch")
    open(fn, "w").write("\n".join(lines))
    print("added mock import to", fn)
else:
    print("mock import already present in", fn)

# 2) config.py: give Config an `arm` attribute and record it in load_config
fn = "exitmgr/config.py"
t = open(fn).read()
if "Config.arm = False" not in t:
    t = t.replace("Config.loop_mode = False",
                  "Config.loop_mode = False\nConfig.arm = False", 1)
    print("added Config.arm class attribute")
if "cfg.arm = arm" not in t:
    t = t.replace("    cfg.dry_run = not arm",
                  "    cfg.dry_run = not arm\n    cfg.arm = arm", 1)
    print("load_config now records cfg.arm")
open(fn, "w").write(t)
