---
title: Prédiction et intégration des métadonnées ingrédients
author: Ecobalyse
---

## 1. Les métadonnées à prédire

Pour chaque nouvel ingrédient, **11 métadonnées** doivent être renseignées :

| Métadonnée | Description | Exemple |
|------------|-------------|---------|
| foodType | Type d'aliment (8 catégories) | fruit, grain, meat |
| novaGroup | Niveau de transformation (NOVA 1-4) | 1 = brut, 4 = ultra-transformé |
| processingState | Etat de transformation | raw, processed |
| categories | Catégories Ecobalyse | vegetable_fresh, grain_processed |
| packaging | Type d'emballage détecté | fresh, frozen, canned |
| transportCooling | Besoin de réfrigération au transport | none, always, once |
| cropGroup | Groupe de culture (végétaux) | VERGERS, BLE TENDRE |
| defaultOrigin | Origine géographique par défaut | France, EuropeAndMaghreb |
| density | Masse volumique (kg/L) | 0.85 |
| inediblePart | Fraction non comestible (0-1) | 0.20 |
| rawToCookedRatio | Ratio poids cuit / poids cru | 0.856 |

## 2. Données de départ

**Pour chaque ingrédient, on dispose de :**

- **Nom de l'ingrédient** (français) : ex. "Filet de saumon"
- **Nom du procédé ACV** (activityName) : ex. "Salmon, fillet, at plant {NO}"

**Données de référence existantes :**

- **ingredients.json** (~560 ingrédients Ecobalyse existants) : sert de base d'entraînement, chaque ingrédient ayant déjà toutes ses métadonnées renseignées
- **fao_density.csv** : ~640 densités alimentaires de référence (source FAO)
- **agb_inedible.csv** : parts non comestibles de référence (annexes Agribalyse)

## 3. Architecture globale

```
                  Nom ingrédient + Nom procédé ACV
                              │
                    ┌─────────┴─────────┐
                    │   Traduction       │
                    │   FR → EN          │
                    └─────────┬─────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
     ┌────────────┐  ┌──────────────┐  ┌───────────┐
     │  FoodOn     │  │  Détection   │  │ Données   │
     │  Ontologie  │  │  mots-clés   │  │ référence │
     │  (50k       │  │  (regex FR   │  │ (CSV)     │
     │  termes     │  │  et EN)      │  │           │
     │  aliments)  │  │              │  │           │
     └──────┬─────┘  └──────┬───────┘  └─────┬─────┘
            └───────────────┼─────────────────┘
                            ▼
               ┌────────────────────────┐
               │  Correspondance au     │
               │  plus proche voisin    │
               │  (par champ)           │
               └────────────┬───────────┘
                            ▼
                    11 métadonnées prédites
```

**Sources et outils utilisés :**

- **FoodOn** : ontologie de 50 000 termes alimentaires structurés en arbre (fruit, légume, viande...). Fournit 20 dimensions sémantiques par ingrédient.
- **Classification NOVA** (Monteiro et al.) : référence pour les 4 niveaux de transformation alimentaire.
- **Modèle de traduction** (Helsinki-NLP) : traduit les noms français en anglais pour la correspondance.
- **Fichiers de référence CSV** : food_type.csv, nova_classification.csv, cropgroup.csv, density.csv, inedible_part.csv, cooked_to_raw.csv, transport_cooling.csv.

## 4. Ordre de prédiction : métadonnées de base en premier

Les métadonnées ne sont pas indépendantes. Certaines servent de base aux autres :

```
  ┌──────────────────────────────────────────────────────┐
  │  NIVEAU 1 : Détectées en premier (base)              │
  │                                                      │
  │  foodType ─────────┐                                 │
  │  (type d'aliment)  │                                 │
  │                    ├──► servent de base aux autres    │
  │  novaGroup ────────┘                                 │
  │  (niveau de                                          │
  │   transformation)                                    │
  └──────────────────────────────────────────────────────┘
                       │
                       ▼
  ┌──────────────────────────────────────────────────────┐
  │  NIVEAU 2 : Dérivées directement                     │
  │                                                      │
  │  processingState ← novaGroup                         │
  │  categories ← foodType + novaGroup                   │
  │  packaging ← mots-clés dans le nom                   │
  └──────────────────────────────────────────────────────┘
                       │
                       ▼
  ┌──────────────────────────────────────────────────────┐
  │  NIVEAU 3 : Utilisent foodType + novaGroup           │
  │             comme valeurs par défaut                  │
  │                                                      │
  │  transportCooling ← packaging + foodType + novaGroup │
  │  cropGroup ← foodType + mots-clés                    │
  │  defaultOrigin ← code pays dans le procédé ACV       │
  │  density ← correspondance textuelle ou défaut        │
  │            foodType                                   │
  │  inediblePart ← mots-clés ou défaut                  │
  │                 foodType + novaGroup                  │
  │  rawToCookedRatio ← mots-clés ou défaut foodType     │
  └──────────────────────────────────────────────────────┘
```

**En résumé** : foodType et novaGroup sont les deux piliers. Ils déterminent les valeurs par défaut de presque toutes les autres métadonnées quand aucune correspondance directe n'est trouvée.

## 5. Résultats et fusion avec les données existantes

**Résultat de la prédiction :**

- ~140 nouveaux ingrédients prédits (variante FR)
- Chaque prédiction inclut une règle explicative et un score de confiance

**Problématiques de fusion :**

Les nouveaux ingrédients doivent être intégrés dans le fichier `activities.json` existant (~800 activités). Plusieurs cas se présentent :

| Situation | Traitement |
|-----------|-----------|
| Nouvel ingrédient, nouvelle activité ACV | Ajout direct |
| Nouvel ingrédient, activité ACV existante | L'ingrédient est rattaché à l'activité existante |
| Même nom d'affichage qu'un ingrédient existant | L'existant est conservé (pas d'écrasement) |

**Gestion de la transition (2025) :**

- Les anciens ingrédients et leurs activités recoivent le suffixe `(2025)` sur leur nom d'affichage, pour les distinguer des nouveaux
- Les activités réutilisées par de nouveaux ingrédients conservent leur nom d'origine (elles seront conservées dans la version finale)
- L'opération est idempotente : relancer la fusion produit le même résultat
