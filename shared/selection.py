"""Net-score selection with a switch margin. Buy-and-hold is never a candidate."""
from __future__ import annotations


def apply_hysteresis(
    candidates: list[dict],
    current: str | None,
    margin: float = 0.02,
) -> tuple[str, str]:
    """Pick a strategy from net scores.

    ``margin`` is an absolute score gap. 0.02 means the challenger must lead by
    2 percentage points. A negative net score is never selected: the result is
    ``cash`` (flat / hold) instead.
    """
    rows = [row for row in candidates if str(row.get("name")) not in {"buy_and_hold", "cash"}]
    if not rows:
        return "cash", "No trading strategies were scored, so the book stays in cash."
    ranked = sorted(rows, key=lambda row: (-float(row["score"]), str(row["name"])))
    best = ranked[0]
    best_score = float(best["score"])
    by_name = {str(row["name"]): row for row in rows}
    incumbent = None if current in (None, "", "cash") else by_name.get(str(current))

    if incumbent is None:
        if best_score < 0:
            return (
                "cash",
                f"Best net score is {best_score:.4f} for {best['name']}, which is negative, so the book stays in cash.",
            )
        return (
            str(best["name"]),
            f"No incumbent. Selected {best['name']} with net score {best_score:.4f}.",
        )

    current_score = float(incumbent["score"])
    lead = best_score - current_score
    if str(best["name"]) != str(current) and lead + 1e-12 >= margin and best_score >= 0:
        return (
            str(best["name"]),
            f"Switched from {current} (net score {current_score:.4f}) to {best['name']} "
            f"(net score {best_score:.4f}) because the lead {lead:.4f} meets the {margin:.4f} margin.",
        )
    if current_score < 0:
        return (
            "cash",
            f"Incumbent {current} net score {current_score:.4f} is negative. "
            f"{best['name']} at {best_score:.4f} does not clear the {margin:.4f} margin with a non-negative score, "
            "so the book goes to cash.",
        )
    if str(best["name"]) == str(current):
        return (
            str(current),
            f"Kept {current}. Its net score {current_score:.4f} is still the best non-negative score.",
        )
    return (
        str(current),
        f"Kept {current} (net score {current_score:.4f}). "
        f"{best['name']} at {best_score:.4f} leads by {lead:.4f}, short of the {margin:.4f} switch margin.",
    )
