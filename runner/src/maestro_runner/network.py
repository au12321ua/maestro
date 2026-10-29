"""Platform network tightening for model runs (U-18, M1 TECH-02).

Harbor cannot tighten a task from the job (``extra_allowed_hosts`` only adds hosts and is
ignored for public tasks), so the Runner rewrites a private copy of each task: each phase
takes the stricter of the author's and the platform's policy, allowlists intersect, and
the author's stricter choice is always kept. Setup keeps the author's baseline, so tools
can be installed.

Two gaps M1 found in Harbor 0.24.0 are closed by wrapping its (private, version-pinned)
policy switch: once a phase was restricted, a request to go back to public becomes
no-network (Harbor reopens public between agent and verification and after
verification); and TCP that was not redirected through the egress proxy is reset, which
cuts connections opened before the switch.

Harbor's kernel probe is optimistic where ``/proc/config.gz`` is missing, and a missing
capability crashes the whole job, so the Runner checks before creating the job and
rejects the run instead (never falling back to public).
"""

from pathlib import Path

RANK = {"no-network": 0, "allowlist": 1, "public": 2}
SIDECAR = "harbor-docker-egress-control-sidecar"
_REJECT_UNREDIRECTED = [
    "meta", "l4proto", "tcp", "ct", "status", "and", "dnat", "==", "0",
    "reject", "with", "tcp", "reset",
]  # fmt: skip


def check_platform(platform: dict | None) -> None:
    """``{"agent": {"mode", "hosts"?}, "verifier": {...}}``; absent means author's."""
    if platform is None:
        return
    if not isinstance(platform, dict) or set(platform) - {"agent", "verifier"}:
        raise ValueError("network: only agent and verifier phases")
    for phase in platform.values():
        if not isinstance(phase, dict) or phase.get("mode") not in RANK:
            raise ValueError("network: each phase needs a mode")
        hosts = phase.get("hosts", [])
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise ValueError("network: hosts must be a list of names")


def stricter(author, platform: dict | None):
    from harbor.models.task.config import NetworkMode, NetworkPolicy

    if platform is None:
        return author
    a, p = author.network_mode.value, platform["mode"]
    if a == "allowlist" and p == "allowlist":
        allowed = set(platform.get("hosts", []))
        hosts = [h for h in author.allowed_hosts if h in allowed]
        return NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=hosts)
    if RANK[a] <= RANK[p]:
        return author
    return NetworkPolicy(network_mode=NetworkMode(p), allowed_hosts=platform.get("hosts", []))


def tighten(task_dir: Path, platform: dict) -> dict:
    """Rewrite ``task.toml`` of a private task copy in place; returns the plan for evidence."""
    from harbor.models.task.config import TaskConfig

    path = task_dir / "task.toml"
    config = TaskConfig.model_validate_toml(path.read_text())
    baseline = config.environment.resolve_baseline()
    plan = {"environment": baseline.model_dump(mode="json"), "steps": {}}
    for role in ("agent", "verifier"):
        section = getattr(config, role)
        author = section.explicit_phase_policy() or baseline
        # A step's own setting replaces the task's in Harbor, so each step is tightened
        # from what Harbor would have used for it.
        for step in config.steps or []:
            own = getattr(step, role)
            step_author = own.explicit_phase_policy() or author
            plan["steps"].setdefault(step.name, {})[role] = _apply(
                own, step_author, platform.get(role)
            )
        plan[role] = _apply(section, author, platform.get(role))
    path.write_text(config.model_dump_toml())
    return plan


def _apply(section, author, platform: dict | None) -> dict:
    effective = stricter(author, platform)
    section.network_mode = effective.network_mode
    section.allowed_hosts = (
        list(effective.allowed_hosts) if effective.network_mode.value == "allowlist" else None
    )
    return {
        "author": author.model_dump(mode="json"),
        "effective": effective.model_dump(mode="json"),
    }


def unrouted_services(task_dir: Path) -> list[str]:
    """Services of the author's compose files that set their own networks: the agent's
    environment and a separate verifier's under ``tests/``. Harbor routes only services
    without them through the egress sidecar, so these would stay open."""
    import yaml

    found = []
    for directory in ("environment", "tests"):
        path = task_dir / directory / "docker-compose.yaml"
        if not path.is_file():
            continue
        try:
            document = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            found.append(f"{directory}/(unreadable compose file)")
            continue
        services = document.get("services") if isinstance(document, dict) else None
        if not isinstance(services, dict):
            continue
        prefix = "" if directory == "environment" else f"{directory}/"
        found += [
            f"{prefix}{name}"
            for name, config in services.items()
            if isinstance(config, dict) and ("network_mode" in config or "networks" in config)
        ]
    return sorted(found)


def restricted(plan: dict) -> bool:
    phases = [plan[role] for role in ("agent", "verifier")]
    phases += [phase for step in plan.get("steps", {}).values() for phase in step.values()]
    return plan["environment"]["network_mode"] != "public" or any(
        phase["effective"]["network_mode"] != "public" for phase in phases
    )


def supported() -> bool:
    from harbor.environments.docker.docker import DockerEnvironment

    return bool(DockerEnvironment._egress_control_kernel_support())


def harden() -> None:
    """Make restriction one-way and reset unredirected TCP (see the module docstring)."""
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import NetworkMode, NetworkPolicy

    original = DockerEnvironment._apply_network_policy
    if getattr(original, "_maestro_hardened", False):
        return

    async def apply(self, policy):
        if (
            getattr(self, "_maestro_restricted", False)
            and policy.network_mode == NetworkMode.PUBLIC
        ):
            policy = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
        await original(self, policy)
        if policy.network_mode != NetworkMode.PUBLIC:
            self._maestro_restricted = True
            await self._run_docker_compose_command(
                ["exec", "--no-TTY", SIDECAR, "nft", "add", "rule", "inet", "gost_egress",
                 "egress", *_REJECT_UNREDIRECTED]
            )  # fmt: skip

    apply._maestro_hardened = True
    DockerEnvironment._apply_network_policy = apply
