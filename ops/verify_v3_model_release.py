#!/usr/bin/env python3
"""Read-only preflight for the signed v3 model release gate (no broker access)."""
import argparse
import json
import sys

import yaml

from exitmgr import model_release_gate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the configured v3 promotion against the active local runtime")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    try:
        with open(args.config, "r", encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        if not isinstance(config, dict):
            raise model_release_gate.ModelReleaseGateError("config root must be a mapping")
        trading = config.get("trading") or {}
        settings = model_release_gate.settings_from_mapping(trading)
        if not settings.enabled:
            raise model_release_gate.ModelReleaseGateError(
                "model release gate is disabled; nothing is eligible for entry")
        evidence = model_release_gate.preflight_v3_release(
            settings, endpoint=trading.get("llm_endpoint", ""))
    except (OSError, yaml.YAMLError, model_release_gate.ModelReleaseGateError) as exc:
        print("BLOCKED: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
