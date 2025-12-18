● Ingredient Metadata Prediction System Overview

  Architecture Diagram

  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                           TRAINING PHASE (fit)                              │
  ├─────────────────────────────────────────────────────────────────────────────┤
  │                                                                             │
  │  ingredients.json ──────┐                                                   │
  │  (426 existing          │                                                   │
  │   ingredients)          │                                                   │
  │                         ▼                                                   │
  │              ┌──────────────────────┐                                       │
  │              │  French→English      │                                       │
  │              │  Translation         │                                       │
  │              │  (Helsinki-NLP)      │                                       │
  │              └──────────┬───────────┘                                       │
  │                         │                                                   │
  │                         ▼                                                   │
  │              ┌──────────────────────┐      ┌─────────────────────────────┐  │
  │              │  Feature Extraction  │      │   Reference CSV Data        │  │
  │              │  per ingredient      │      │                             │  │
  │              │                      │      │  • food_type.csv (139)      │  │
  │              │  ┌────────────────┐  │      │  • processing_state.csv     │  │
  │              │  │ FoodOn Ontology│  │      │    (129)                    │  │
  │              │  │ (52K terms)    │  │      │  • cropgroup.csv (73)       │  │
  │              │  │ → 20 dims      │  │      │  • density.csv (747)        │  │
  │              │  └────────────────┘  │      │  • inedible_part.csv (215)  │  │
  │              │  ┌────────────────┐  │      │  • cooked_to_raw.csv (129)  │  │
  │              │  │ Regex Patterns │  │      │  • transport_cooling.csv    │  │
  │              │  │ (25 binary)    │  │      │    (49)                     │  │
  │              │  │ is_meat,       │  │      └─────────────┬───────────────┘  │
  │              │  │ is_frozen...   │  │                    │                  │
  │              │  └────────────────┘  │                    │                  │
  │              └──────────┬───────────┘                    │                  │
  │                         │                                │                  │
  │                         ▼                                ▼                  │
  │              ┌─────────────────────────────────────────────────────────┐    │
  │              │           NearestNeighborMatcher (one per field)        │    │
  │              │                                                         │    │
  │              │  All matchers combine ingredients.json + reference CSV: │    │
  │              │                                                         │    │
  │              │   foodType_matcher ←── food_type.csv only (139)         │    │
  │              │   processingState_matcher ←── ingredients + CSV (555)   │    │
  │              │   cropGroup_matcher ←── ingredients + CSV (499)         │    │
  │              │   transportCooling_matcher ←── ingredients + CSV (475)  │    │
  │              │   density_matcher ←── ingredients + CSV (747)           │    │
  │              │   inediblePart_matcher ←── ingredients + CSV (641)      │    │
  │              │   rawToCookedRatio_matcher ←── ingredients + CSV (555)  │    │
  │              └─────────────────────────────────────────────────────────┘    │
  │                                                                             │
  └─────────────────────────────────────────────────────────────────────────────┘


  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                         PREDICTION PHASE (predict)                          │
  ├─────────────────────────────────────────────────────────────────────────────┤
  │                                                                             │
  │   New Ingredient                                                            │
  │   ┌─────────────────────────┐                                               │
  │   │ name: "Watermelon"      │                                               │
  │   │ activityName: "..." (opt)│                                              │
  │   └───────────┬─────────────┘                                               │
  │               │                                                             │
  │               ▼                                                             │
  │   ┌───────────────────────────────────────────────────────────────────┐     │
  │   │                    NearestNeighborMatcher.predict()               │     │
  │   │                                                                   │     │
  │   │   PRIORITY 1: Exact Text Match                                    │     │
  │   │   ┌─────────────────────────────────────────────────────────┐     │     │
  │   │   │ "watermelon" == "watermelon" in reference? → YES (1.0)  │     │     │
  │   │   │ or translated match                                     │     │     │
  │   │   └─────────────────────────────────────────────────────────┘     │     │
  │   │                          │                                        │     │
  │   │                          ▼ (if no exact match)                    │     │
  │   │   PRIORITY 2: Substring Match (longest wins)                      │     │
  │   │   ┌─────────────────────────────────────────────────────────┐     │     │
  │   │   │ "melon" in "watermelon"? → confidence 0.95              │     │     │
  │   │   └─────────────────────────────────────────────────────────┘     │     │
  │   │                          │                                        │     │
  │   │                          ▼ (if no substring match)                │     │
  │   │   PRIORITY 3: FoodOn + Regex Similarity                           │     │
  │   │   ┌─────────────────────────────────────────────────────────┐     │     │
  │   │   │ Extract 45-dim feature vector:                          │     │     │
  │   │   │   • 20 FoodOn dimensions (ontology similarity)          │     │     │
  │   │   │   • 25 regex binary features                            │     │     │
  │   │   │                                                         │     │     │
  │   │   │ Cosine similarity with all reference items              │     │     │
  │   │   │ Return best match                                       │     │     │
  │   │   └─────────────────────────────────────────────────────────┘     │     │
  │   └───────────────────────────────────────────────────────────────────┘     │
  │               │                                                             │
  │               ▼                                                             │
  │   ┌───────────────────────────────────────────────────────────────────┐     │
  │   │                      Predicted Metadata                           │     │
  │   │                                                                   │     │
  │   │   foodType: "fruit"          (matched "pastèque")                 │     │
  │   │   processingState: "raw"     (matched "watermelon")               │     │
  │   │   cropGroup: "LEGUMES-FLEURS"                                     │     │
  │   │   transportCooling: "always"                                      │     │
  │   │   density: 1.0                                                    │     │
  │   │   inediblePart: 0.35                                              │     │
  │   │   rawToCookedRatio: 1.0                                           │     │
  │   └───────────────────────────────────────────────────────────────────┘     │
  │                                                                             │
  └─────────────────────────────────────────────────────────────────────────────┘

  Key Components

  1. FoodOn Ontology Features (20 dimensions)

  Query: "Watermelon"
             ↓
     FoodOn Ontology (52,628 food terms)
             ↓
     Find top-20 most similar terms by word overlap
             ↓
     [0.8, 0.6, 0.4, 0.3, ...] (20 similarity scores)

  2. Regex Binary Features (25 dimensions)

  DETECTION_PATTERNS = {
      "is_meat": r"\b(viande|meat|boeuf|porc|poulet|...)\b",
      "is_fish": r"\b(poisson|fish|saumon|thon|...)\b",
      "is_dairy": r"\b(lait|milk|fromage|cheese|...)\b",
      "is_vegetable": r"\b(légume|vegetable|carotte|...)\b",
      "is_fruit": r"\b(fruit|pomme|orange|...)\b",
      "is_frozen": r"\b(surgelé|frozen|congelé)\b",
      "is_canned": r"\b(conserve|canned|boîte)\b",
      "at_farm_gate": r"\{[A-Z]{2}\}.*\bU\b",
      # ... 25 total patterns
  }

  3. NearestNeighborMatcher Priority System

  Input: "Pomme de terre" (Potato)

  1. EXACT MATCH (confidence 1.0)
     └─ "pomme de terre" == reference["pomme de terre"]? ✓ → MATCH

  2. SUBSTRING MATCH (confidence 0.95)
     └─ If no exact: find longest substring in reference

  3. SIMILARITY FALLBACK
     └─ Cosine similarity of FoodOn+regex features

  Data Flow Summary

  ┌──────────────┐    ┌─────────────┐    ┌──────────────┐    ┌─────────────┐
  │   Training   │    │   Feature   │    │   Nearest    │    │  Prediction │
  │     Data     │───▶│  Extraction │───▶│   Neighbor   │───▶│   Output    │
  │              │    │             │    │   Matchers   │    │             │
  └──────────────┘    └─────────────┘    └──────────────┘    └─────────────┘
         │                  │                   │                   │
         │                  │                   │                   │
    ingredients.json   FoodOn (20d)     7 matchers, each      foodType
    + CSV reference    + Regex (25d)    combining:            processingState
      data (merged)    = 45 dims        - ingredients.json    cropGroup
                                        - reference CSV       density
                                                              inediblePart
                                                              rawToCookedRatio
                                                              transportCooling

  Example Prediction

  Input: {"name": "Salmon fillet"}

  Step 1: Translate → "Filet de saumon"

  Step 2: For each field, find best match:
    ├─ foodType:        "saumon" → fish_seafood (exact match)
    ├─ processingState: "salmon" → raw (exact match)
    ├─ cropGroup:       N/A (animal product, no cropGroup)
    ├─ transportCooling: "saumon" → always (fish needs cooling)
    ├─ density:         "saumon" → 1.05 (exact match)
    ├─ inediblePart:    similarity → 0.0 (fillet, no waste)
    └─ rawToCookedRatio: similarity → 0.75

  Output: {
    "foodType": "fish_seafood",
    "processingState": "raw",
    "transportCooling": "always",
    "density": 1.05,
    "inediblePart": 0.0,
    "rawToCookedRatio": 0.75
  }

