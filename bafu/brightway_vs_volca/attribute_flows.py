#!/usr/bin/env python3
"""Attribute one (product, category) divergence to the biosphere flow that drives it.

The per-product counterpart to `bafu/flow-characterization/diagnose_flows.py` (which scans the
whole database for uncharacterised flows). Given a product name substring and a category name
substring, it runs the single-category LCA on the ecobalyse-Brightway side and prints:

  * the top flows that DO contribute to the score, and
  * the flows in the supply chain with a non-zero amount but NO characterisation factor -- the
    silent zeros where a name/compartment mismatch (or a genuine method-coverage boundary) hides.

That is how the two surviving brightway_vs_volca divergences were attributed (see README):
antimony extraction charged to `Stibnite, in ground` (uncharacterised on the ecobalyse side), and
offshore wells' `Occupation, seabed, ...` scored by VoLCA but not by EF 3.1 Land use.

Reads BRIGHTWAY2_DIR, EB_PROJECT, EB_DATABASE, EB_METHOD from the local `.env` (same as compare.py).

    uv run attribute_flows.py "Antimony, at refinery" "Resource use, minerals and metals"
    uv run attribute_flows.py "offshore, at ocean" "Land use"
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()
import bw2data, bw2calc  # noqa: E402
import numpy as np  # noqa: E402


def method_tuple(method_family: str, cat_sub: str):
    """The method category whose name matches cat_sub (exact wins over substring)."""
    fam = [m for m in bw2data.methods if m[0] == method_family]
    exact = [m for m in fam if any(cat_sub == p for p in m[1:])]
    sub = [m for m in fam if any(cat_sub in p for p in m[1:])]
    hits = exact or sub
    if not hits:
        sys.exit(f"no category matching {cat_sub!r} in method {method_family!r}")
    return hits[0]


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} '<product name substring>' '<category substring>'")
    prod_sub, cat_sub = sys.argv[1], sys.argv[2]

    bw2data.projects.set_current(os.environ["EB_PROJECT"])
    db = bw2data.Database(os.environ["EB_DATABASE"])
    acts = [a for a in db if prod_sub in a["name"]]
    if not acts:
        sys.exit(f"no product matching {prod_sub!r} in {os.environ['EB_DATABASE']!r}")
    if len(acts) > 1:
        print(f"note: {len(acts)} products match, using {acts[0]['name']!r}")
    act = acts[0]

    m = method_tuple(os.environ["EB_METHOD"], cat_sub)
    cfs = {k: a for k, a, *_ in bw2data.Method(m).load() if a != 0}

    lca = bw2calc.LCA({act.id: 1}, method=m)
    lca.lci()
    lca.lcia()
    print(f"product  : {act['name']!r}")
    print(f"category : {m[1:]}   ({len(cfs)} non-zero CFs)")
    print(f"score    : {lca.score:.6g} {bw2data.methods[m].get('unit', '')}\n")

    rev = lca.dicts.biosphere.reversed  # matrix row -> biosphere node id
    contrib = np.asarray(lca.characterized_inventory.sum(axis=1)).ravel()
    amount = np.asarray(lca.inventory.sum(axis=1)).ravel()

    def line(r):
        n = bw2data.get_node(id=rev[r])
        cf = cfs.get(rev[r])
        cf_s = f"CF={cf:.4g}" if cf is not None else "CF=none"
        return (f"  amount={amount[r]:+.4g}  contrib={contrib[r]:+.4g}  {cf_s:>12}  "
                f"{n['name']!r} {tuple(n.get('categories', ()))} [{n.get('unit')}]")

    print("top characterised flows:")
    for r in np.argsort(-np.abs(contrib))[:8]:
        if contrib[r] == 0:
            break
        print(line(r))

    # Uncharacterised flows that live in the SAME characterisation domain as this category's CFs
    # (same top-level compartment + unit as some characterised flow). This drops emissions/water
    # the category never scores anyway and surfaces the real suspect -- a resource/land flow whose
    # siblings are characterised but which the method (as imported) misses. Largest amount first.
    def domain(nid):
        n = bw2data.get_node(id=nid)
        return ((n.get("categories") or (None,))[0], n.get("unit"))

    char_domains = {domain(nid) for nid in cfs}
    silent = [r for r in range(len(amount))
              if amount[r] != 0 and rev[r] not in cfs and domain(rev[r]) in char_domains]
    silent.sort(key=lambda r: -abs(amount[r]))
    print("\nuncharacterised flows in this category's domain (silent zeros, the suspects):")
    for r in silent[:8]:
        print(line(r))
    if not silent:
        print("  (none — every in-domain flow is characterised)")


if __name__ == "__main__":
    main()
