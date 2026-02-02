#!/usr/bin/env python3
"""
Export predicted ingredients to CSV and activities.json format.

Usage:
    python export.py metadata --variant FR     # Export FR variant
    python export.py metadata --variant ORG    # Export organic variant
    python export.py final_data                # Generate final CSV with impacts
    python export.py metadata --variant FR --add-old-suffix     # Add (2025) suffix to existing
    python export.py metadata --variant FR --remove-old-suffix  # Remove (2025) suffix

Variants: FR, ORG, UE, DEF, NUE

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
import uuid
from enum import Enum
from pathlib import Path

# Namespace UUID for deterministic UUID generation (generated once, never changes)
ECOBALYSE_NAMESPACE = uuid.UUID("a4e1d123-5c67-4b89-9def-1234567890ab")


class Variant(Enum):
    FR = "FR"
    ORG = "ORG"
    UE = "UE"
    DEF = "DEF"
    NUE = "NUE"


VARIANT_SUFFIX = {
    Variant.FR: " FR",
    Variant.ORG: " Bio",
    Variant.UE: " UE",
    Variant.DEF: " par défaut",
    Variant.NUE: " HORS UE",
}

VARIANT_SCENARIO = {
    Variant.FR: "reference",
    Variant.ORG: "organic",
    Variant.UE: "import",
    Variant.DEF: "import",
    Variant.NUE: "import",
}

from dotenv import load_dotenv

load_dotenv()

import bw2data
import pandas as pd
from rich.progress import track

from predict import Predictor

bw2data.projects.set_current("ecobalyse")

# =============================================================================
# ANIMAL DETECTION
# =============================================================================

ANIMAL_PATTERNS = {
    "cattle": {
        "patterns": [r"\b(beef|boeuf|veau|veal|cattle|bovine|cow)\b"],
        "group2": "cow",
        "product_default": "meat",
    },
    "pig": {
        "patterns": [r"\b(pork|porc|pig|swine|ham|jambon|bacon|saucisse|sausage)\b"],
        "group2": "pig",
        "product_default": "meat",
    },
    "poultry": {
        "patterns": [
            r"\b(chicken|poulet|turkey|dinde|duck|canard|poultry|volaille|hen|poule)\b"
        ],
        "group2": "chicken",
        "product_default": "meat",
    },
    "sheep": {
        "patterns": [r"\b(lamb|agneau|sheep|mouton|mutton)\b"],
        "group2": "sheep",
        "product_default": "meat",
    },
}

ANIMAL_PRODUCT_PATTERNS = {
    "egg": r"\b(egg|oeuf|œuf)\b",
    "milk": r"\b(milk|lait|dairy|cheese|fromage|yogurt|yaourt|cream|crème|butter|beurre)\b",
    "meat": r"\b(meat|viande|flesh|chair)\b",
}


def detect_animal_fields(name: str, activity_name: str) -> dict:
    """Detect animalGroup1, animalGroup2, animalProduct from ingredient name."""
    text = f"{name} {activity_name}".lower()

    animal_group1 = None
    animal_group2 = None
    product_default = "meat"

    for group1, config in ANIMAL_PATTERNS.items():
        for pattern in config["patterns"]:
            if re.search(pattern, text, re.IGNORECASE):
                animal_group1 = group1
                animal_group2 = config["group2"]
                product_default = config["product_default"]
                break
        if animal_group1:
            break

    if not animal_group1:
        return {}

    animal_product = product_default
    for product, pattern in ANIMAL_PRODUCT_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            animal_product = product
            break

    return {
        "animalGroup1": animal_group1,
        "animalGroup2": animal_group2,
        "animalProduct": animal_product,
    }


# =============================================================================
# HELPERS
# =============================================================================


def generate_alias(name: str) -> str:
    """Generate alias from English name."""
    alias = name.lower()
    alias = re.sub(r"[\s_]+", "-", alias)
    alias = re.sub(r"[^a-z0-9-]", "", alias)
    alias = re.sub(r"-+", "-", alias)
    return alias.strip("-")


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


def get_db_unit(activity_name):
    dbs = ("Agribalyse 3.2", "Ecoinvent 3.9.1", "Ecoinvent 3.11", "WFLDB")
    for db in dbs:
        if (
            len(
                activities := [
                    a for a in bw2data.Database(db) if a["name"] == activity_name
                ]
            )
            >= 1
        ):
            return activities[0]["unit"], db
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
            str(row["Liste 4.2 Trad"]).strip()
            if pd.notna(row.get("Liste 4.2 Trad"))
            else ""
        )
        activity_name = (
            str(row["icv final"]).strip() if pd.notna(row["icv final"]) else ""
        )
        unit, source = get_db_unit(activity_name)

        if not name or not activity_name:
            continue

        # Extract production location for FR variant handling
        production_fr = str(row.get("Production_FR", "")).strip()

        ingredient = {"name": name, "activityName": activity_name}
        predictions = predictor.predict(ingredient)

        results.append({
            "name": name,
            "french_name": french_name,
            "activity_name": activity_name,
            "source": source,
            "unit": fix_unit(unit),
            "predictions": predictions,
            "variant": variant,
            "production_fr": production_fr,
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
        "defaultOrigin",
        "defaultOriginMatch",
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
                "defaultOrigin": pred.get("defaultOrigin", ""),
                "defaultOriginMatch": _format_match(pred.get("defaultOriginMatch")),
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
) -> dict:
    """Build an activity entry in the activities.json format."""
    # Determine suffix based on variant and production location
    if variant == Variant.FR and production_fr == "DOM":
        variant_suffix = " FR Outre-Mer"
        alias_suffix = "-fr-overseas"
    else:
        variant_suffix = VARIANT_SUFFIX[variant]
        alias_suffix = "-" + variant.value.lower()

    # Alias from English name + variant suffix (lowercase)
    alias = generate_alias(name) + alias_suffix

    # DisplayName from French name + variant suffix
    display_name = (french_name if french_name else name) + variant_suffix

    # Generate deterministic UUIDs based on displayName
    # This ensures each unique displayName gets a unique UUID
    activity_id = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"activity:{display_name}"))
    ingredient_id = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"ingredient:{display_name}"))

    # Scenario from variant
    scenario = VARIANT_SCENARIO[variant]

    ingredient = {
        "alias": alias,
        "defaultOrigin": predictions.get("defaultOrigin", "OutOfEuropeAndMaghreb"),
        "displayName": display_name,
        "id": ingredient_id,
        "inediblePart": predictions.get("inediblePart", 0),
        "inediblePartMatch": predictions.get("inediblePartMatch"),
        "ingredientCategories": predictions.get("categories", ["misc"]),
        "ingredientDensity": predictions.get("density", 1.0),
        "ingredientDensityMatch": predictions.get("densityMatch"),
        "rawToCookedRatio": predictions.get("rawToCookedRatio", 1.0),
        "rawToCookedRatioMatch": predictions.get("rawToCookedRatioMatch"),
        "scenario": scenario,
        "transportCooling": predictions.get("transportCooling", "none"),
        "transportCoolingMatch": predictions.get("transportCoolingMatch"),
        "visible": True,
    }

    if predictions.get("cropGroup"):
        ingredient["cropGroup"] = predictions["cropGroup"]
        ingredient["cropGroupMatch"] = predictions.get("cropGroupMatch")

    animal_fields = detect_animal_fields(name, activity_name)
    if animal_fields:
        ingredient["animalGroup1"] = animal_fields["animalGroup1"]
        ingredient["animalGroup2"] = animal_fields["animalGroup2"]
        ingredient["animalProduct"] = animal_fields["animalProduct"]

    return {
        "activityName": activity_name,
        "alias": alias,
        "categories": ["ingredient"],
        "displayName": display_name,
        "id": activity_id,
        "metadata": {"food": [ingredient]},
        "scopes": ["food"],
        "source": source,
        "unit": unit,
    }


def write_json(results: list, output_path: str):
    """Write activities to JSON file."""
    activities = []
    for r in results:
        activity = build_activity_entry(
            r["name"],
            r["french_name"],
            r["activity_name"],
            r["source"],
            r["unit"],
            r["predictions"],
            r["variant"],
            r.get("production_fr", ""),
        )
        activities.append(activity)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(activities, f, indent=2, ensure_ascii=False)

    print(f"JSON written to {output_path}")


# =============================================================================
# CLI
# =============================================================================

INPUT_CSV = Path(__file__).parent / "source/new_ingredient_FR.csv"
OUTPUT_CSV = Path(__file__).parent / "generated/predictions.csv"
OUTPUT_JSON = Path(__file__).parent / "generated/new_activities.json"
FINAL_OUTPUT_CSV = Path(__file__).parent / "generated/new_ingredients.csv"

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

OLD_DISPLAY_SUFFIX = " (2025)"
NEW_ALIAS_PREFIX = "new-"


def normalize_display_name(name: str, old_suffix: str) -> str:
    return name[: -len(old_suffix)] if name.endswith(old_suffix) else name


def normalize_alias(alias: str | None, new_prefix: str) -> str | None:
    if alias is None:
        return None
    return alias[len(new_prefix) :] if alias.startswith(new_prefix) else alias


def extract_activities_and_ingredients(
    activities_list: list[dict],
    old_suffix: str,
    new_prefix: str,
) -> tuple[dict[str, dict], dict[str, dict], list[dict]]:
    """Extract and normalize flat dicts from nested activities.json.
    Keyed by displayName. Consolidates by activityName. Last wins.
    """
    activities, ingredients, other = {}, {}, []
    by_activity_name = {}  # activityName -> activity displayName

    for a in activities_list:
        if "displayName" not in a:
            other.append(a)
            continue
        act_name = a.get("activityName")
        act_display = a["displayName"]

        if act_name and act_name in by_activity_name:
            act_display = by_activity_name[act_name]
        else:
            if act_name:
                by_activity_name[act_name] = act_display
            activities[act_display] = {k: v for k, v in a.items() if k != "metadata"}
            activities[act_display]["alias"] = normalize_alias(
                a.get("alias", ""), new_prefix
            )

        for ing in a.get("metadata", {}).get("food", []):
            ing = {**ing, "activity_display": act_display}
            ing["displayName"] = normalize_display_name(ing["displayName"], old_suffix)
            ing["alias"] = normalize_alias(ing.get("alias", ""), new_prefix)
            ingredients[ing["displayName"]] = ing

    return activities, ingredients, other


def apply_suffixes(
    activities: dict[str, dict],
    ingredients: dict[str, dict],
    new_act_names: set[str],
    new_ing_names: set[str],
    keep_set: set[str],
    old_suffix: str,
    new_prefix: str,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Add old_suffix to old ingredient displayNames, new_prefix to new aliases."""
    new_acts = {
        dn: {**act, "alias": new_prefix + act["alias"]}
        if dn in new_act_names and act.get("alias")
        else act
        for dn, act in activities.items()
    }
    new_ings = {}
    for dn, ing in ingredients.items():
        ing = {**ing}
        if dn in new_ing_names:
            if dn not in keep_set and ing.get("alias"):
                ing["alias"] = new_prefix + ing["alias"]
        elif dn not in keep_set:
            ing["displayName"] += old_suffix
        new_ings[dn] = ing
    return new_acts, new_ings


def reassemble(
    activities: dict[str, dict],
    ingredients: dict[str, dict],
    other: list[dict],
) -> list[dict]:
    """Reassemble flat dicts back into nested activities.json format."""
    by_activity = {}
    for ing in ingredients.values():
        ad = ing["activity_display"]
        by_activity.setdefault(ad, []).append(
            {k: v for k, v in ing.items() if k != "activity_display"}
        )

    result = []
    for dn, act in activities.items():
        entry = {**act}
        ings = by_activity.get(dn, [])
        if ings:
            entry["metadata"] = {"food": ings}
        result.append(entry)
    return result + other


def merge_activities(
    new_activities_path: Path,
    target_activities_path: Path,
    add_old_suffix: bool = False,
    remove_old_suffix: bool = False,
):
    """Merge new_activities.json into target activities.json.

    Uses flat dicts keyed by UUID for activities and ingredients.
    Normalizes on load (strips previous merge artifacts), then merges
    with new overriding existing, and optionally applies suffixes.

    Options:
    - add_old_suffix: Add " (2025)" suffix to pre-existing ingredients
    - remove_old_suffix: Remove " (2025)" suffix (normalization already does this)
    """
    with open(new_activities_path) as f:
        new_list = json.load(f)
    with open(target_activities_path) as f:
        existing_list = json.load(f)

    keep_csv_path = Path(__file__).parent / "source/keep.csv"
    keep_set = set()
    if keep_csv_path.exists():
        with open(keep_csv_path, encoding="utf-8") as f:
            keep_set = {line.strip() for line in f if line.strip()}

    old_suffix = OLD_DISPLAY_SUFFIX
    new_prefix = NEW_ALIAS_PREFIX

    # Extract into flat dicts (normalizing on load)
    existing_acts, existing_ings, other = extract_activities_and_ingredients(
        existing_list, old_suffix, new_prefix
    )
    new_acts, new_ings, _ = extract_activities_and_ingredients(
        new_list, old_suffix, new_prefix
    )

    # Build activityName -> displayName map from existing
    existing_act_by_name = {
        act["activityName"]: dn
        for dn, act in existing_acts.items()
        if "activityName" in act
    }

    # For new activities: only add if activityName is genuinely new.
    # For new ingredients mapping to existing activityNames: remap activity_display.
    added_acts = {}
    for dn, act in new_acts.items():
        act_name = act.get("activityName")
        if act_name not in existing_act_by_name:
            added_acts[dn] = act
            existing_act_by_name[act_name] = dn

    # Remap new ingredients to existing activity displayNames where applicable
    for ing_dn, ing in new_ings.items():
        act_display = ing["activity_display"]
        # Find the activityName for this ingredient's activity
        source_act = new_acts.get(act_display)
        if source_act:
            act_name = source_act["activityName"]
            existing_dn = existing_act_by_name.get(act_name)
            if existing_dn and existing_dn != act_display:
                ing["activity_display"] = existing_dn

    merged_acts = {**existing_acts, **added_acts}
    # Existing ingredients win on displayName collision to avoid cross-activity overwrites
    merged_ings = {**new_ings, **existing_ings}

    # Apply suffix logic
    if add_old_suffix:
        merged_acts, merged_ings = apply_suffixes(
            merged_acts,
            merged_ings,
            set(added_acts),
            set(new_ings),
            keep_set,
            old_suffix,
            new_prefix,
        )

    result = reassemble(merged_acts, merged_ings, other)

    with open(target_activities_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"Merged {len(added_acts)} new activities into {len(merged_acts)} total activities")


def generate_final_data():
    """Generate final CSV with all ingredient data and impacts.

    Combines:
    - source/new_ingredient_FR.csv (base data)
    - new_activities.json (predicted metadata)
    - processes_impacts.json (environmental impacts, matched by activityName)
    """
    # Load source CSV
    print(f"Loading {INPUT_CSV}...")
    source_df = pd.read_csv(INPUT_CSV)

    ECOBALYSE_DATA = Path(os.environ["ECOBALYSE_DATA"])
    ECOBALYSE = Path(os.environ["ECOBALYSE"])

    # Load processes_impacts.json - key by activityName for direct matching
    processes_path = ECOBALYSE_DATA / "public/data/processes_impacts.json"
    print(f"Loading {processes_path}...")
    with open(processes_path) as f:
        processes_list = json.load(f)
    processes_by_name = {p["activityName"]: p for p in processes_list}

    # Load new_activities.json to get predicted metadata
    print(f"Loading {OUTPUT_JSON}...")
    with open(OUTPUT_JSON) as f:
        new_activities = json.load(f)
    # Map activityName to full activity (which contains metadata)
    activities_by_name = {a["activityName"]: a for a in new_activities}

    # Load ingredients.json to get ecosystemicServices
    ingredients_path = ECOBALYSE / "public/data/food/ingredients.json"
    print(f"Loading {ingredients_path}...")
    with open(ingredients_path) as f:
        ingredients_list = json.load(f)
    ingredients_by_name = {i["activityName"]: i for i in ingredients_list}

    print(
        f"\nLoaded: {len(new_activities)} activities, {len(processes_by_name)} processes, {len(ingredients_list)} ingredients"
    )

    # Process each row
    print(f"Processing {len(source_df)} ingredients...")
    results = []
    matched_processes = 0

    for _, row in source_df.iterrows():
        result = dict(row)  # Copy all source columns
        activity_name = row["icv final"]

        # Get predicted metadata from new_activities.json
        activity = activities_by_name.get(activity_name)
        if activity:
            food_meta = activity.get("metadata", {}).get("food", [{}])[0]
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

        # Get ecosystemicServices from ingredients.json
        ingredient = ingredients_by_name.get(activity_name)
        if ingredient:
            es = ingredient.get("ecosystemicServices", {}) or {}
            result["cropDiversity"] = es.get("cropDiversity") or 0
            result["hedges"] = es.get("hedges") or 0
            result["livestockDensity"] = es.get("livestockDensity") or 0
            result["permanentPasture"] = es.get("permanentPasture") or 0
            result["plotSize"] = es.get("plotSize") or 0
        else:
            result["cropDiversity"] = ""
            result["hedges"] = ""
            result["livestockDensity"] = ""
            result["permanentPasture"] = ""
            result["plotSize"] = ""

        results.append(result)

    # Write output
    output_df = pd.DataFrame(results)
    output_df.to_csv(FINAL_OUTPUT_CSV, index=False)
    print(f"\nMatched: {matched_processes}/{len(results)} processes with impacts")
    print(f"Final data written to {FINAL_OUTPUT_CSV}")


def main():
    parser = argparse.ArgumentParser(
        description="Export predicted ingredients to CSV and JSON"
    )
    parser.add_argument(
        "command",
        choices=["metadata", "final_data"],
        help="metadata: export predictions + merge activities. final_data: generate final CSV with impacts",
    )
    parser.add_argument(
        "--variant",
        type=lambda v: Variant[v.upper()],
        choices=list(Variant),
        metavar="{FR,ORG,UE,DEF,NUE}",
        help="Variant: FR, ORG, UE, DEF, NUE (required for metadata command)",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear translation cache before running",
    )
    parser.add_argument(
        "--add-old-suffix",
        action="store_true",
        help="Add '(2025)' suffix to pre-existing ingredients",
    )
    parser.add_argument(
        "--remove-old-suffix",
        action="store_true",
        help="Remove '(2025)' suffix from all ingredients",
    )
    args = parser.parse_args()

    # Validate mutually exclusive options
    if args.add_old_suffix and args.remove_old_suffix:
        parser.error("--add-old-suffix and --remove-old-suffix are mutually exclusive")

    # Validate --variant is required for metadata command
    if args.command == "metadata" and args.variant is None:
        parser.error("--variant is required for the metadata command")

    if args.command == "final_data":
        generate_final_data()
        return

    if args.clear_cache:
        Predictor.clear_translation_cache()
        print("Translation cache cleared")

    ECOBALYSE_DATA = Path(os.environ["ECOBALYSE_DATA"])
    ECOBALYSE = Path(os.environ["ECOBALYSE"])

    # Load training data
    ingredients_path = ECOBALYSE / "public/data/food/ingredients.json"
    print(f"Loading training data from {ingredients_path}...")
    with open(ingredients_path) as f:
        training_data = json.load(f)

    # Train predictor
    print(f"\nTraining on {len(training_data)} ingredients...")
    predictor = Predictor()
    predictor.fit(training_data)

    # Load input CSV
    print(f"\nLoading {INPUT_CSV}...")
    df = pd.read_csv(INPUT_CSV)

    if "item" not in df.columns or "icv final" not in df.columns:
        raise ValueError("CSV must have 'item' and 'icv final' columns")

    # Predict for all ingredients
    print(f"\nProcessing {len(df)} ingredients...")
    results = predict_all(predictor, df, args.variant)

    # Write outputs
    print(f"\nWriting {len(results)} results...")
    write_csv(results, OUTPUT_CSV)
    write_json(results, OUTPUT_JSON)

    # Merge into activities.json
    activities_path = ECOBALYSE_DATA / "activities.json"
    if activities_path.exists():
        merge_activities(
            OUTPUT_JSON,
            activities_path,
            args.add_old_suffix,
            args.remove_old_suffix,
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
        print(f"\nWarning: ACTIVITIES path does not exist: {activities_path}")

    print("\nDone!")


if __name__ == "__main__":
    main()
