"""Derive stage budgets from frozen container limits, including older snapshots."""

from maestro.execution.materials import DEFAULT_CPUS, DEFAULT_MEMORY_MB


def stage_resources(settings: dict, definition: dict) -> dict[str, dict]:
    environments = [dict(e) for e in settings.get("environments") or []]
    if not environments:
        return {
            "execution": {
                "cpus": float(settings.get("cpus") or DEFAULT_CPUS),
                "memory_mb": int(settings.get("memory_mb") or DEFAULT_MEMORY_MB),
            }
        }
    # Legacy overrides were totals across *all* containers. Only the positive
    # difference was added to main. Reconstruct that before splitting phases.
    main = next(e for e in environments if e["role"] == "environment/main")
    for key in ("cpus", "memory_mb"):
        extra = max(0, (settings.get(key) or 0) - sum(e[key] for e in environments))
        main[key] += int(extra)

    def total(entries):
        return {key: sum(e[key] for e in entries) for key in ("cpus", "memory_mb")}

    execution = [e for e in environments if e["role"].startswith("environment/")]
    result = {"execution": total(execution)}
    verifier = next((e for e in environments if e["role"] == "verifier"), None)
    if verifier:
        # Without tests/main Harbor restarts the environment/ Compose group, with
        # the verifier's main limits and the original auxiliary service limits.
        images = ((definition.get("preparation") or {}).get("spec") or {}).get("images") or []
        own = any(e.get("role") == "tests/main" for e in images)
        directory = "tests/" if own else "environment/"
        auxiliaries = [
            e
            for e in environments
            if e["role"].startswith(directory) and not e["role"].endswith("/main")
        ]
        result["verification"] = total([verifier, *auxiliaries])
    return result
