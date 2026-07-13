"""Diagonal parity clouds: ecobalyse-Brightway vs VoLCA on BAFU, EF 3.1.

Both engines compute the same base (the same BAFU SimaPro CSV) with the same method
(EF 3.1 adapted). x = ecobalyse-Brightway (anchor), y = each VoLCA collection. On the
diagonal = the two engines agree. Adding the 1.05 collection as a second series exposes
categories 1.03 under-covers (e.g. Water Use): those points leave the diagonal upward.

Self-contained: starts VoLCA itself (pyvolca download/Server), loads the BAFU CSV + the EF
method collections, scores every matched process in bulk, computes the Brightway side via
bw2calc MultiLCA, and writes a self-contained HTML report (heatmap + one diagonal scatter
per indicator + biggest-gap tables). The VoLCA plumbing (activities, bulk impacts, unit
factors, scatter/report layout) is adapted from
/home/dadafkas/projets/VoLCA/volca-deploy/volca/examples/bafu_oracle_compare.py.

    uv run compare.py --report etat.html [--sample 0] [--out rows.csv]

Config comes from .env (see .env.example): BRIGHTWAY2_DIR, EB_*, VOLCA_* .
"""

import argparse
import csv
import io
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()  # BRIGHTWAY2_DIR must be set before bw2data is imported

import bw2calc  # noqa: E402
import bw2data  # noqa: E402
from bw2data import get_multilca_data_objs  # noqa: E402


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"missing required env var {name} (see .env.example)")
    return v


EB_PROJECT = env("EB_PROJECT", "ecobalyse")
EB_DATABASE = env("EB_DATABASE", "BAFU 2026v1")
EB_METHOD = env("EB_METHOD", "Environmental Footprint 3.1 (adapted)")
VOLCA_PORT = int(env("VOLCA_PORT", "8091"))
VOLCA_DB = env("VOLCA_DB", "bafu-2026v1")
BAFU_DB_PATH = env("BAFU_DB_PATH")
VOLCA_CONFIG_BASE = env("VOLCA_CONFIG_BASE")
VOLCA_BINARY = os.environ.get("VOLCA_BINARY")
COLLECTIONS = [c.strip() for c in env("VOLCA_COLLECTIONS").split(",") if c.strip()]
BASE_URL = f"http://localhost:{VOLCA_PORT}"


# --------------------------------------------------------------- VoLCA engine ---


def build_config(port: int) -> str:
    """Base config (EF methods + curated refdata) + a [[databases]] block for BAFU."""
    base = Path(VOLCA_CONFIG_BASE).read_text()
    block = (
        f'\n[[databases]]\nname = "{VOLCA_DB}"\n'
        f"path = {_toml_str(BAFU_DB_PATH)}\nload = false\n"
    )
    tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    tmp.write(base + block)
    tmp.close()
    return tmp.name


def _toml_str(s: str) -> str:
    import json

    return json.dumps(s)  # valid TOML basic string (quotes + escapes)


def start_engine():
    """Return a started pyvolca Server (spawned on a dedicated port), or connect if alive."""
    from volca import Server

    config = build_config(VOLCA_PORT)
    if VOLCA_BINARY:
        srv = Server(config=config, port=VOLCA_PORT, binary=VOLCA_BINARY)
    else:
        try:
            from volca import download

            inst = download()
            srv = Server(config=config, port=VOLCA_PORT, binary=str(inst.binary))
        except Exception as e:  # no prebuilt binary and download failed
            sys.exit(
                f"cannot obtain a VoLCA binary ({e}). Set VOLCA_BINARY in .env to a "
                "locally built engine, or ensure volca.download() has network access."
            )
    print(f"starting VoLCA on :{VOLCA_PORT} (first run parses BAFU + EF methods) ...", file=sys.stderr)
    srv.start(idle_timeout=1800, wait_timeout=600)
    return srv


def load_activities() -> dict[str, tuple[str, str]]:
    """key -> (processId, productUnit). VoLCA's productName is the same string as the Brightway
    activity name ('core {LOC} U'), so both sides key through bw_key to line up exactly."""
    r = requests.get(f"{BASE_URL}/api/v1/db/{VOLCA_DB}/activities", params={"limit": 20000})
    r.raise_for_status()
    return {
        bw_key(a["productName"]): (a["processId"], a["productUnit"])
        for a in r.json()["results"]
    }


def volca_impacts(
    pids: list[str], collection: str, chunk: int = 300, exclude_long_term: bool = False
) -> dict[str, dict]:
    """processId -> {category: score} for one collection, scored in bulk. `exclude_long_term`
    matches ecobalyse's noLT strategy (VoLCA drops delayed long-term emissions at query time,
    no method patch needed)."""
    out: dict[str, dict] = {}
    url = f"{BASE_URL}/api/v1/db/{VOLCA_DB}/impacts/{quote(collection, safe='')}"
    params = {"exclude-long-term": "true"} if exclude_long_term else {}
    for i in range(0, len(pids), chunk):
        r = requests.post(url, params=params, json={"processIds": pids[i : i + chunk]})
        r.raise_for_status()
        for e in r.json()["results"]:
            out[e["processId"]] = {x["methodName"]: x["score"] for x in e["impacts"]["results"]}
        print(f"  {collection}: {min(i + chunk, len(pids))}/{len(pids)}", file=sys.stderr)
    return out


# ------------------------------------------------------- ecobalyse Brightway ---


def bw_key(name: str) -> str:
    """'1-pentanol, at plant {RER} U' -> '1-pentanol, at plant - RER' (VoLCA activity key)."""
    s = name.strip()
    if s.endswith(" U"):
        s = s[:-2]
    m = re.search(r"\{([^}]+)\}\s*$", s)
    loc = m.group(1).strip() if m else ""
    core = re.sub(r"\s*\{[^}]+\}\s*$", "", s).strip()
    return f"{core} - {loc}"


def demand_sign(act) -> int:
    """+1 / -1 so a waste (negative production) process is still scored per 1 unit produced."""
    pa = act.get("production amount")
    if pa is None:
        for e in act.production():
            pa = e.get("amount")
            break
    if not pa:
        return 1
    return 1 if pa > 0 else -1


def bw_keys() -> set[str]:
    """All BAFU activity keys (cheap: names only), to intersect + sample before MultiLCA."""
    bw2data.projects.set_current(EB_PROJECT)
    return {bw_key(a["name"]) for a in bw2data.Database(EB_DATABASE)}


def ecobalyse_impacts(keys_wanted: set[str], chunk: int = 50):
    """key -> ({category: score}, unit), for the wanted keys only, via MultiLCA."""
    bw2data.projects.set_current(EB_PROJECT)
    categories = [m for m in bw2data.methods if m[0] == EB_METHOD]
    if not categories:
        sys.exit(f"no method '{EB_METHOD}' in project '{EB_PROJECT}'")
    method_config = {"impact_categories": [tuple(m) for m in categories]}

    unit_of = {}
    seen: dict[str, object] = {}
    for a in bw2data.Database(EB_DATABASE):
        k = bw_key(a["name"])
        if k in keys_wanted and k not in seen:  # first activity wins a key
            seen[k] = a
            unit_of[k] = a.get("unit", "")
    id_to_key = {a.id: k for k, a in seen.items()}
    acts = list(seen.values())

    scores: dict[str, dict] = {k: {} for k in seen}
    for i in range(0, len(acts), chunk):
        part = acts[i : i + chunk]
        demands = {str(a.id): {a.id: demand_sign(a)} for a in part}
        data_objs = get_multilca_data_objs(functional_units=demands, method_config=method_config)
        mlca = bw2calc.MultiLCA(demands=demands, method_config=method_config, data_objs=data_objs)
        mlca.lci()
        mlca.lcia()
        for (method_t, fu_name), ci in mlca.characterized_inventories.items():
            scores[id_to_key[int(fu_name)]][method_t[1]] = float(ci.sum())
        print(f"  ecobalyse: {min(i + chunk, len(acts))}/{len(acts)}", file=sys.stderr)
    return scores, unit_of


# ------------------------------------------------------------------- units ---

# canonical unit label; both engines read the same SimaPro CSV, so most units are
# name-only differences (kg == kilogram). Electricity is the real one: ecobalyse
# converts to kWh, VoLCA keeps MJ.
UNIT_CANON = {
    "kilogram": "kg", "kg": "kg",
    "megajoule": "MJ", "MJ": "MJ",
    "joule": "J", "j": "J",
    "kilowatt hour": "kWh", "kWh": "kWh",
    "cubic meter": "m3", "m3": "m3",
    "square meter": "m2", "m2": "m2",
    "ton kilometer": "tkm", "tkm": "tkm",
    "kilometer": "km", "km": "km", "meter": "m", "m": "m",
    "square meter-year": "m2a", "m2a": "m2a", "m2year": "m2a", "m2*a": "m2a",
    "cubic meter-year": "m3a", "m3a": "m3a", "m3year": "m3a", "m3*a": "m3a",
    "person kilometer": "pkm", "pkm": "pkm", "personkm": "pkm",
    "unit": "unit", "p": "unit", "Item(s)": "unit", "dimensionless": "unit",
}
# magnitude of each unit in its dimension's base (energy=J, length=m). Deriving the
# conversion from this makes the inverse pairs impossible to get backwards (the previous
# hand-written table had km<->m inverted, planting a spurious 1e6 second diagonal).
UNIT_BASE = {
    "J": ("energy", 1.0), "MJ": ("energy", 1e6), "kWh": ("energy", 3.6e6),
    "m": ("length", 1.0), "km": ("length", 1e3),
}


def unit_factor(eco_unit: str, volca_unit: str):
    """factor to multiply the ecobalyse value by to express it per 1 VoLCA unit, or None
    if the two units are not comparable."""
    ce = UNIT_CANON.get(eco_unit, eco_unit)
    cv = UNIT_CANON.get(volca_unit, volca_unit)
    if ce == cv:
        return 1.0
    be, bv = UNIT_BASE.get(ce), UNIT_BASE.get(cv)
    if be and bv and be[0] == bv[0]:  # same physical dimension
        return bv[1] / be[1]  # impact per 1 eco-unit -> per 1 volca-unit
    return None


assert unit_factor("kWh", "MJ") == 1 / 3.6  # electricity, the real rescaling
assert unit_factor("kilometer", "meter") == 1e-3  # transport: per km -> per m
assert unit_factor("kg", "megajoule") is None  # cross-dimension = not comparable


# ------------------------------------------------------------------- report ---


def heat_color(v):
    if v is None:
        return "#e5e7eb", "#6b7280"
    t = min(v / 0.30, 1.0)
    if t < 0.5:
        f = t / 0.5
        r, g, b = int(26 + f * 224), int(150 + f * 54), int(80 + f * 12)
    else:
        f = (t - 0.5) / 0.5
        r, g, b = int(250 + f * -35), int(204 + f * -156), int(92 + f * -53)
    fg = "#111827" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#f9fafb"
    return f"#{r:02x}{g:02x}{b:02x}", fg


def fmt(x):
    if x is None:
        return "—"
    if x == 0:
        return "0"
    a = abs(x)
    return f"{x:.3g}" if 1e-3 <= a < 1e6 else f"{x:.2e}"


def fmt_gap(ratio):
    if ratio == 0:
        return "÷∞"
    if 0.1 <= ratio <= 10:
        return f"{ratio - 1:+.0%}"
    return f"×{ratio:.0f}" if ratio > 10 else f"÷{1 / ratio:.0f}"


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


COLORS = ["#3aa0d1", "#111827", "#e0602c", "#8a7fbd", "#1aa64f"]


def scatter_svg(categories, rows_by_cat, series):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(categories)
    ncol = 4
    nrow = math.ceil(n / ncol)
    fig, axes = plt.subplots(nrow, ncol, figsize=(4.2 * ncol, 3.6 * nrow))
    axes = axes.flatten()
    for ax, cat in zip(axes, categories):
        rows = rows_by_cat.get(cat, [])
        allpos = []
        for idx, (label, field) in enumerate(series):
            xs, ys = [], []
            for r in rows:
                x, y = r["ecobalyse"], r[field]
                if x is not None and y is not None and x > 0 and y > 0:
                    xs.append(x)
                    ys.append(y)
            if xs:
                ax.scatter(xs, ys, s=8, alpha=0.45, color=COLORS[idx % len(COLORS)],
                           label=label, edgecolors="none", rasterized=True)
                allpos += xs + ys
        if allpos:
            lo, hi = min(allpos), max(allpos)
            ax.plot([lo, hi], [lo, hi], "--", color="#9ca3af", lw=0.9, zorder=0)
            ax.set_xscale("log")
            ax.set_yscale("log")
        ax.set_title(cat, fontsize=8)
        ax.tick_params(labelsize=6)
        ax.set_xlabel("ecobalyse", fontsize=7)
    for ax in axes[n:]:
        ax.axis("off")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", fontsize=10, markerscale=1.6, ncol=len(series))
    fig.suptitle("Chaque collection VoLCA (y) vs ecobalyse-Brightway (x) — sur la diagonale = accord", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    buf = io.StringIO()
    fig.savefig(buf, format="svg", dpi=120)
    plt.close(fig)
    svg = buf.getvalue()
    return svg[svg.index("<svg") :]


def build_report(rows, path, series, meta):
    categories = sorted({r["category"] for r in rows})
    rows_by_cat = defaultdict(list)
    for r in rows:
        rows_by_cat[r["category"]].append(r)

    # heatmap: median |volca/ecobalyse - 1| per (category, series)
    head = "".join(f"<th>{lbl}</th>" for lbl, _ in series)
    hrows = ""
    for cat in categories:
        rr = rows_by_cat[cat]
        cells = ""
        for _, field in series:
            gaps = [abs(r[field] / r["ecobalyse"] - 1)
                    for r in rr if r[field] is not None and r["ecobalyse"] not in (None, 0)]
            v = median(gaps)
            bg, fg = heat_color(v)
            txt = "—" if v is None else f"{v:.0%}"
            flag = " ⚠" if (v is not None and v > 0.10) else ""
            cells += f'<td style="background:{bg};color:{fg}">{txt}{flag}</td>'
        hrows += f'<tr><td class="ind">{cat}</td><td class="n">{len(rr)}</td>{cells}</tr>'
    heatmap_html = (f'<table class="heat"><thead><tr><th>indicateur</th><th>n</th>{head}</tr>'
                    f"</thead><tbody>{hrows}</tbody></table>")

    scatter_html = scatter_svg(categories, rows_by_cat, series)

    floor = {cat: (median([abs(r["ecobalyse"]) for r in rows_by_cat[cat]
                           if r["ecobalyse"] not in (None, 0)]) or 0) for cat in categories}
    tables_html = ""
    for lbl, field in series:
        entries, dropped = [], 0
        for r in rows:
            ref, oth = r["ecobalyse"], r[field]
            if ref is None or oth is None or ref == 0:
                continue
            if (ref > 0) != (oth > 0) or max(abs(ref), abs(oth)) < floor.get(r["category"], 0):
                dropped += 1
                continue
            ratio = oth / ref
            fold = max(ratio, 1 / ratio) if ratio > 0 else float("inf")
            entries.append((fold, r["product"], r["category"], ref, oth, ratio))
        entries.sort(reverse=True)
        body = "".join(
            f"<tr><td>{p}</td><td>{c}</td><td class='num'>{fmt(ref)}</td>"
            f"<td class='num'>{fmt(o)}</td><td class='num gap'>{fmt_gap(rt)}</td></tr>"
            for _, p, c, ref, o, rt in entries[:40]
        )
        n = len(entries)
        within = sum(1 for e in entries if abs(e[5] - 1) <= 0.10)
        noise = f", {dropped} non comparables écartées" if dropped else ""
        share = (f"{within} à ≤10 % ({within / n:.0%}), top 40 des plus gros écarts{noise}"
                 if n else "aucune paire comparable")
        tables_html += (
            f'<h3>ecobalyse vs VoLCA {lbl} <span class="sub">— {n} paires, {share}</span></h3>'
            '<table class="gaps"><thead><tr><th>produit</th><th>indicateur</th>'
            f"<th>ecobalyse</th><th>VoLCA {lbl}</th><th>écart (base ecobalyse)</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
        )

    html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>{EB_DATABASE} — ecobalyse-Brightway vs VoLCA (EF 3.1)</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#111827;line-height:1.4}}
 h1{{font-size:22px}} h2{{font-size:17px;margin-top:34px;border-bottom:2px solid #e5e7eb;padding-bottom:4px}}
 h3{{font-size:14px;margin-top:22px}} .sub{{font-weight:400;color:#6b7280;font-size:12px}}
 .lead{{color:#374151;max-width:60em}}
 table{{border-collapse:collapse;font-size:12px}} td,th{{padding:3px 7px;border:1px solid #e5e7eb}}
 .heat td,.heat th{{text-align:center}} .heat .ind{{text-align:left}} .heat .n{{color:#9ca3af}}
 .gaps td.num{{text-align:right;font-variant-numeric:tabular-nums}} .gaps td.gap{{font-weight:600}}
 .gaps tbody tr:nth-child(even){{background:#f9fafb}}
 svg{{max-width:100%;height:auto}} .meta{{color:#6b7280;font-size:12px}}
</style></head><body>
<h1>{EB_DATABASE} — ecobalyse-Brightway vs VoLCA (EF 3.1)</h1>
<p class="lead">Même base (le même export SimaPro CSV de BAFU) et même méthode calculées par deux
moteurs indépendants. x = ecobalyse-Brightway, y = chaque collection VoLCA. Sur la diagonale = accord.
Une catégorie qui diverge pointe un écart moteur/couverture à investiguer (cf. Water Use en 1.03).</p>
<p class="meta">{meta}</p>
<h2>1 · Synthèse — médiane de |VoLCA/ecobalyse − 1| par indicateur × collection</h2>
<p class="lead">Vert = accord, rouge = écart, ⚠ marque &gt; 10 %.</p>
{heatmap_html}
<h2>2 · Nuages diagonaux — un par indicateur</h2>
{scatter_html}
<h2>3 · Plus gros écarts — par collection</h2>
<p class="lead">Une ligne par (produit, indicateur), triée par écart décroissant, base ecobalyse.
Ne sont classées que les paires de même signe et dont le plus grand côté atteint la magnitude
médiane de l'indicateur (sinon flux en trace).</p>
{tables_html}
</body></html>"""
    Path(path).write_text(html)
    return path


# --------------------------------------------------------------------- main ---


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="products to score (0 = full matched panel)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="brightway_vs_volca.csv")
    ap.add_argument("--report", default="etat_des_lieux.html")
    ap.add_argument(
        "--exclude-long-term",
        action="store_true",
        help="ask VoLCA to drop long-term emissions too, matching ecobalyse's noLT strategy "
        "(bulk endpoint, no slowdown). Makes fwe/IR comparable.",
    )
    args = ap.parse_args()

    srv = start_engine()
    try:
        from volca import Client

        Client(base_url=srv.base_url, db=VOLCA_DB).load_database(VOLCA_DB)
        acts = load_activities()
        print(f"VoLCA activities: {len(acts)}", file=sys.stderr)

        matched = sorted(set(acts) & bw_keys())
        print(f"matched ecobalyse ∩ VoLCA: {len(matched)}", file=sys.stderr)
        if args.sample and 0 < args.sample < len(matched):
            import random

            random.seed(args.seed)
            matched = sorted(random.sample(matched, args.sample))

        eb_scores, eb_unit = ecobalyse_impacts(set(matched))  # heavy compute, matched only

        # unit factor per matched key (ecobalyse -> per volca unit); skip + report unknowns
        factor, skipped = {}, []
        for k in matched:
            f = unit_factor(eb_unit.get(k, ""), acts[k][1])
            if f is None:
                skipped.append((k, eb_unit.get(k), acts[k][1]))
            else:
                factor[k] = f
        for k, eu, vu in skipped[:20]:
            print(f"[skip] {k}: no unit factor {eu!r} -> {vu!r}", file=sys.stderr)
        if skipped:
            print(f"{len(skipped)} keys skipped for unknown unit conversion", file=sys.stderr)
        matched = [k for k in matched if k in factor]

        pids = sorted({acts[k][0] for k in matched})
        series = [(c.split("adapted ")[-1].rstrip(")") or c, f"v{i}") for i, c in enumerate(COLLECTIONS)]
        volca = {}
        for (label, field), coll in zip(series, COLLECTIONS):
            by_pid = volca_impacts(pids, coll, exclude_long_term=args.exclude_long_term)
            volca[field] = {k: by_pid.get(acts[k][0], {}) for k in matched}
    finally:
        srv.stop()

    # rows: one per (product, category); ecobalyse anchor (x) + each VoLCA collection (y)
    rows = []
    for k in matched:
        for cat, eco in eb_scores[k].items():
            eco = eco * factor[k]
            if eco is None or abs(eco) < 1e-12:
                continue
            row = {"product": k, "category": cat, "ecobalyse": eco}
            for _, field in series:
                row[field] = volca[field][k].get(cat)
            rows.append(row)

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product", "category", "ecobalyse", *(fld for _, fld in series)])
        w.writeheader()
        w.writerows(rows)

    meta = f"{len(matched)} produits × {len(set(r['category'] for r in rows))} indicateurs · panel ecobalyse ∩ {VOLCA_DB}"
    out = build_report(rows, args.report, series, meta)
    print(f"\nrows: {len(rows)} -> {args.out}\nrapport: {out}")


if __name__ == "__main__":
    main()
