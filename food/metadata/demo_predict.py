#!/usr/bin/env python3
"""
Démonstration du prédicteur de métadonnées.

Usage:
    python demo_predict.py
"""

import json

import pandas
from predict import Predictor

# Données d'entraînement (échantillon)
TRAINING_DATA = []
with open("../ingredients.json") as data:
    TRAINING_DATA = json.load(data)


def main():
    print("=" * 60)
    print("DÉMONSTRATION DU PRÉDICTEUR DE MÉTADONNÉES")
    print("=" * 60)

    # 1. Entraînement
    print("\n📚 Entraînement sur", len(TRAINING_DATA), "ingrédients...")
    predictor = Predictor()
    predictor.fit(TRAINING_DATA)

    # 2. Évaluation
    print("\n📈 Évaluation en cross-validation:")
    predictor.evaluate()

    # 3. Tests de prédiction
    #   test_ingredients = [
    #       {
    #           "name": "Courgette bio",
    #           "activityName": "Zucchini, organic, at farm gate {FR} U",
    #       },
    #       {
    #           "name": "Filet de cabillaud surgelé",
    #           "activityName": "Cod fillet, frozen, at processing {IS} U",
    #       },
    #       {
    #           "name": "Noix de cajou grillée",
    #           "activityName": "Cashew nut, roasted, at plant {VN} U",
    #       },
    #       {
    #           "name": "Yaourt nature",
    #           "activityName": "Yoghurt, plain, at dairy {FR} U",
    #       },
    #       {
    #           "name": "Lentilles vertes",
    #           "activityName": "Green lentils, at farm gate {FR} U",
    #       },
    #   ]
    test_ingredients = (
        pandas.read_csv("../icv_high_impact_final.csv")[["item", "icv_final"]]
        .rename(columns={"item": "name", "icv_final": "activityName"})
        .dropna()
        .to_dict("records")
    )

    print("\n" + "=" * 60)
    print("🔮 PRÉDICTIONS POUR NOUVEAUX INGRÉDIENTS")
    print("=" * 60)

    for ing in test_ingredients:
        print(f"\n{'─' * 50}")
        print(f"🥗 {ing['name']}")
        print(f"   Process: {ing['activityName']}")

        predictions, confidence = predictor.predict_with_confidence(ing)

        print("\n   Prédictions:")
        for key, value in predictions.items():
            if key in confidence:
                conf = confidence[key]
                conf_bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
                print(f"   • {key}: {value} [{conf_bar}] {conf:.0%}")
            else:
                print(f"   • {key}: {value}")

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
