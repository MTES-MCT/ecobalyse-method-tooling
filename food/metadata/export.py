#!/usr/bin/env python3
"""
Export predicted ingredients to CSV and activities.json format.

Usage:
    python export.py                    # Export all new ingredients
    python export.py --clear-cache      # Clear translation cache first

Outputs:
    - generated/predictions.csv: CSV with all predictions and confidence scores
    - generated/new_activities.json: Activities format for Ecobalyse
"""

import argparse
import csv
import json
import os
import re
import uuid
from pathlib import Path

# Namespace UUID for deterministic UUID generation (generated once, never changes)
ECOBALYSE_NAMESPACE = uuid.UUID("a4e1d123-5c67-4b89-9def-1234567890ab")

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


def predict_all(predictor: Predictor, input_df: pd.DataFrame) -> list:
    """
    Predict metadata for all ingredients in the DataFrame.

    Returns list of dicts with: name, french_name, activity_name, source, predictions
    """
    results = []

    for _, row in track(
        input_df.iterrows(), total=len(input_df), description="Predicting..."
    ):
        name = str(row["item"]).strip()
        french_name = (
            str(row["nom"]).strip()
            if pd.notna(row.get("nom"))
            else ""
        )
        activity_name = (
            str(row["icv final"]).strip() if pd.notna(row["icv final"]) else ""
        )
        unit, source = get_db_unit(activity_name)

        if not name or not activity_name:
            continue

        ingredient = {"name": name, "activityName": activity_name}
        predictions = predictor.predict(ingredient)

        results.append({
            "name": name,
            "french_name": french_name,
            "activity_name": activity_name,
            "source": source,
            "unit": fix_unit(unit),
            "predictions": predictions,
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
) -> dict:
    """Build an activity entry in the activities.json format."""
    alias = generate_alias(french_name if french_name else name)

    # Generate deterministic UUIDs based on activity_name
    # This ensures the same activity always gets the same UUID
    activity_id = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"activity:{activity_name}"))
    ingredient_id = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"ingredient:{activity_name}"))

    display_name = french_name if french_name else name

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
        "scenario": "reference",
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


def merge_activities(new_activities_path: Path, target_activities_path: Path):
    """Merge new_activities.json into target activities.json.

    Two-level merge with global ingredient uniqueness:
    1. Process level: merge by activityName (preserve existing UUIDs)
    2. Ingredient level: globally unique by displayName
       - If ingredient exists anywhere: preserve UUID, move to new activity
       - If ingredient is new: add with generated UUID

    Ingredients are globally unique: if an ingredient moves from one activity
    to another, it is removed from the old activity and added to the new one.
    """
    with open(new_activities_path) as f:
        new_activities = json.load(f)

    with open(target_activities_path) as f:
        existing_activities = json.load(f)

    # Separate activities with/without activityName (e.g. textile materials)
    existing_by_name = {a["activityName"]: a for a in existing_activities if "activityName" in a}
    other_activities = [a for a in existing_activities if "activityName" not in a]

    # Build global ingredient index: displayName -> {ingredient, activity_name}
    global_ingredients = {}
    for activity in existing_by_name.values():
        for ing in activity.get("metadata", {}).get("food", []):
            global_ingredients[ing["displayName"]] = {
                "ingredient": ing,
                "activity_name": activity["activityName"],
            }

    # Track which ingredients to remove from old activities
    ingredients_to_remove = {}  # activity_name -> set of displayNames to remove

    # Process new activities
    for new_activity in new_activities:
        activity_name = new_activity["activityName"]

        # Preserve activity UUID if it exists
        if activity_name in existing_by_name:
            new_activity["id"] = existing_by_name[activity_name]["id"]

        # Process ingredients
        new_ingredients = new_activity.get("metadata", {}).get("food", [])
        for new_ing in new_ingredients:
            display_name = new_ing["displayName"]

            if display_name in global_ingredients:
                existing_entry = global_ingredients[display_name]
                # Preserve existing ingredient UUID
                new_ing["id"] = existing_entry["ingredient"]["id"]

                # Mark for removal from old activity (if different)
                old_activity_name = existing_entry["activity_name"]
                if old_activity_name != activity_name:
                    if old_activity_name not in ingredients_to_remove:
                        ingredients_to_remove[old_activity_name] = set()
                    ingredients_to_remove[old_activity_name].add(display_name)

            # Update global index to point to new location
            global_ingredients[display_name] = {
                "ingredient": new_ing,
                "activity_name": activity_name,
            }

        # Update activity in index
        existing_by_name[activity_name] = new_activity

    # Remove moved ingredients from old activities
    for activity_name, display_names in ingredients_to_remove.items():
        activity = existing_by_name.get(activity_name)
        if activity and "metadata" in activity and "food" in activity["metadata"]:
            activity["metadata"]["food"] = [
                ing for ing in activity["metadata"]["food"]
                if ing["displayName"] not in display_names
            ]

    merged = list(existing_by_name.values()) + other_activities
    with open(target_activities_path, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    print(f"Merged {len(new_activities)} activities into {target_activities_path}")
    print(f"Total activities: {len(merged)}")


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

    # Load processes_impacts.json - key by activityName for direct matching
    processes_path = Path(os.environ["PROCESSES"])
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
    ingredients_path = Path(os.environ["INGREDIENTS"])
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
        nargs="?",
        default="metadata",
        choices=["metadata", "final_data"],
        help="metadata: export predictions + merge activities. final_data: generate final CSV with impacts",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear translation cache before running",
    )
    args = parser.parse_args()

    if args.command == "final_data":
        generate_final_data()
        return

    if args.clear_cache:
        Predictor.clear_translation_cache()
        print("Translation cache cleared")

    # Load training data
    ingredients_path = Path(os.environ["INGREDIENTS"])
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
    results = predict_all(predictor, df)

    # Write outputs
    print(f"\nWriting {len(results)} results...")
    write_csv(results, OUTPUT_CSV)
    write_json(results, OUTPUT_JSON)

    # Merge into activities.json if configured
    activities_path = os.environ.get("ACTIVITIES")
    if activities_path:
        activities_path = Path(activities_path)
        if activities_path.exists():
            merge_activities(OUTPUT_JSON, activities_path)
            print(
                "\nNext step: run 'just export-all' in ecobalyse-data to regenerate ingredients.json"
            )
        else:
            print(f"\nWarning: ACTIVITIES path does not exist: {activities_path}")

    print("\nDone!")


if __name__ == "__main__":
    main()
