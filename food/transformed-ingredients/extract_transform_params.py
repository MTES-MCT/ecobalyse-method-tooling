"""Extract transformation parameters from Agribalyse processes via VoLCA.

Reverse-engineers the parameters needed by Ecobalyse's "composant" module
(yield, electricity, heat, allocation, water, co-products, biowaste) for a
small set of reference processes by composing /aggregate queries through
``volca.agribalyse.decompose``.

Usage:
    uv run python extract_transform_params.py
    uv run python extract_transform_params.py --output transform_params.csv
"""

import argparse
import csv
from dataclasses import asdict, dataclass

from volca import Client
from volca.agribalyse import Decomposition, decompose


VOLCA_URL = "http://localhost:8080"
DB = "agribalyse-3.2"

# (label, variant, process_id) — reference processes spanning the 3 patterns:
#   Pattern A wrapper_wfldb : Beurre, Lait en poudre
#   Pattern B direct        : Jus de pomme NFC
#   Pattern C layered       : Tomate pelée
REFERENCE_PROCESSES: list[tuple[str, str, str]] = [
    (
        "Beurre 82%",
        "FR",
        "7c235fbd-e757-5987-a6ea-28bf873dda83_54652585-3ae3-5810-80b0-9e00a093d0c2",
    ),
    (
        "Jus de pomme NFC",
        "FR",
        "1a4c2770-5794-5f31-b269-4604930b2143_9c67e6cc-8f24-5f44-9243-d64c2494a8b3",
    ),
    (
        "Tomate pelée",
        "FR",
        "b81fb01c-7808-567b-99d0-b1487e69dc3e_19b7531c-1bd7-521b-8118-89f9c848446f",
    ),
    (
        "Lait en poudre entier",
        "FR",
        "145e1331-113c-5686-9d0f-2ff332f37dad_cc42bcb0-d2ef-5533-9d13-9453c3371d3b",
    ),
]


@dataclass
class Row:
    """One line in the output CSV — matches the spreadsheet schema."""

    ingredient: str
    variante: str
    pattern: str
    icv_matiere_premiere: str
    rdmt_mat_kg_per_kg: float
    perte_pct: float
    elec_kwh: float
    chaleur_mj: float
    allocation_pct: float
    eau_ajoutee_kg: float
    biowaste_pct: float
    dummy_op: bool
    is_byproduct: bool
    coproduits: str
    process_id: str


def main_product_allocation_pct(d: Decomposition) -> float:
    """Pick the allocation factor that applies to the product we are extracting.

    The product name appears in the activity name; we match it against the
    parsed allocation factors. When no allocation block is found, the process
    is assumed mono-product (100%).
    """
    if d.allocation is None or not d.allocation.factors:
        return 100.0
    raw_name_l = (d.raw_material_name or "").lower()
    # Try to find an allocation key that doesn't appear in the raw material
    # name — the raw material is the input, so the output product is whichever
    # key is *not* the raw material. For butter the raw is "Cow milk" and the
    # output is "butter": "butter" doesn't appear in the raw name.
    for key, value in d.allocation.factors.items():
        if key not in raw_name_l:
            return round(value * 100, 2)
    return round(next(iter(d.allocation.factors.values())) * 100, 2)


def to_row(label: str, variant: str, pid: str, d: Decomposition) -> Row:
    output_kg = 1.0  # all reference processes have a 1 kg reference product
    raw_kg = d.raw_material_kg or 0.0
    biowaste_pct = (d.biowaste_kg / output_kg) * 100
    # Loss = mass that doesn't make it into the output (raw input minus
    # what's left after the process). Falls back to biowaste share when the
    # raw material count alone over-attributes loss (e.g. butter has 6.58 kg
    # milk in / 1 kg butter out, but that gap is allocation, not loss).
    if d.allocation:
        loss_pct = biowaste_pct
    else:
        loss_pct = max(0.0, (raw_kg - output_kg) / raw_kg * 100) if raw_kg else 0.0
    return Row(
        ingredient=label,
        variante=variant,
        pattern=d.pattern,
        icv_matiere_premiere=d.raw_material_name or "?",
        rdmt_mat_kg_per_kg=round(raw_kg, 4),
        perte_pct=round(loss_pct, 2),
        elec_kwh=round(d.electricity_kwh, 4),
        chaleur_mj=round(d.heat_mj, 4),
        allocation_pct=main_product_allocation_pct(d),
        eau_ajoutee_kg=round(d.tap_water_kg, 4),
        biowaste_pct=round(biowaste_pct, 2),
        dummy_op=d.dummy_op,
        is_byproduct=d.is_byproduct,
        coproduits="; ".join(f"{n} ({q:.3g} {u})" for n, q, u in d.co_products),
        process_id=pid,
    )


def print_table(rows: list[Row]) -> None:
    """Render rows to stdout as a fixed-width table."""
    headers = list(asdict(rows[0]).keys())
    widths = {
        h: max(len(h), max(len(str(getattr(r, h))) for r in rows)) for h in headers
    }
    sep = " | "
    print(sep.join(h.ljust(widths[h]) for h in headers))
    print("-+-".join("-" * widths[h] for h in headers))
    for r in rows:
        print(sep.join(str(getattr(r, h)).ljust(widths[h]) for h in headers))


def write_csv(rows: list[Row], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--volca-url", default=VOLCA_URL)
    parser.add_argument("--db", default=DB)
    parser.add_argument("--output", default="transform_params.csv")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process every activity matched by the 'transformed' preset instead of the 4 reference PIDs.",
    )
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    client = Client(base_url=args.volca_url, db=args.db)

    if args.all:
        acts = client.search_activities(preset="transformed", limit=args.limit)
        targets = [(a.name, a.location, a.process_id) for a in acts]
        print(f"Found {len(targets)} activities via preset=transformed")
    else:
        targets = REFERENCE_PROCESSES

    rows: list[Row] = []
    for label, variant, pid in targets:
        print(f"-> {label} ({variant}) ...")
        try:
            d = decompose(client, pid)
        except Exception as e:
            print(f"   skipped: {e}")
            continue
        rows.append(to_row(label, variant, pid, d))

    print()
    print_table(rows)
    print()
    write_csv(rows, args.output)
    print(f"Wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
