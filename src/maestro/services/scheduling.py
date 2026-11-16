"""Bounded multi-resource packing within a run's FIFO priority."""

from maestro.core import clock

CANDIDATES = 32
BEAM_WIDTH = 128
PROTECT_AFTER_SECONDS = 30
PROTECT_AFTER_BYPASSES = 8


def protected(item: dict, now) -> bool:
    return item["bypass_count"] >= PROTECT_AFTER_BYPASSES or (
        item["ready_at"] is not None
        and (now - clock.from_text(item["ready_at"])).total_seconds() >= PROTECT_AFTER_SECONDS
    )


def pack(
    candidates: list[dict], slots: int, cpus: float, memory: int, host_cpus: float, host_memory: int
) -> list[dict]:
    """Keep a bounded beam of complementary CPU/memory combinations.

    Equal scores prefer older work. This is an online heuristic, not a prediction
    of runtime or an optimal makespan solver.
    """
    beam = [(0.0, 0, ())]

    def score(state):
        cpu, mem, chosen = state
        c, m = cpu / host_cpus, mem / host_memory
        return (c + m + min(c, m) * 0.25, len(chosen), tuple(-i for i in chosen))

    for index, item in enumerate(candidates[:CANDIDATES]):
        need = item["need"]
        added = []
        for cpu, mem, chosen in beam:
            if (
                len(chosen) < slots
                and cpu + need["cpus"] <= cpus + 1e-9
                and mem + need["memory_mb"] <= memory
            ):
                added.append((cpu + need["cpus"], mem + need["memory_mb"], (*chosen, index)))
        # Deduplicate equivalent resource totals, retaining their stable oldest set.
        states = {}
        for state in sorted([*beam, *added], key=score, reverse=True):
            states.setdefault((round(state[0], 6), state[1], len(state[2])), state)
        beam = sorted(states.values(), key=score, reverse=True)[:BEAM_WIDTH]
    return [candidates[i] for i in max(beam, key=score)[2]]
