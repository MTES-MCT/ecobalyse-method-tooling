# brightway_vs_volca

Diagonal parity clouds comparing **ecobalyse-Brightway** and **VoLCA** on the BAFU database,
same base and same method (EF 3.1 adapted). x = ecobalyse-Brightway, y = each VoLCA collection;
**on the diagonal = the two engines agree**. It answers "does our Brightway import reproduce
VoLCA?" and, by adding the 1.05 collection, exposes the categories 1.03 under-covers (Water Use).

Both sides read the **same** SimaPro CSV (`BAFU2026v1.CSV.zip`), so a divergence is the engine or
the method coverage, not the input.

## Directory structure

```
brightway_vs_volca/
├── compare.py          # the tool (self-contained)
├── pyproject.toml      # uv project (pyvolca, requests, bw2data, bw2calc, matplotlib, dotenv)
├── .env.example        # copy to .env and adjust paths
└── README.md
```

## Setup

Copy `.env.example` to `.env`. The two sides:

- **ecobalyse (x)** — `BRIGHTWAY2_DIR`, `EB_PROJECT`, `EB_DATABASE` (`BAFU 2026v1`), `EB_METHOD`.
- **VoLCA (y)** — the tool **starts VoLCA itself**: `VOLCA_PORT` (dedicated, e.g. 8091), `VOLCA_DB`
  (`bafu-2026v1`), `BAFU_DB_PATH` (the same CSV zip ecobalyse imports — uploaded to the engine on
  the first run, reused afterwards), `VOLCA_CONFIG_BASE` (a config providing the EF method
  collections + curated flow synonyms, e.g. `examples/volca-bafu.toml`), optional `VOLCA_BINARY`
  (else `volca.download()` fetches one), and `VOLCA_COLLECTIONS`.

`VOLCA_COLLECTIONS` is the list of VoLCA method collections plotted as y-series. Default is the
1.03 collection (same method as ecobalyse). Add the 1.05 collection to expose the Water Use gap:

```
VOLCA_COLLECTIONS=Environmental Footprint 3.1 (adapted 1.03),Environmental Footprint 3.1 (adapted 1.05)
```

## Usage

```bash
uv run compare.py --report etat.html          # full matched panel
uv run compare.py --sample 500 --report etat.html   # quick sample
uv run compare.py --exclude-long-term --report etat.html   # both sides drop long-term
```

`--exclude-long-term` asks VoLCA to drop long-term emissions too, matching ecobalyse's `noLT`
strategy, so `Eutrophication freshwater` and `Ionising radiation` become comparable (otherwise
VoLCA counts long-term groundwater phosphate / long-term radon that ecobalyse zeroes). The bulk
endpoint accepts the flag directly, so this runs at full speed on the whole matched panel.
Requires the VoLCA engine **≥ 0.9.1** (and `pyvolca ≥ 0.7.2`, wire 2): on 0.9.0 the bulk
endpoint silently ignores `exclude-long-term`, so those categories would not collapse.

First run starts the engine and parses BAFU + the EF methods (a few minutes; later runs hit the
cache). It writes a CSV of rows (`product, category, ecobalyse, v0, ...`) and a self-contained HTML
report:

1. **heatmap** — median `|VoLCA/ecobalyse − 1|` per indicator × collection (⚠ over 10 %).
2. **nuages diagonaux** — one log-log scatter per EF category, x = ecobalyse, y = each collection.
3. **biggest-gap tables** — per collection, the (product, indicator) pairs that diverge most.

## Reading the result

- 1.03 vs ecobalyse-1.03 **on the diagonal** = engine + import agree.
- A category that leaves the diagonal is the target: use `bafu/flow-characterization/` and a
  per-flow contribution comparison to attribute the cause (coverage hole, name/compartment
  mismatch, unit, or a legitimate engine/import difference), then close coverage holes with
  synonyms/mapping in the ecobalyse repo.
- Electricity (kWh vs MJ) is reconciled by the unit factor, not a real divergence.

## Findings (BAFU 2025v2, EF 3.1 adapted 1.03 both sides, 2026-07)

Global median VoLCA/ecobalyse = **1.0000**, ~73 % of points within ±5 % (81 % once long-term
emissions are aligned). Every diverging category was attributed:

| Divergence | Cause | Status |
|---|---|---|
| Eutrophication freshwater ×4.9, Ionising radiation ×3.8 | ecobalyse `noLT` zeroes long-term CFs (Phosphate groundwater long-term, Radon-222); VoLCA keeps them | intended adaptation; both collapse to ~1.00 with `--exclude-long-term` |
| Resource use fossils ×1.65 on nuclear | ecobalyse `uraniumFRU` (−40 % on uranium fossil CF) | intended (artificial) adaptation |
| Ecotoxicity / Human toxicity inorganics ~0.91 | VoLCA groundwater CF resolution (below) | VoLCA defect, nothing to fix in ecobalyse |
| Resource use minerals ×27 000 on antimony / stibnite | ecoinvent draws antimony via the resource flow `Stibnite, in ground`; EF characterises only the element `Antimony` (CF 1). VoLCA's resolver bridges Stibnite→Antimony, ecobalyse's import does not (silent zero) | ecobalyse coverage hole, left as-is (out of textile/food scope). VoLCA closer to truth; the proper fix is a content-adjusted CF (~0.72), **not** a plain synonym — see below |
| Land use ×18–396 on offshore oil/gas wells | offshore extraction occupies/transforms **seabed**; EF 3.1 Land use is a terrestrial soil-quality (LANCA) index with no seabed CF. VoLCA folds seabed onto land occupation/transformation CFs | VoLCA over-count — seabed is not land; ecobalyse (0) matches the reference method (below) |

### Armes égales (both sides run `noLT` + `uraniumFRU`, 2026-07-10)

Re-ran the full matched panel (11,238 products) with both adaptations aligned on the VoLCA
side too — `--exclude-long-term` (now supported on the bulk endpoint, see below) plus a
`Environmental Footprint 3.1 (adapted 1.03, ecobalyse)` collection carrying the uranium
adjustment. Global median stays **1.0000**; every category the table above calls "intended
adaptation" collapses to ~1.00, leaving only the already-tracked groundwater residual.

VoLCA's side of `uraniumFRU` no longer needs a hand-patched CSV (a 24 MB derived file, previously
regenerated by a throwaway script): the engine now supports declarative, idempotent method
patches — a `[[methods.patches]]` block in the method's TOML config that rescales or overrides
matched characterization factors at load time (see `volca/examples/volca-bafu.toml` in
volca-deploy for the uranium patch). `noLT` needs no patch at all — VoLCA's existing
`exclude-long-term` switch does the same thing at query time, now also available on the bulk
`/impacts` POST endpoint (previously per-activity only, which is why this harness's
`--exclude-long-term` mode was slow).

### The VoLCA groundwater issue

The inorganics gap is essentially one flow, `Iron, ion` emitted to **water/groundwater
(non long-term)**. Inventory amounts and CFs are identical on both sides where both characterize;
the divergence is CF resolution for the groundwater subcompartment. The 1.03 method CSV has no
explicit `Water;groundwater` line for that flow (only `(unspecified)` = 2108.5 and
`groundwater, long-term` = 0), because the 1.03 changelog deliberately removed subcompartment
lines equal to unspecified: they "will automatically be characterised with the same CF as the
unspecified subcompartment" (SimaPro fallback semantics). Brightway applies that fallback;
VoLCA applies it for `river` and `lake` but **not** for `groundwater` (the flow simply vanishes,
and its score is identical with and without `exclude-long-term`), most likely because its
compartment mapping folds `groundwater` onto `groundwater, long-term` (explicit CF 0), shadowing
the fallback. Any groundwater emission whose method only defines the unspecified CF is lost by
VoLCA; the small residual `Chloride` gap has the same signature. To be reported upstream to VoLCA.

### Antimony/stibnite and offshore seabed (2026-07-13)

The two largest surviving off-diagonal groups share one mechanism and split on which engine is
right. Both come from **VoLCA resolving a flow by name/compartment where ecobalyse-Brightway
matches the method CF by exact biosphere node id**: a flow the reference `Environmental Footprint
3.1 (adapted).1.03.CSV` never characterises becomes a VoLCA score and an ecobalyse silent zero.

- **Resource use, minerals and metals — antimony/stibnite (×27 000, VoLCA right).** ecoinvent
  models antimony extraction with the resource flow `Stibnite, in ground` (1.256 kg for
  `Antimony, at refinery`; 0.7 kg for the ore). The method carries a single antimony resource CF,
  `Raw;(unspecified);Antimony = 1 kg Sb eq/kg`, on the **element** `Antimony`; there is no
  `Stibnite` line. VoLCA bridges `Stibnite → Antimony` and scores 1.256 kg Sb eq (= the stibnite
  mass); ecobalyse never mapped it, so the antimony resource counts as zero (the element flow that
  does carry the CF has amount 3·10⁻¹⁴, i.e. unused). This is an ecobalyse **coverage hole**, left
  as-is: antimony refining is out of textile/food scope, so the panel-wide effect is nil. Note if
  it is ever closed: the naive fix — a `Stibnite, in ground → Antimony` synonym in
  `strategies/simapro_biosphere/synonyms` — inherits CF 1 and over-counts by stibnite's sulphur
  fraction (~40 %), exactly like VoLCA. EF's own convention for ore flows folds the element content
  into the CF (e.g. `Chromium, 25.5 % in chromite … = 0.000443`, `Lead, 5.0 % in sulfide … =
  0.00634`), so the correct value is a content-adjusted CF ≈ 0.72 kg Sb eq/kg, not a 1:1 synonym.

- **Land use — offshore oil/gas wells (×18–396, VoLCA over-counts).** Offshore extraction
  occupies and transforms **seabed** (`Occupation, seabed, drilling and mining` ≈ 260 m²·yr and the
  matching `Transformation, …, seabed`). EF 3.1 Land use is a **terrestrial soil-quality index**
  (LANCA); the reference method has no CF for any seabed — nor river/lake — flow, on purpose:
  seabed is not soil. VoLCA folds the seabed flows onto terrestrial occupation/transformation CFs
  (~4700 Pt/unit), producing ≈1.19·10⁶ Pt for the offshore well; ecobalyse correctly scores them
  zero. Nothing to fix in ecobalyse; to be reported to VoLCA alongside the groundwater issue.

Neither touches ecobalyse's real scope (antimony refining and offshore drilling are not in
textile/food supply chains at meaningful weight); they are engine-level method-coverage
boundaries, not `compare.py` defects. Attributed with a per-product characterised-inventory dump
(the `Antimony`/`Well … offshore` flows above), the counterpart to the database-wide
`bafu/flow-characterization/diagnose_flows.py`.

## Provenance

The VoLCA plumbing (activities, bulk `impacts` POST, unit factors, scatter/report layout) is
adapted from `/home/dadafkas/projets/VoLCA/volca-deploy/volca/examples/bafu_oracle_compare.py`
(which compares VoLCA against the official BAFU oracle spreadsheet).
