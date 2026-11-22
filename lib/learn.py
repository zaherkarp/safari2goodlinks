"""Learning from Z's corrections, and the numbers `status` reports.

Every time `apply` routes a tab, the ledger stores a decision row: the
site, what the tool guessed, and where Z actually put it. Nothing else is
stored here; the learned rules are computed from those rows each run, so
there is no separate rule list to drift out of date:

    a site becomes a learned rule when Z put at least `min_count` of its
    tabs in the same section, and that section holds at least `min_share`
    of all his decisions for the site.

Because rules are recomputed, they correct themselves. If Z starts moving
a site's tabs somewhere else, the share drops below `min_share` and the
rule disappears until a new pattern is clear. `stg2gl-triage forget SITE`
deletes a site's decisions outright.

Learned rules never auto-send. They only change the guess Z reviews.
"""

from __future__ import annotations

from classify import UNSORTED


def learned_rules(conn, min_count: int = 3, min_share: float = 0.8):
    """Return [(site, category, why)] from the decisions table."""
    rows = conn.execute(
        "SELECT site, final, COUNT(*) FROM decisions GROUP BY site, final"
    ).fetchall()
    per_site = {}
    for site, final, n in rows:
        per_site.setdefault(site, {})[final] = n
    out = []
    for site, counts in sorted(per_site.items()):
        total = sum(counts.values())
        final, n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        if final != UNSORTED and n >= min_count and n / total >= min_share:
            out.append((site, final, f"learned: you put {n} of {total} {site} tabs in {final}"))
    return out


def overridden_rules(conn, min_count: int = 2):
    """Hand-written rules whose guesses Z keeps changing.

    Learned rules cannot fix these, because hand-written rules are
    evaluated first, so `status` lists them for Z to edit instead.
    Returns [(why, guessed, final, count)].
    """
    return conn.execute(
        """
        SELECT why, guessed, final, COUNT(*) AS n FROM decisions
        WHERE guessed <> final AND guessed <> ? AND why NOT LIKE 'learned:%'
        GROUP BY why, guessed, final HAVING n >= ?
        ORDER BY n DESC
        """,
        (UNSORTED, min_count),
    ).fetchall()


def sorting_health(conn, last_runs: int = 3):
    """Numbers for deciding whether page labels (option 4) would help.

    unsorted_share: of tabs the last few collects classified, how many
        no rule could place.
    correction_share: of tabs the tool did guess (not unsorted) and Z then
        routed, how many he moved somewhere else.
    """
    runs = conn.execute(
        "SELECT total, unsorted FROM runs ORDER BY id DESC LIMIT ?", (last_runs,)
    ).fetchall()
    total = sum(r[0] for r in runs)
    unsorted = sum(r[1] for r in runs)
    guessed = conn.execute("SELECT COUNT(*) FROM decisions WHERE guessed <> ?", (UNSORTED,)).fetchone()[0]
    wrong = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE guessed <> ? AND guessed <> final", (UNSORTED,)
    ).fetchone()[0]
    return {
        "runs": len(runs),
        "classified": total,
        "unsorted_share": (unsorted / total) if total else None,
        "guessed_routed": guessed,
        "correction_share": (wrong / guessed) if guessed else None,
    }


def page_hint_advice(health, page_hints_mode: str, min_tabs: int = 20,
                     unsorted_threshold: float = 0.4, correction_threshold: float = 0.3):
    """One line of advice, or None. Only speaks up with enough data."""
    if page_hints_mode != "off" or health["classified"] < min_tabs:
        return None
    u, c = health["unsorted_share"], health["correction_share"]
    if u is not None and u >= unsorted_threshold:
        return (f"{u:.0%} of recent tabs matched no rule. Page labels may help: "
                'set "page_hints": "unsorted" in the triage section of rules.json.')
    if c is not None and c >= correction_threshold and health["guessed_routed"] >= min_tabs:
        return (f"You moved {c:.0%} of guessed tabs. Check the rules listed above, "
                'or try "page_hints": "unsorted" for more information on each tab.')
    return None
