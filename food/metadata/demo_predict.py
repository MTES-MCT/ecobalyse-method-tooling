#!/usr/bin/env python3
"""
Démonstration du prédicteur de métadonnées.

Usage:
    python demo_predict.py                    # Test sur tous les ingrédients CSV
    python demo_predict.py "Tomate cerise"    # Test unitaire sur un ingrédient
"""

import argparse
import json
from pathlib import Path

import pandas
from predict import Predictor
from rich.console import Console
from rich.progress import track
from rich.table import Table

# Données d'entraînement (échantillon)
TRAINING_DATA = []
with open("../ingredients.json") as data:
    TRAINING_DATA = json.load(data)


def main():
    parser = argparse.ArgumentParser(description="Démonstration du prédicteur")
    parser.add_argument(
        "ingredient", nargs="?", help="Nom d'ingrédient à tester (optionnel)"
    )
    parser.add_argument(
        "--activity", "-a", default="", help="Nom du procédé ACV (optionnel)"
    )
    parser.add_argument(
        "--clear-cache", action="store_true", help="Effacer le cache de traduction"
    )
    args = parser.parse_args()

    if args.clear_cache:
        Predictor.clear_translation_cache()
        # Also remove saved model (embeddings changed)
        model_path = Path("/tmp/demo_predictor.pkl")
        if model_path.exists():
            model_path.unlink()
            print("Model cache cleared")
        # Don't return - continue to run the demo

    # 1. Entraînement
    print("\n📚 Entraînement sur", len(TRAINING_DATA), "ingrédients...")
    predictor = Predictor()
    predictor.fit(TRAINING_DATA)

    # 2. Mode test ou batch
    if args.ingredient:
        # Test sur un seul ingrédient
        print(f"\n🔮 Test : {args.ingredient}")
        ing = {"name": args.ingredient, "activityName": args.activity}
        predictions, confidence = predictor.predict_with_confidence(ing)

        print(f"\n{'─' * 50}")
        print(f"🥗 {ing['name']}")
        if args.activity:
            print(f"   Process: {args.activity}")
        print("\n   Prédictions:")
        for key, value in predictions.items():
            if key in confidence:
                conf = confidence[key]
                print(f"   • {key}: {value} ({conf:.0%})")
            else:
                print(f"   • {key}: {value}")
        print("=" * 60)
        return

    # 3. Évaluation
    print("\n📈 Évaluation en cross-validation:")
    predictor.evaluate()

    # 4. Tests de prédiction batch
    test_ingredients = (
        pandas.read_csv("../icv_high_impact_final.csv")[["item", "icv_final"]]
        .rename(columns={"item": "name", "icv_final": "activityName"})
        .dropna()
        .to_dict("records")
    )

    print("\n" + "=" * 60)
    print("🔮 PRÉDICTIONS POUR NOUVEAUX INGRÉDIENTS")
    print("=" * 60)

    console = Console()

    # Create table with columns for each metadata + match
    # Set max_width=0 to hide columns (easy to re-enable by changing to a positive value)
    # Color coding: cyan=name, yellow=categories, green=transport, magenta=cropGroup,
    #               blue=density, red=inedible, white=ratio
    table = Table(title="Predictions", show_header=True, header_style="bold")
    table.add_column("Name", style="cyan", no_wrap=True, max_width=25)
    # Categories group (yellow)
    table.add_column("categories", style="yellow", no_wrap=True)
    table.add_column("match", style="yellow", no_wrap=True, max_width=18)
    table.add_column("conf", style="yellow", justify="right")
    # Transport group (green)
    table.add_column("pkg", style="green", no_wrap=True, max_width=0)
    table.add_column("transport", style="green", no_wrap=True)
    # CropGroup group (magenta)
    table.add_column("cropGroup", style="magenta", no_wrap=True, max_width=20)
    table.add_column("conf", style="magenta", justify="right", max_width=4)
    # Density group (blue)
    table.add_column("density", style="blue", justify="right", max_width=5)
    table.add_column("match", style="blue", no_wrap=True, max_width=20)
    table.add_column("conf", style="blue", justify="right", max_width=4)
    # Inedible group (red)
    table.add_column("inedible", style="red", justify="right", max_width=5)
    table.add_column("match", style="red", no_wrap=True, max_width=20)
    table.add_column("conf", style="red", justify="right", max_width=4)
    # Ratio group (white/default)
    table.add_column("c/raw", justify="right", max_width=5)
    table.add_column("match", no_wrap=True, max_width=20)
    table.add_column("conf", justify="right", max_width=4)

    # Collect predictions first with progress bar
    all_predictions = []
    for ing in track(test_ingredients, description="Predicting..."):
        predictions, confidence = predictor.predict_with_confidence(ing)
        all_predictions.append((ing, predictions, confidence))

    # Build table from collected predictions
    for ing, predictions, confidence in all_predictions:
        # Format values
        categories = predictions.get("categories", [])
        categories_str = ", ".join(categories) if categories else "-"
        food_type_match = (predictions.get("foodTypeMatch") or "-")[:18]
        packaging = predictions.get("packaging") or "-"
        transport = predictions.get("transportCooling", "-")
        crop = predictions.get("cropGroup") or "-"
        density = f"{predictions.get('density', 0):.2f}"
        density_match = (predictions.get("densityMatch") or "-")[:15]
        inedible = f"{predictions.get('inediblePart', 0):.2f}"
        inedible_match = (predictions.get("inediblePartMatch") or "-")[:15]
        ratio = f"{predictions.get('rawToCookedRatio', 0):.2f}"
        ratio_match = (predictions.get("rawToCookedRatioMatch") or "-")[:15]

        # Format confidence scores
        def fmt_conf(key):
            if key in confidence:
                return f"{confidence[key]:.0%}"
            return "-"

        table.add_row(
            ing["name"][:25],
            categories_str,
            food_type_match,
            fmt_conf("categories"),
            packaging,
            transport,
            crop,
            fmt_conf("cropGroup"),
            density,
            density_match,
            fmt_conf("density"),
            inedible,
            inedible_match,
            fmt_conf("inediblePart"),
            ratio,
            ratio_match,
            fmt_conf("rawToCookedRatio"),
        )

    console.print(table)

    # 4. Sauvegarde du modèle
    print("\n" + "=" * 60)
    print("💾 Sauvegarde du modèle...")
    predictor.save("/tmp/demo_predictor.pkl")

    # 5. Rechargement et test
    print("\n🔄 Rechargement du modèle...")
    predictor2 = Predictor.load("/tmp/demo_predictor.pkl")

    test = {"name": "Pomme de terre FR", "activityName": "Potato, at farm gate {FR} U"}
    pred = predictor2.predict(test)
    print(f"\n✓ Test après rechargement: {test['name']}")
    print(f"  → categories: {pred['categories']}")
    print(f"  → packaging: {pred['packaging']}")
    print(f"  → cropGroup: {pred['cropGroup']}")
    print(f"  → transportCooling: {pred['transportCooling']}")


if __name__ == "__main__":
    main()
