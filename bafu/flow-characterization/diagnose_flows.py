#!/usr/bin/env python3
"""List the biosphere flows of a database that the impact method does NOT characterize.

A flow linked to the biosphere but without any characterization factor contributes a silent
zero to the score. On a freshly imported database (e.g. BAFU) that is how a naming or
compartment mismatch hides: the flow links, the LCA runs, the substance simply counts as zero.
This script surfaces those flows so the mapping / synonym work can be targeted instead of guessed.

Reads its configuration from the environment (a local `.env`, see `.env.example`):
BRIGHTWAY2_DIR, EB_PROJECT, EB_METHOD, EB_BIOSPHERE, EB_DATABASE.

    uv run diagnose_flows.py [out.csv]

Prints the characterized / not-characterized counts and the top uncharacterized flows ranked by
how many processes emit them. With a path argument, writes the full list to CSV.

Note: it compares biosphere **node ids**. Run it only against a project where the database, the
method and the datapackages were (re)imported and synced together, otherwise the method's CF ids
no longer match the current biosphere node ids and everything looks uncharacterized.
"""

import csv
import os
import sys
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()  # BRIGHTWAY2_DIR must be in the environment before bw2data is imported

import bw2data  # noqa: E402

PROJECT = os.environ["EB_PROJECT"]
METHOD = os.environ["EB_METHOD"]
BIOSPHERE = os.environ["EB_BIOSPHERE"]
DATABASE = os.environ["EB_DATABASE"]


def characterized_ids() -> set[int]:
    """Node ids carrying a non-zero CF in at least one category of the method family."""
    ids: set[int] = set()
    for method in bw2data.methods:
        if method[0] != METHOD:
            continue
        for key, amount, *_ in bw2data.Method(method).load():
            if amount != 0:
                ids.add(key)
    return ids


def main() -> None:
    bw2data.projects.set_current(PROJECT)

    characterized = characterized_ids()
    biosphere_nodes = {n["code"]: n for n in bw2data.Database(BIOSPHERE)}
    # methods key on node id, exchanges on (database, code): bridge one to the other
    code_to_id = {code: node.id for code, node in biosphere_nodes.items()}

    n_processes: dict[str, int] = defaultdict(int)  # flow code -> nb emitting processes
    total_abs: dict[str, float] = defaultdict(float)  # flow code -> sum of |amount|
    for ds in bw2data.Database(DATABASE).load().values():
        for exc in ds.get("exchanges", []):
            if exc.get("type") != "biosphere":
                continue
            key = exc.get("input")
            if not key:  # post-import every biosphere edge is linked, but be explicit
                continue
            code = key[1]
            n_processes[code] += 1
            total_abs[code] += abs(exc.get("amount", 0.0) or 0.0)

    uncharacterized = [c for c in n_processes if code_to_id.get(c) not in characterized]
    uncharacterized.sort(key=lambda c: n_processes[c], reverse=True)

    print(f"database                          : {DATABASE!r}")
    print(f"distinct biosphere flows used     : {len(n_processes)}")
    print(f"  characterized by the method     : {len(n_processes) - len(uncharacterized)}")
    print(f"  NOT characterized (silent zero) : {len(uncharacterized)}")
    print("\nTop uncharacterized flows (by nb of emitting processes):")
    print(f"{'#proc':>6}  {'sum|amount|':>12}  name  [categories]  unit")
    for code in uncharacterized[:40]:
        node = biosphere_nodes.get(code, {})
        cats = ", ".join(node.get("categories", []) or [])
        print(
            f"{n_processes[code]:>6}  {total_abs[code]:>12.4g}  "
            f"{node.get('name', code)!r}  [{cats}]  {node.get('unit', '')}"
        )

    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", newline="") as fp:
            w = csv.writer(fp)
            w.writerow(["nb_processes", "sum_abs_amount", "name", "categories", "unit", "code"])
            for code in uncharacterized:
                node = biosphere_nodes.get(code, {})
                w.writerow(
                    [
                        n_processes[code],
                        total_abs[code],
                        node.get("name", ""),
                        ", ".join(node.get("categories", []) or []),
                        node.get("unit", ""),
                        code,
                    ]
                )
        print(f"\nFull list written to {sys.argv[1]}")


if __name__ == "__main__":
    main()
