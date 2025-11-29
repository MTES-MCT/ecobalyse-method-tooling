#!/usr/bin/env python3
"""
Démonstration du prédicteur de métadonnées.

Usage:
    python demo_predict.py                    # Test sur tous les ingrédients CSV
    python demo_predict.py "Tomate cerise"    # Test unitaire sur un ingrédient
"""

import argparse
import json

import pandas
from rich.console import Console
from rich.progress import track
from rich.table import Table

from predict import Predictor

# Données d'entraînement (échantillon)
TRAINING_DATA = []
with open("../ingredients.json") as data:
    TRAINING_DATA = json.load(data)


def main():
    parser = argparse.ArgumentParser(description="Démonstration du prédicteur")
    parser.add_argument("ingredient", nargs="?", help="Nom d'ingrédient à tester (optionnel)")
    parser.add_argument("--activity", "-a", default="", help="Nom du procédé ACV (optionnel)")
    parser.add_argument("--clear-cache", action="store_true", help="Effacer le cache de traduction")
    args = parser.parse_args()

    if args.clear_cache:
        Predictor.clear_translation_cache()
        return
    print("=" * 60)
    print("DÉMONSTRATION DU PRÉDICTEUR DE MÉTADONNÉES")
    print("=" * 60)

    # 1. Entraînement
    print("\n📚 Entraînement sur", len(TRAINING_DATA), "ingrédients...")
    predictor = Predictor()
    predictor.fit(TRAINING_DATA)

    # 2. Mode test unitaire ou batch
    if args.ingredient:
        # Test unitaire sur un seul ingrédient
        print(f"\n🔮 Test unitaire: {args.ingredient}")
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

    # Create table with columns for each metadata + confidence
    table = Table(title="Predictions", show_header=True, header_style="bold")
    table.add_column("Name", style="cyan", no_wrap=True, max_width=30)
    table.add_column("categories", no_wrap=True)
    table.add_column("conf", justify="right")
    table.add_column("cropGroup", no_wrap=True)
    table.add_column("conf", justify="right")
    table.add_column("transport", no_wrap=True)
    table.add_column("conf", justify="right")
    table.add_column("origin", no_wrap=True)
    table.add_column("density", justify="right")
    table.add_column("conf", justify="right")
    table.add_column("inedible", justify="right")
    table.add_column("conf", justify="right")
    table.add_column("ratio", justify="right")
    table.add_column("conf", justify="right")

    # Collect all confidence scores for mean calculation
    all_conf = {
        "categories": [],
        "cropGroup": [],
        "transportCooling": [],
        "density": [],
        "inediblePart": [],
        "rawToCookedRatio": [],
    }

    # Collect predictions first with progress bar
    all_predictions = []
    for ing in track(test_ingredients, description="Predicting..."):
        predictions, confidence = predictor.predict_with_confidence(ing)
        all_predictions.append((ing, predictions, confidence))

    # Build table from collected predictions
    for ing, predictions, confidence in all_predictions:

        # Collect confidence scores
        for key in all_conf:
            if key in confidence:
                all_conf[key].append(confidence[key])

        # Format values
        cats = ",".join(predictions.get("categories", []))
        crop = predictions.get("cropGroup") or "-"
        transport = predictions.get("transportCooling", "-")
        origin = predictions.get("defaultOrigin", "-")
        density = f"{predictions.get('density', 0):.2f}"
        inedible = f"{predictions.get('inediblePart', 0):.2f}"
        ratio = f"{predictions.get('rawToCookedRatio', 0):.2f}"

        # Format confidence scores
        def fmt_conf(key):
            if key in confidence:
                return f"{confidence[key]:.0%}"
            return "-"

        table.add_row(
            ing["name"][:30],
            cats,
            fmt_conf("categories"),
            crop,
            fmt_conf("cropGroup"),
            transport,
            fmt_conf("transportCooling"),
            origin,
            density,
            fmt_conf("density"),
            inedible,
            fmt_conf("inediblePart"),
            ratio,
            fmt_conf("rawToCookedRatio"),
        )

    # Add mean row
    def mean_conf(scores):
        return f"{sum(scores) / len(scores):.0%}" if scores else "-"

    table.add_row(
        "[bold]MEAN[/bold]",
        "",
        f"[bold]{mean_conf(all_conf['categories'])}[/bold]",
        "",
        f"[bold]{mean_conf(all_conf['cropGroup'])}[/bold]",
        "",
        f"[bold]{mean_conf(all_conf['transportCooling'])}[/bold]",
        "",
        "",
        f"[bold]{mean_conf(all_conf['density'])}[/bold]",
        "",
        f"[bold]{mean_conf(all_conf['inediblePart'])}[/bold]",
        "",
        f"[bold]{mean_conf(all_conf['rawToCookedRatio'])}[/bold]",
        style="on dark_green",
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
    print(f"  → cropGroup: {pred['cropGroup']}")
    print(f"  → transportCooling: {pred['transportCooling']}")

    print("\n" + "=" * 60)
    print("✅ Démonstration terminée!")
    print("=" * 60)


if __name__ == "__main__":
    main()
