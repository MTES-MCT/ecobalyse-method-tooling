#!/usr/bin/env python3
"""
Export predicted ingredients to CSV and activities.json format.

Usage:
    python export.py metadata --variant FR     # Export FR variant
    python export.py metadata --variant ORG    # Export organic variant
    python export.py final_data                # Generate final CSV with impacts
    python export.py metadata --variant FR --add-old-suffix     # Add (2025) suffix to existing
    python export.py remove-old                                # Remove suffixed entries entirely

Variants: FR, ORG, UE, OI, NUE

Outputs:
    - generated/predictions.csv: CSV with all predictions and confidence scores
    - generated/new_activities.json: Activities format for Ecobalyse
"""

import argparse
import csv
import json
import os
import re
import shutil
import unicodedata
import uuid
from collections import Counter
from enum import Enum
from pathlib import Path
from urllib.request import urlopen

import inflect

# Catalog read/write/merge primitives live in a side-effect-free module so
# they can be imported by tools that don't want bw2data/pandas/dotenv.
from lci_catalog import (
    ECOBALYSE_NAMESPACE,
    OLD_ALIAS_SUFFIX,
    OLD_DISPLAY_SUFFIX,
    apply_suffixes,
    dedupe_suffixed_ids,
    extract_activities_and_ingredients,
    extract_geo,
    load_lci_catalog,
    merge_activities,
    normalize_alias,
    normalize_display_name,
    reassemble,
    slugify,
    write_lci_catalog,
)

_inflect_engine = inflect.engine()

PROCESSING_QUALIFIERS = {
    "dried", "fresh", "raw", "frozen", "smoked", "canned", "cooked", "uncooked",
    "roasted", "ground", "blanched", "peeled", "shelled", "dehusked", "pickled",
    "fermented", "salted", "dehydrated", "pasteurized", "refined", "whole",
    "concentrated",
}

PLURAL_EXCEPTIONS = {"oats", "french-fries"}


class Variant(Enum):
    FR = "FR"
    BIO = "BIO"
    UE = "UE"
    OI = "OI"
    NUE = "NUE"


VARIANT_SUFFIX = {
    Variant.FR: " FR",
    Variant.BIO: " Bio",
    Variant.UE: " UE",
    Variant.OI: " Origine Inconnue",
    Variant.NUE: " HORS UE",
}

VARIANT_ALIAS_SUFFIX = {
    Variant.FR: "-fr",
    Variant.BIO: "-organic",
    Variant.UE: "-eu",
    Variant.OI: "-default",
    Variant.NUE: "-non-eu",
}

VARIANT_SCENARIO = {
    Variant.FR: "reference",
    Variant.BIO: "organic",
    Variant.UE: "import",
    Variant.OI: "import",
    Variant.NUE: "import",
}

VARIANT_ORIGIN = {
    Variant.FR: "France",
    Variant.BIO: "France",
    Variant.UE: "EuropeAndMaghreb",
    Variant.OI: "OutOfEuropeAndMaghreb",
    Variant.NUE: "OutOfEuropeAndMaghreb",
}

from dotenv import load_dotenv

load_dotenv()

import bw2data
import pandas as pd
from rich.progress import track

from predict import Predictor

bw2data.projects.set_current("ecobalyse")

# =============================================================================
# ANIMAL DETECTION (from reference/animal.csv)
# =============================================================================

REFERENCE_DIR = Path(__file__).parent / "reference"


def _load_animal_data() -> list[dict]:
    """Load reference/animal.csv into a list of dicts with a compiled regex."""
    rows = []
    with open(REFERENCE_DIR / "animal.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({
                "pattern": re.compile(r"\b" + re.escape(row["name"]) + r"\b", re.IGNORECASE),
                "animalGroup1": row["animalGroup1"],
                "animalGroup2": row["animalGroup2"],
                "animalProduct": row["animalProduct"],
            })
    return rows


ANIMAL_ENTRIES = _load_animal_data()


def detect_animal_fields(name: str, activity_name: str) -> dict:
    """Detect animalGroup1, animalGroup2, animalProduct from reference CSV."""
    text = f"{name} {activity_name}"
    for entry in ANIMAL_ENTRIES:
        if entry["pattern"].search(text):
            return {
                "animalGroup1": entry["animalGroup1"],
                "animalGroup2": entry["animalGroup2"],
                "animalProduct": entry["animalProduct"],
            }
    return {}


# =============================================================================
# HELPERS
# =============================================================================


def generate_alias(name: str) -> str:
    """Generate alias from English name.

    Singularizes base words, moves processing qualifiers to the end.
    """
    alias = name.lower()
    alias = re.sub(r"[\s_]+", "-", alias)
    alias = re.sub(r"[^a-z0-9-]", "", alias)
    alias = re.sub(r"-+", "-", alias)
    alias = alias.strip("-")

    if alias in PLURAL_EXCEPTIONS:
        return alias

    words = alias.split("-")

    # Separate processing qualifiers from base words (order-preserving)
    base_words = []
    qualifier_words = []
    for w in words:
        if w in PROCESSING_QUALIFIERS:
            qualifier_words.append(w)
        else:
            base_words.append(w)

    # Singularize base words
    singularized = []
    for w in base_words:
        singular = _inflect_engine.singular_noun(w)
        singularized.append(singular if singular else w)

    return "-".join(singularized + qualifier_words)


def simplify_for_alias(name: str) -> str:
    """Drop clarifying alternative-lists from an ingredient name before alias generation.

    Two patterns are dropped:
    - parenthesized clarifications: ``Bell pepper (green, yellow, or red)`` → ``Bell pepper``
    - trailing alternative tails containing `` or ``:
      ``Sea salt, celery salt, or fleur de sel`` → ``Sea salt``
    - if a parenthetical was already stripped, the leading comma-tail is also
      dropped (it's redundant with the dropped paren):
      ``Raw rice, all varieties (white, wild, ...)`` → ``Raw rice``

    Real qualifiers (no parens, no `` or `` in the tail) are kept:
    ``Almonds, in shell``, ``Squash, raw, with skin`` are returned as-is.
    """
    stripped, n_strip = re.subn(r"\s*\([^)]*\)", "", name)
    stripped = stripped.strip()
    if "," in stripped:
        head, tail = stripped.split(",", 1)
        if " or " in tail or n_strip > 0:
            return head.strip()
    return stripped


# LCA-process boilerplate phrases that don't help identify the activity.
# Order matters in alternation: longer phrases first.
_LCA_NOISE_RE = re.compile(
    r",\s*(?:at\s+farm\s+gate|at\s+farm|national\s+average|conventional)",
    re.IGNORECASE,
)

# Year-version markers (e.g. `organic 2025`) used in BIO LCI names. Strip the
# year to avoid colliding with the legacy `-2025` alias suffix that already
# tags pre-existing ingredients in activities.json.
_YEAR_MARKER_RE = re.compile(r"\s+20\d{2}\b")


def generate_activity_alias(activity_name: str) -> str:
    """Generate alias from a Brightway/Agribalyse activityName.

    Keeps discriminating qualifiers (`, in shell`, `, non-basmati`, `, dried`,
    …) but drops process-LCA boilerplate (`, at farm`, `{geo}`, `| production
    …`, ` U`, `- Adapted from …`, ` 20XX` year markers).
    """
    head = re.split(r"[{(|]", activity_name, maxsplit=1)[0]
    head = _LCA_NOISE_RE.sub("", head)
    head = _YEAR_MARKER_RE.sub("", head)
    head = re.sub(r"\s+U\s*$", "", head).strip().strip(",").strip()
    return generate_alias(head)


_MARKER_RE = re.compile(r"\s*\{\{[^}]*\}\}\s*")


def clean_activity_name(activity_name: str) -> str:
    """Strip `{{archive-alias-…}}` annotation markers used in the source CSV.

    These are human-only hints (no code consumes them) but they leak into the
    predictor's text matching and trigger false positives (e.g. `{{bacon-…}}`
    matching the `\\bbacon\\b` cured-meat rule on a `Live pig` activity).
    """
    return _MARKER_RE.sub(" ", activity_name).strip()


def _format_match(match_info: dict | None) -> str:
    """Format match rule for CSV output."""
    if match_info is None:
        return ""
    return match_info.get("rule", "")


def _format_conf(match_info: dict | None) -> str:
    """Format confidence from match info for CSV output."""
    if match_info is None:
        return ""
    conf = match_info.get("confidence")
    return f"{conf:.3f}" if conf else ""


def get_db_unit(activity_name, location=""):
    """Return (unit, db_name, location_or_empty).

    The location is only returned when it was needed to disambiguate
    multiple activities with the same name (e.g. WFLDB).
    """
    dbs = ("Agribalyse 3.2", "Ecoinvent 3.9.1", "Ecoinvent 3.11", "WFLDB", "Ecobalyse", "Ginko 2025")
    for db in dbs:
        activities = [a for a in bw2data.Database(db) if a["name"] == activity_name]
        if len(activities) == 1:
            return activities[0]["unit"], db, ""
        if len(activities) > 1 and location:
            filtered = [a for a in activities if a.get("location") == location]
            if len(filtered) >= 1:
                return filtered[0]["unit"], db, location
    raise Exception(f"Not found in {str(dbs)}: {activity_name}")


def fix_unit(unit):
    return {"kilogram": "kg", "unit": "item", "litre": "L"}[unit]


# =============================================================================
# PREDICTION
# =============================================================================


def predict_all(predictor: Predictor, input_df: pd.DataFrame, variant: Variant) -> list:
    """
    Predict metadata for all ingredients in the DataFrame.

    Returns list of dicts with: name, french_name, activity_name, source, predictions, variant
    """
    results = []

    for _, row in track(
        input_df.iterrows(), total=len(input_df), description="Predicting..."
    ):
        name = str(row["item"]).strip()
        french_name = (
            str(row["item trad"]).strip()
            if pd.notna(row.get("item trad"))
            else ""
        )
        activity_name = (
            clean_activity_name(str(row["icv final"])) if pd.notna(row["icv final"]) else ""
        )
        if not name or not activity_name:
            continue

        csv_location = str(row.get("location", "")).strip() if pd.notna(row.get("location")) else ""
        unit, source, location = get_db_unit(activity_name, csv_location)

        # Extract production location for FR variant handling
        production_fr = str(row.get("production fr", "")).strip()
        antilles = str(row.get("antilles", "")).strip().upper() == "TRUE"

        ingredient = {"name": name, "activityName": activity_name}
        predictions = predictor.predict(ingredient)

        visible = str(row.get("visible", "TRUE")).strip().upper() == "TRUE"

        results.append({
            "name": name,
            "french_name": french_name,
            "activity_name": activity_name,
            "source": source,
            "unit": fix_unit(unit),
            "predictions": predictions,
            "variant": variant,
            "production_fr": production_fr,
            "antilles": antilles,
            "location": location,
            "visible": visible,
        })

    return results


# =============================================================================
# CSV OUTPUT
# =============================================================================


def write_csv(results: list, output_path: str):
    """Write predictions to CSV file."""
    fieldnames = [
        "name",
        "categories",
        "foodType",
        "foodTypeMatch",
        "foodTypeConf",
        "novaGroup",
        "novaGroupMatch",
        "novaGroupConf",
        "processingState",
        "processingStateMatch",
        "processingStateConf",
        "packaging",
        "packagingMatch",
        "transportCooling",
        "transportCoolingMatch",
        "cropGroup",
        "cropGroupMatch",
        "cropGroupConf",
        "density",
        "densityMatch",
        "densityConf",
        "inediblePart",
        "inediblePartMatch",
        "inediblePartConf",
        "rawToCookedRatio",
        "rawToCookedRatioMatch",
        "rawToCookedRatioConf",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for r in results:
            pred = r["predictions"]
            categories = pred.get("categories", [])

            writer.writerow({
                "name": r["name"],
                "categories": ",".join(categories) if categories else "",
                "foodType": pred.get("foodType", ""),
                "foodTypeMatch": _format_match(pred.get("foodTypeMatch")),
                "foodTypeConf": _format_conf(pred.get("foodTypeMatch")),
                "novaGroup": pred.get("novaGroup", ""),
                "novaGroupMatch": _format_match(pred.get("novaGroupMatch")),
                "novaGroupConf": _format_conf(pred.get("novaGroupMatch")),
                "processingState": pred.get("processingState", ""),
                "processingStateMatch": _format_match(pred.get("processingStateMatch")),
                "processingStateConf": _format_conf(pred.get("processingStateMatch")),
                "packaging": pred.get("packaging") or "",
                "packagingMatch": _format_match(pred.get("packagingMatch")),
                "transportCooling": pred.get("transportCooling", ""),
                "transportCoolingMatch": _format_match(
                    pred.get("transportCoolingMatch")
                ),
                "cropGroup": pred.get("cropGroup") or "",
                "cropGroupMatch": _format_match(pred.get("cropGroupMatch")),
                "cropGroupConf": _format_conf(pred.get("cropGroupMatch")),
                "density": f"{pred.get('density', 0):.3f}",
                "densityMatch": _format_match(pred.get("densityMatch")),
                "densityConf": _format_conf(pred.get("densityMatch")),
                "inediblePart": f"{pred.get('inediblePart', 0):.2f}",
                "inediblePartMatch": _format_match(pred.get("inediblePartMatch")),
                "inediblePartConf": _format_conf(pred.get("inediblePartMatch")),
                "rawToCookedRatio": f"{pred.get('rawToCookedRatio', 0):.3f}",
                "rawToCookedRatioMatch": _format_match(
                    pred.get("rawToCookedRatioMatch")
                ),
                "rawToCookedRatioConf": _format_conf(pred.get("rawToCookedRatioMatch")),
            })

    print(f"CSV written to {output_path}")


# =============================================================================
# JSON OUTPUT (activities.json format)
# =============================================================================


def build_activity_entry(
    name: str,
    french_name: str,
    activity_name: str,
    source: str,
    unit: str,
    predictions: dict,
    variant: Variant,
    production_fr: str = "",
    location: str = "",
    visible: bool = True,
    antilles: bool = False,
    geo_disambiguate: bool = False,
) -> dict:
    """Build an activity entry in the activities.json format.

    Activity-level alias is derived from `activity_name` (LCI process identity);
    ingredient-level alias is derived from `name` (Ecobalyse ingredient identity).
    They differ when several ingredients reuse the same upstream activity as a
    proxy (e.g., `fig-eu` ingredient hosted on `peach` activity).

    When `geo_disambiguate` is True, the geo code from `{…}` in `activity_name`
    is inserted before the variant suffix on the activity alias, to break ties
    between distinct activityNames that collapse to the same alias.
    """
    # Determine suffix based on variant and production location
    if variant == Variant.FR and production_fr == "DOM":
        variant_suffix = " FR Outre-Mer"
        alias_suffix = "-fr-overseas"
    elif variant == Variant.UE and antilles:
        variant_suffix = " UE Antilles"
        alias_suffix = "-eu-antilles"
    else:
        variant_suffix = VARIANT_SUFFIX[variant]
        alias_suffix = VARIANT_ALIAS_SUFFIX[variant]

    # Activity alias from LCI activityName only (no variant suffix: an activity
    # can be shared across variants — same `Apple {IT}` LCI feeds the FR, UE
    # and OI variants of the apple ingredient). Geo is only appended when two
    # distinct activityNames collide on the base alias.
    activity_alias = generate_activity_alias(activity_name)
    if geo_disambiguate:
        geo = extract_geo(activity_name)
        if geo:
            activity_alias = f"{activity_alias}-{geo}"

    # Ingredient alias from Ecobalyse ingredient name + variant suffix.
    # simplify_for_alias drops parenthesized clarifications and trailing
    # alternative-lists (e.g. "Bell pepper (green, yellow, or red)" → "Bell
    # pepper") so newly added ingredients get a short, stable alias. The full
    # `item` text remains the displayName/source-of-truth.
    ingredient_alias = generate_alias(simplify_for_alias(name)) + alias_suffix

    # Ingredient displayName from French name + variant suffix
    ingredient_display_name = (french_name if french_name else name) + variant_suffix

    # Activity displayName: variant-neutral, derived from the LCI activityName.
    # Take the leading product segment (before `{`/`(`/`|`) and append the geo
    # code from `{…}` to keep the displayName unique across activities sharing
    # a product name (e.g. `Broccoli (GLO)` vs `Broccoli (CH)`).
    activity_display_base = re.split(r"[{(|]", activity_name, maxsplit=1)[0].strip().rstrip(",").strip()
    geo_code = extract_geo(activity_name)
    activity_display_name = (
        f"{activity_display_base} ({geo_code.upper()})"
        if geo_code
        else activity_display_base
    )

    # Activity UUID is keyed by alias (= activityName-derived) to keep stable
    # identity across variants/exports; two activities sharing a displayName
    # but pointing to different LCI processes get distinct UUIDs.
    # Ingredient UUID stays keyed by displayName (its Ecobalyse identity).
    activity_id = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"activity:{activity_alias}"))
    ingredient_id = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"ingredient:{ingredient_display_name}"))

    # Scenario from variant
    scenario = VARIANT_SCENARIO[variant]

    ingredient = {
        "alias": ingredient_alias,
        "defaultOrigin": VARIANT_ORIGIN[variant],
        "displayName": ingredient_display_name,
        "id": ingredient_id,
        "inediblePart": predictions.get("inediblePart", 0),
        "inediblePartMatch": predictions.get("inediblePartMatch"),
        "ingredientCategories": predictions.get("categories", ["misc"])
            + (["organic"] if variant == Variant.BIO else []),
        "ingredientDensity": predictions.get("density", 1.0),
        "ingredientDensityMatch": predictions.get("densityMatch"),
        "rawToCookedRatio": predictions.get("rawToCookedRatio", 1.0),
        "rawToCookedRatioMatch": predictions.get("rawToCookedRatioMatch"),
        "scenario": scenario,
        "transportCooling": predictions.get("transportCooling", "none"),
        "transportCoolingMatch": predictions.get("transportCoolingMatch"),
        "visible": visible,
    }

    if predictions.get("cropGroup"):
        ingredient["cropGroup"] = predictions["cropGroup"]
        ingredient["cropGroupMatch"] = predictions.get("cropGroupMatch")

    animal_fields = detect_animal_fields(name, activity_name)
    if animal_fields:
        ingredient["animalGroup1"] = animal_fields["animalGroup1"]
        ingredient["animalGroup2"] = animal_fields["animalGroup2"]
        ingredient["animalProduct"] = animal_fields["animalProduct"]

    entry = {
        "activityName": activity_name,
        "alias": activity_alias,
        "categories": ["ingredient"],
        "displayName": activity_display_name,
        "id": activity_id,
        "metadata": [{**ingredient, "scopes": ["food", "food2"]}],
        "scopes": ["food", "food2"],
        "source": source,
        "unit": unit,
    }
    if location:
        entry["location"] = location
    return entry


def _build_entry_for_row(r: dict, geo_disambiguate: bool = False) -> dict:
    return build_activity_entry(
        r["name"],
        r["french_name"],
        r["activity_name"],
        r["source"],
        r["unit"],
        r["predictions"],
        r["variant"],
        r.get("production_fr", ""),
        r.get("location", ""),
        r.get("visible", True),
        r.get("antilles", False),
        geo_disambiguate=geo_disambiguate,
    )


def write_json(results: list, output_path: str):
    """Write activities to JSON, grouped by activity alias.

    Several rows can share the same upstream `activity_name` (proxy ingredients);
    they merge into a single activity entry with multiple ingredients in
    `metadata`. If two distinct `activity_name` strings collapse to the same
    activity alias within the variant, both fall back to a geo-disambiguated
    alias (e.g. `apple-it-eu` vs `apple-es-eu`).
    """
    # Pass 1: try without geo disambiguation, detect collisions.
    base_entries = [_build_entry_for_row(r) for r in results]
    activity_names_per_alias: dict[str, set[str]] = {}
    for entry in base_entries:
        activity_names_per_alias.setdefault(entry["alias"], set()).add(
            entry["activityName"]
        )
    colliding = {a for a, ans in activity_names_per_alias.items() if len(ans) > 1}
    if colliding:
        print(f"Geo-disambiguating {len(colliding)} colliding alias(es): {sorted(colliding)}")

    # Pass 2: rebuild colliding rows with geo suffix; group all rows by final alias.
    by_alias: dict[str, dict] = {}
    for r, entry in zip(results, base_entries):
        if entry["alias"] in colliding:
            entry = _build_entry_for_row(r, geo_disambiguate=True)
        key = entry["alias"]
        if key in by_alias:
            by_alias[key]["metadata"].extend(entry["metadata"])
        else:
            by_alias[key] = entry

    activities = list(by_alias.values())
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(activities, f, indent=2, ensure_ascii=False)

    print(f"JSON written to {output_path} ({len(activities)} activities, {sum(len(a['metadata']) for a in activities)} ingredients)")


# =============================================================================
# CLI
# =============================================================================

INPUT_CSV_DIR = Path(__file__).parent / "source"
GENERATED_DIR = Path(__file__).parent / "generated"


def get_input_csv(variant: Variant) -> Path:
    return INPUT_CSV_DIR / f"new_ingredient_{variant.value}.csv"


def fetch_source_csv(variant: Variant) -> Path:
    """Download the Google Sheet tab for `variant` and overwrite the local source CSV.

    Reads GSHEET_ID and GSHEET_GID_{VARIANT} from the environment (.env).
    """
    sheet_id = os.environ["GSHEET_ID"]
    gid = os.environ[f"GSHEET_GID_{variant.value}"]
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}"
    )
    target = get_input_csv(variant)
    print(f"Fetching {variant.value} from Google Sheet (gid={gid})...")
    with urlopen(url, timeout=30) as resp:
        data = resp.read().replace(b"\r\n", b"\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(f"  wrote {len(data)} bytes to {target}")
    return target


def load_source_csv(variant: Variant, fetch: bool = True) -> pd.DataFrame:
    if fetch:
        fetch_source_csv(variant)
    df = pd.read_csv(get_input_csv(variant))
    df.columns = df.columns.str.lower().str.replace("_", " ")
    if "under review" in df.columns:
        mask = df["under review"].astype(str).str.strip().str.upper() == "TRUE"
        if mask.any():
            print(f"Forcing visible=FALSE on {int(mask.sum())} rows marked 'under review'")
            df.loc[mask, "visible"] = "FALSE"
    return df


def get_output_paths(variant: Variant) -> tuple[Path, Path, Path]:
    """Generate output paths with variant suffix."""
    suffix = f"_{variant.value}"
    return (
        GENERATED_DIR / f"predictions{suffix}.csv",
        GENERATED_DIR / f"new_activities{suffix}.json",
        GENERATED_DIR / f"fichier_final{suffix}.csv",
    )

# Impact columns to extract from processes_impacts.json
IMPACT_COLUMNS = [
    "acd",
    "cch",
    "etf-c",
    "fru",
    "fwe",
    "htc-c",
    "htn-c",
    "ior",
    "ldu",
    "mru",
    "ozd",
    "pco",
    "pma",
    "swe",
    "tre",
    "wtu",
    "ecs",
]

ECOSYSTEMIC_SERVICES_MULTIPLIERS = {
    "cropDiversity": -1.5,
    "hedges": -3,
    "livestockDensity": 3000,
    "permanentPasture": -7,
    "plotSize": -4,
}


def _build_es_by_base_alias(ingredients: list[dict]) -> dict[str, dict]:
    """Build a base-alias → ecosystemicServices map from ingredients.json.

    ecosystemicServices values are variant-agnostic by design (only `scenario`
    differs across variants of the same ingredient), so we strip the variant
    suffix from each alias and pick the first non-empty ES set per base.
    """
    variant_suffixes = sorted(
        {
            "-fr-overseas",
            "-eu-antilles",
            "-fr",
            "-eu",
            "-organic",
            "-default",
            "-non-eu",
            *VARIANT_ALIAS_SUFFIX.values(),
        },
        key=len,
        reverse=True,
    )

    def strip_all_suffixes(alias: str) -> str:
        # Strip legacy `-2025` first, then any variant suffix (longest match wins).
        if alias.endswith(OLD_ALIAS_SUFFIX):
            alias = alias[: -len(OLD_ALIAS_SUFFIX)]
        for sfx in variant_suffixes:
            if alias.endswith(sfx):
                return alias[: -len(sfx)]
        return alias

    es_by_base: dict[str, dict] = {}
    for ing in ingredients:
        al = ing.get("alias")
        if not al:
            continue
        es = ing.get("ecosystemicServices") or {}
        if not any(v for v in es.values() if v):
            continue
        base = strip_all_suffixes(al)
        es_by_base.setdefault(base, es)
    return es_by_base


def generate_final_data(variant: Variant, fetch: bool = True):
    """Generate final CSV with all ingredient data and impacts.

    Combines:
    - source/new_ingredient_{variant}.csv (base data)
    - new_activities.json (predicted metadata)
    - processes_impacts.json (environmental impacts, matched by activityName)
    - ingredients.json (ecosystemicServices ONLY, looked up by base alias —
      ES is variant-agnostic; the rest of the ingredient is not consulted)
    """
    output_csv, output_json, final_output_csv = get_output_paths(variant)

    # Load source CSV
    input_csv = get_input_csv(variant)
    print(f"Loading {input_csv}...")
    source_df = load_source_csv(variant, fetch=fetch)

    ECOBALYSE_DATA = Path(os.environ["ECOBALYSE_DATA"])
    ECOBALYSE = Path(os.environ["ECOBALYSE"])

    # Load processes_impacts.json - key by activityName for direct matching
    processes_path = ECOBALYSE_DATA / "public/data/processes_impacts.json"
    print(f"Loading {processes_path}...")
    with open(processes_path) as f:
        processes_list = json.load(f)
    processes_by_name = {p["activityName"]: p for p in processes_list}

    # Load new_activities.json to get predicted metadata
    print(f"Loading {output_json}...")
    with open(output_json) as f:
        new_activities = json.load(f)
    # Map alias to full activity (alias is unique; activityName is not)
    activities_by_alias = {a["alias"]: a for a in new_activities}

    # Load ingredients.json — used ONLY for ecosystemicServices lookup.
    ingredients_path = ECOBALYSE / "public/data/food/ingredients.json"
    print(f"Loading {ingredients_path} (ES lookup only)...")
    with open(ingredients_path) as f:
        ingredients_list = json.load(f)
    es_by_base_alias = _build_es_by_base_alias(ingredients_list)

    print(
        f"\nLoaded: {len(new_activities)} activities, {len(processes_by_name)} processes,"
        f" {len(es_by_base_alias)} ecosystemicServices entries (by base alias)"
    )

    # Process each row
    print(f"Processing {len(source_df)} ingredients...")
    results = []
    matched_processes = 0

    for _, row in source_df.iterrows():
        result = dict(row)  # Copy all source columns
        activity_name = clean_activity_name(str(row["icv final"]))

        # Derive alias the same way build_activity_entry does (alias is unique, activityName is not)
        antilles = str(row.get("antilles", "")).strip().upper() == "TRUE"
        if variant == Variant.FR and row.get("production fr") == "DOM":
            alias_suffix = "-fr-overseas"
        elif variant == Variant.UE and antilles:
            alias_suffix = "-eu-antilles"
        else:
            alias_suffix = VARIANT_ALIAS_SUFFIX[variant]
        row_alias = generate_alias(row["item"]) + alias_suffix

        # Get predicted metadata from new_activities.json
        activity = activities_by_alias.get(row_alias)
        if activity:
            food_meta = next((m for m in activity.get("metadata", []) if "food" in m.get("scopes", [])), {})
            result["categories"] = ";".join(food_meta.get("ingredientCategories", []))
            result["transportCooling"] = food_meta.get("transportCooling", "")
            result["cropGroup"] = food_meta.get("cropGroup", "")
            result["defaultOrigin"] = food_meta.get("defaultOrigin", "")
            result["density"] = food_meta.get("ingredientDensity", "")
            result["inediblePart"] = food_meta.get("inediblePart", "")
            result["rawToCookedRatio"] = food_meta.get("rawToCookedRatio", "")
        else:
            result["categories"] = ""
            result["transportCooling"] = ""
            result["cropGroup"] = ""
            result["defaultOrigin"] = ""
            result["density"] = ""
            result["inediblePart"] = ""
            result["rawToCookedRatio"] = ""

        # Get impacts directly from processes_impacts.json by activityName
        process = processes_by_name.get(activity_name)
        if process:
            matched_processes += 1
            impacts = process.get("impacts", {})
            for col in IMPACT_COLUMNS:
                result[col] = impacts.get(col, "")
        else:
            for col in IMPACT_COLUMNS:
                result[col] = ""

        # ecosystemicServices: variant-agnostic — look up from ingredients.json
        # by base alias (= ingredient identity stripped of variant suffix).
        # Apply the multiplier defined in ECOSYSTEMIC_SERVICES_MULTIPLIERS.
        base_alias = generate_alias(row["item"])
        es = es_by_base_alias.get(base_alias) or {}
        for field, multiplier in ECOSYSTEMIC_SERVICES_MULTIPLIERS.items():
            raw = es.get(field) or 0
            result[field] = raw * multiplier if raw else ""

        results.append(result)

    # Write output
    output_df = pd.DataFrame(results)
    output_df.to_csv(final_output_csv, index=False)
    print(f"\nMatched: {matched_processes}/{len(results)} processes with impacts")
    print(f"Final data written to {final_output_csv}")


def remove_old(target_catalog_dir: Path):
    """Remove activities/ingredients whose alias ends with '-2025' or displayName ends with ' (2025)'.

    Also removes feed.json entries whose top-level key ends with '-2025'.
    """
    old_alias_suffix = OLD_ALIAS_SUFFIX
    old_display_suffix = OLD_DISPLAY_SUFFIX

    activities_list = load_lci_catalog(target_catalog_dir)

    filtered = []
    removed_activities = 0
    removed_ingredients = 0
    for activity in activities_list:
        alias = activity.get("alias", "")
        display = activity.get("displayName", "")
        if alias.endswith(old_alias_suffix) or display.endswith(old_display_suffix):
            removed_activities += 1
            continue

        # Filter food ingredients within the activity
        meta_list = activity.get("metadata", [])
        food_ings = [m for m in meta_list if "food" in m.get("scopes", [])]
        if food_ings:
            new_food = []
            for ing in food_ings:
                ing_alias = ing.get("alias", "")
                ing_display = ing.get("displayName", "")
                if ing_alias.endswith(old_alias_suffix) or ing_display.endswith(old_display_suffix):
                    removed_ingredients += 1
                else:
                    new_food.append(ing)
            non_food = [m for m in meta_list if "food" not in m.get("scopes", [])]
            if new_food:
                activity = {**activity, "metadata": non_food + new_food}
            else:
                if non_food:
                    activity = {**activity, "metadata": non_food}
                else:
                    activity = {k: v for k, v in activity.items() if k != "metadata"}

        filtered.append(activity)

    write_lci_catalog(filtered, target_catalog_dir)
    print(f"lci_catalog: removed {removed_activities} activities, {removed_ingredients} ingredients")

    # Load and filter feed.json
    feed_path = target_catalog_dir.parent / "food/ecosystemic_services/feed.json"
    if feed_path.exists():
        with open(feed_path, encoding="utf-8") as f:
            feed_data = json.load(f)

        removed_feed = 0
        filtered_feed = {}
        for key, value in feed_data.items():
            if key.endswith(old_alias_suffix):
                removed_feed += 1
            else:
                filtered_feed[key] = value
        feed_data = filtered_feed

        with open(feed_path, "w", encoding="utf-8") as f:
            json.dump(feed_data, f, indent=2, ensure_ascii=False)
        print(f"feed.json: removed {removed_feed} entries")

    print("Done!")


VARIANT_INDEPENDENT_FIELDS = [
    "foodType",
    "novaGroup",
    "processingState",
    "transportCooling",
    "cropGroup",
    "density",
    "inediblePart",
    "rawToCookedRatio",
    "packaging",
    "categories",
]


def compare_variants(generated_dir: Path):
    """Compare predictions across variant CSVs and warn on mismatches."""
    csv_files = sorted(generated_dir.glob("predictions_*.csv"))
    if len(csv_files) < 2:
        return

    variant_data = {}
    for csv_file in csv_files:
        variant = csv_file.stem.replace("predictions_", "")
        with open(csv_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            variant_data[variant] = {row["name"]: row for row in reader}

    variants = list(variant_data.keys())
    all_names = set()
    for data in variant_data.values():
        all_names.update(data.keys())

    mismatches = 0
    for name in sorted(all_names):
        present = {v: variant_data[v][name] for v in variants if name in variant_data[v]}
        if len(present) < 2:
            continue
        variant_list = list(present.keys())
        for field in VARIANT_INDEPENDENT_FIELDS:
            values = {v: present[v].get(field, "") for v in variant_list}
            unique = set(values.values())
            if len(unique) > 1:
                parts = ", ".join(f"{v}={values[v]}" for v in variant_list)
                print(f"WARNING: '{name}' has different '{field}' across variants: {parts}")
                mismatches += 1

    if mismatches:
        print(f"\n{mismatches} cross-variant mismatches found.")
    else:
        print("\nNo cross-variant mismatches found.")


def main():
    parser = argparse.ArgumentParser(
        description="Export predicted ingredients to CSV and JSON"
    )
    parser.add_argument(
        "command",
        choices=["metadata", "final_data", "remove-old"],
        help="metadata: export predictions + merge activities. final_data: generate final CSV with impacts. remove-old: remove suffixed entries",
    )
    parser.add_argument(
        "--variant",
        type=lambda v: Variant[v.upper()],
        choices=list(Variant),
        metavar="{FR,ORG,UE,OI,NUE}",
        help="Variant: FR, ORG, UE, OI, NUE (required, used in output file names)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear translation cache before running",
    )
    parser.add_argument(
        "--add-old-suffix",
        action="store_true",
        help="Add '(2025)' suffix to pre-existing ingredient displayNames and '-2025' suffix to their aliases",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip Google Sheet fetch, use existing local source/*.csv as-is",
    )
    args = parser.parse_args()

    # Handle remove-old command (no variant needed)
    if args.command == "remove-old":
        ECOBALYSE_DATA = Path(os.environ["ECOBALYSE_DATA"])
        catalog_dir = ECOBALYSE_DATA / "lci_catalog"
        remove_old(catalog_dir)
        return

    # Validate --variant is required for metadata and final_data commands
    if args.variant is None:
        parser.error("--variant is required for metadata and final_data commands")

    # Get output paths for this variant
    output_csv, output_json, final_output_csv = get_output_paths(args.variant)

    if args.command == "final_data":
        generate_final_data(args.variant, fetch=not args.no_fetch)
        return

    if args.clear_cache:
        Predictor.clear_translation_cache()
        print("Translation cache cleared")

    ECOBALYSE_DATA = Path(os.environ["ECOBALYSE_DATA"])

    # Build predictor from reference CSVs (no ingredients.json training corpus)
    predictor = Predictor()
    predictor.fit()

    # Load input CSV
    input_csv = get_input_csv(args.variant)
    print(f"\nLoading {input_csv}...")
    df = load_source_csv(args.variant, fetch=not args.no_fetch)

    if "item" not in df.columns or "icv final" not in df.columns:
        raise ValueError("CSV must have 'item' and 'icv final' columns")

    # Predict for all ingredients
    print(f"\nProcessing {len(df)} ingredients...")
    results = predict_all(predictor, df, args.variant)

    # Write outputs
    print(f"\nWriting {len(results)} results...")
    write_csv(results, output_csv)
    write_json(results, output_json)

    # Compare predictions across variants
    compare_variants(GENERATED_DIR)

    # Merge into lci_catalog
    catalog_dir = ECOBALYSE_DATA / "lci_catalog"
    if catalog_dir.exists():
        merge_activities(
            output_json,
            catalog_dir,
            args.add_old_suffix,
        )

        # Copy reference CSVs to ecobalyse-data
        ref_src = Path(__file__).parent / "reference"
        ref_dst = ECOBALYSE_DATA / "food/metadata"
        ref_dst.mkdir(parents=True, exist_ok=True)
        for csv_file in sorted(ref_src.glob("*.csv")):
            shutil.copy2(csv_file, ref_dst / csv_file.name)
        print(f"Copied reference files to {ref_dst}")

        print(
            "\nNext step: run 'just export-all' in ecobalyse-data to regenerate ingredients.json"
        )
    else:
        print(f"\nWarning: lci_catalog directory does not exist: {catalog_dir}")

    print("\nDone!")


if __name__ == "__main__":
    main()
