"""The gate: provenance and recompute, which the core runs on each claim before the claim checks.

A broken gate makes the verdict REJECT. No registered check can replace the gate or switch
provenance off (docs/SCHEMA.md, section 3).
"""
