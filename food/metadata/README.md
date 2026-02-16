# Ingredient Metadata Prediction System

## Directory Structure

```
metadata/
├── source/
│   ├── new_ingredient_FR.csv      # Input: FR ingredients to predict
│   ├── new_ingredient_OI.csv      # Input: OI ingredients to predict
│   └── keep.csv                   # DisplayNames to keep unsuffixed during merge
├── generated/
│   ├── predictions_{variant}.csv       # Output: CSV with predictions + confidence
│   ├── new_activities_{variant}.json   # Output: activities.json format
│   └── fichier_final_{variant}.csv     # Output: final CSV with metadata + impacts
├── data/
│   └── foodon.owl                 # FoodOn ontology (auto-downloaded)
├── reference/
│   ├── food_type.csv              # custom food type mappings
│   ├── processing_state.csv       # custom processing state mappings
│   ├── nova_classification.csv    # NOVA 1-4 reference data
│   ├── cropgroup.csv              # custom crop group mappings
│   ├── density.csv                # custom density values
│   ├── inedible_part.csv          # custom inedible part percentages
│   ├── fao_density.csv            # FAO density reference (do not modify)
│   ├── agb_inedible.csv           # AGB inedible reference (do not modify)
│   ├── cooked_to_raw.csv          # custom cooked/raw ratios
│   ├── transport_cooling.csv      # custom transport cooling
│   ├── food_type_density.csv      # foodType default densities
│   ├── food_type_inedible_part.csv # foodType×NOVA default inedible parts
│   └── food_type_cooked_to_raw.csv # foodType default cooking ratios
├── export.py                      # Main export script
├── predict.py                     # Predictor class
├── validate_nova.py               # NOVA classification validation
├── foodon_loader.py               # FoodOn ontology loader
└── .env                           # Environment configuration
```

## Setup

Create a `.env` file with required environment variables:

```bash
BRIGHTWAY2_DIR=/path/to/brightway-dirs/main
ECOBALYSE_DATA=../../../ecobalyse-data
ECOBALYSE=../../../ecobalyse
```

## Export Workflow

The export process has multiple steps to generate complete ingredient data with environmental impacts.

### Step 1: Export Metadata and Merge Activities

```bash
uv run export.py metadata --variant FR --add-old-suffix
```

This will:
1. Train the predictor on existing ingredients from `$ECOBALYSE/public/data/food/ingredients.json`
2. Predict metadata for ingredients in `source/new_ingredient_{variant}.csv`
3. Generate `generated/predictions_{variant}.csv` (detailed predictions with confidence)
4. Generate `generated/new_activities_{variant}.json` (Ecobalyse format)
5. Merge new activities into `$ECOBALYSE_DATA/activities.json`
6. Copy reference CSVs to `$ECOBALYSE_DATA/food/metadata/`

The `--add-old-suffix` flag adds a `(2025)` suffix to pre-existing activity and ingredient displayNames. Activities reused by new ingredients keep their original displayName.

Variants: `FR`, `ORG`, `UE`, `OI`, `NUE`.

### Step 2: Regenerate Ingredients in ecobalyse-data

After step 1, run in ecobalyse-data:

```bash
just export-all
```

This regenerates `ingredients.json` with the new activities.

### Step 3: Generate Final Data with Impacts

```bash
uv run export.py final_data --variant FR
uv run export.py final_data --variant OI
```

This will:
1. Read `source/new_ingredient_{variant}.csv`
2. Add metadata from `new_activities_{variant}.json` (via activityName matching)
3. Add environmental impacts from `processes_impacts.json` (via activityName)
4. Generate `generated/fichier_final_{variant}.csv`

### Input Files: `source/new_ingredient_{variant}.csv`

Common columns (both FR and OI):
- `Id unique`: Unique identifier (e.g., EB_id0001)
- `item`: English ingredient name
- `item trad`: French translation
- `icv final`: LCA activity name (used for matching)
- `location`: Geographic code (e.g., FR, CN, GLO)

FR-specific columns:
- `Production_FR`: FR/NON FR/DOM
- `proxy`: Proxy strategy

OI-specific columns:
- `database`: Source database name
- `ecs`: Ecosystemic services code

## Usage

```bash
uv run export.py metadata --variant FR                    # Export + merge (no suffix)
uv run export.py metadata --variant OI                    # Export OI variant
uv run export.py metadata --variant FR --add-old-suffix   # Export + merge + add (2025) suffix
uv run export.py metadata --variant FR --clear-cache      # Clear translation cache first
uv run export.py final_data --variant FR                  # Generate final CSV with impacts
uv run export.py final_data --variant OI                  # Generate final CSV with impacts (OI)
uv run export.py remove-old                               # Remove (2025)-suffixed entries from activities.json
uv run validate_nova.py --folds 5                         # Validate NOVA classification (5-fold CV)
```

## Architecture

### Training Phase

```
ingredients.json ──────┐
(existing              │
 ingredients)          │
                       ▼
            ┌──────────────────────┐
            │  French→English      │
            │  Translation         │
            │  (Helsinki-NLP)      │
            └──────────┬───────────┘
                       │
                       ▼
            ┌──────────────────────┐      ┌─────────────────────────────┐
            │  Feature Extraction  │      │   Reference CSV Data        │
            │  per ingredient      │      │                             │
            │                      │      │  • food_type.csv            │
            │  ┌────────────────┐  │      │  • processing_state.csv     │
            │  │ FoodOn Ontology│  │      │  • cropgroup.csv            │
            │  │ (52K terms)    │  │      │  • density.csv              │
            │  │ → 20 dims      │  │      │  • inedible_part.csv        │
            │  └────────────────┘  │      │  • cooked_to_raw.csv        │
            │  ┌────────────────┐  │      │  • transport_cooling.csv    │
            │  │ Regex Patterns │  │      └─────────────┬───────────────┘
            │  │ (28 binary)    │  │                    │
            │  └────────────────┘  │                    │
            └──────────┬───────────┘                    │
                       │                                │
                       ▼                                ▼
            ┌─────────────────────────────────────────────────────────┐
            │           NearestNeighborMatcher (one per field)        │
            │                                                         │
            │   foodType_matcher ←── food_type.csv only               │
            │   nova_matcher ←── nova_classification.csv              │
            │   cropGroup_matcher ←── ingredients + CSV               │
            │   transportCooling_matcher ←── ingredients + CSV        │
            │   density_matcher ←── ingredients + CSV                 │
            │   inediblePart_matcher ←── ingredients + CSV            │
            │   rawToCookedRatio_matcher ←── ingredients + CSV        │
            └─────────────────────────────────────────────────────────┘
```

### Prediction Phase

```
New Ingredient
┌─────────────────────────┐
│ name: "Watermelon"      │
│ activityName: ".." (opt)│
└───────────┬─────────────┘
            │
            ▼
┌───────────────────────────────────────────────────────────────────┐
│                    NearestNeighborMatcher.predict()               │
│                                                                   │
│   PRIORITY 1: Exact Text Match (confidence = 1.0)                 │
│   ┌─────────────────────────────────────────────────────────┐     │
│   │ "watermelon" == "watermelon" in reference?              │     │
│   └─────────────────────────────────────────────────────────┘     │
│                          │                                        │
│                          ▼ (if no exact match)                    │
│   PRIORITY 1.5: Semantic Near-Exact Match (confidence = 0.98)     │
│   ┌─────────────────────────────────────────────────────────┐     │
│   │ Sentence-transformer embedding similarity > 0.9         │     │
│   │ Handles plurals like "Avocado" ≈ "Avocados"            │     │
│   └─────────────────────────────────────────────────────────┘     │
│                          │                                        │
│                          ▼ (if no semantic match)                 │
│   PRIORITY 2: Word Boundary Match (confidence = 0.95)             │
│   ┌─────────────────────────────────────────────────────────┐     │
│   │ Uses regex \b word boundaries to match complete words   │     │
│   │ Prevents false positives like "bread" in "breadfruit"   │     │
│   └─────────────────────────────────────────────────────────┘     │
│                          │                                        │
│                          ▼ (if no word match)                     │
│   PRIORITY 3: FoodOn + Regex Similarity (confidence = cosine)     │
│   ┌─────────────────────────────────────────────────────────┐     │
│   │ 48-dim feature vector: 20 FoodOn + 28 regex             │     │
│   │ Cosine similarity with all reference items              │     │
│   └─────────────────────────────────────────────────────────┘     │
└───────────────────────────────────────────────────────────────────┘
            │
            ▼
┌───────────────────────────────────────────────────────────────────┐
│                      Predicted Metadata                           │
│                                                                   │
│   foodType: "fruit"                                               │
│   processingState: "raw"                                          │
│   cropGroup: "LEGUMES-FLEURS"                                     │
│   transportCooling: "always"                                      │
│   density: 1.0                                                    │
│   inediblePart: 0.35                                              │
│   rawToCookedRatio: 1.0                                           │
└───────────────────────────────────────────────────────────────────┘
```

## NOVA Classification

The system classifies ingredients into NOVA 4-group categories:

| NOVA Group | Description | Examples |
|------------|-------------|----------|
| **NOVA 1** | Unprocessed/minimally processed | Fresh fruits, vegetables, meat, fish, milk, eggs, dried/frozen foods |
| **NOVA 2** | Culinary ingredients | Oils, butter, sugar, salt, flour, starch, vinegar |
| **NOVA 3** | Processed foods | Canned vegetables, cheese, ham, bacon, smoked fish, jam |
| **NOVA 4** | Ultra-processed | Textured proteins, corn syrup, distilled spirits, ready meals |

### NOVA Detection Priority

```
1. Distilled spirits (brandy, vodka, etc.) → NOVA 4
2. NOVA 2 culinary ingredients (oil, butter, sugar, salt, flour)
3. NOVA 3 processed indicators (jam, pickled, cured, smoked, ham, bacon)
4. NOVA 4 ultra-processed (textured, hydrolyzed, corn syrup, maltodextrin)
5. Activity name patterns ("at farm", "at plant", "production")
6. FoodType-based defaults (fruit/vegetable/fish → NOVA 1)
7. Nearest neighbor matching on reference data
8. Default: NOVA 1
```

### Validation

The NOVA classifier can be validated using cross-validation:

```bash
uv run validate_nova.py --folds 5   # 5-fold cross-validation
uv run validate_nova.py --test-ratio 0.3  # 70/30 train/test split
```

Current performance (5-fold CV on 93 reference items):
- **Accuracy: 89.2%**
- NOVA 1: F1=0.893, NOVA 2: F1=0.960, NOVA 3: F1=0.829, NOVA 4: F1=0.872

### processingState Derivation

`processingState` is derived from NOVA:
- NOVA 1 → `raw`
- NOVA 2, 3, 4 → `processed`

## Output Format

### predictions.csv

| Column | Description |
|--------|-------------|
| name | Ingredient name |
| categories | Predicted categories (comma-separated) |
| foodType | Food type + match rule + confidence |
| **novaGroup** | NOVA 1-4 classification |
| **novaGroupMatch** | Detection rule + confidence |
| processingState | Derived from NOVA (raw/processed) |
| transportCooling | Transport cooling + match rule |
| cropGroup | Crop group + match rule + confidence |
| density | Density value + match rule + confidence |
| inediblePart | Inedible part + match rule + confidence |
| rawToCookedRatio | Raw-to-cooked ratio + match rule + confidence |

### new_activities.json

Match info includes a human-readable rule explanation and confidence:

```json
{
  "ingredientDensity": 0.9,
  "ingredientDensityMatch": {
    "rule": "Matched with bell pepper in density.csv",
    "confidence": 0.95
  },
  "novaGroup": 1,
  "novaGroupMatch": {
    "rule": "at_farm_source → NOVA 1",
    "confidence": 0.95
  }
}
```

All Match fields follow the same format: `{"rule": "...", "confidence": float}`. The rule explains how the value was determined (text match, keyword detection, or default fallback).

## Feature Extraction

Each ingredient is represented as a 48-dimensional feature vector (20 FoodOn + 28 regex).

### FoodOn Ontology Features (20 dimensions)

The FoodOn ontology (~50K food terms) provides structured semantic features:

```
Query: "Watermelon"
         ↓
   FoodOn Ontology lookup (exact/fuzzy match)
         ↓
   Extract ancestor categories from ontology graph
         ↓
   20-dim feature vector:
     - dims 0-8:  Type flags (vegetable, fruit, grain, meat, fish, dairy, nut, spice, beverage)
     - dims 9-13: Processing flags (raw, cooked, preserved, fermented, processed)
     - dims 14-17: Source flags (plant, animal, fungus, mineral)
     - dims 18-19: Numeric (hierarchy_depth, match_confidence)
```

### Regex Binary Features (28 dimensions)

Pattern-based detection for French/English ingredient names:

```python
DETECTION_PATTERNS = {
    # Processing (9 patterns)
    "is_organic", "is_fresh", "is_frozen", "is_cooked", "is_raw",
    "is_dried", "is_processed", "is_canned", "is_smoked",

    # Food types - Animals (5 patterns)
    "is_meat", "is_fish", "is_seafood", "is_egg", "is_dairy",

    # Food types - Plants (9 patterns)
    "is_vegetable", "is_fruit", "is_grain", "is_legume", "is_nut_seed",
    "is_oil_fat", "is_spice", "is_beverage", "is_sugar_sweet",

    # LCA process info (5 patterns)
    "at_farm_gate", "at_plant", "at_processing", "is_greenhouse", "is_heated_greenhouse"
}
```

## Confidence Scores

| Match Type | Confidence |
|------------|------------|
| Exact match | 1.0 |
| Semantic near-exact match | 0.98 |
| Word boundary match | 0.95 |
| FoodOn + regex similarity | 0.0 - 1.0 (cosine) |

## Field Prediction Summary

Each field uses a specific prediction strategy:

| Field | Strategy | Fallback |
|-------|----------|----------|
| **foodType** | Text match on food_type.csv | Feature similarity |
| **novaGroup** | Rule-based (see NOVA section) | Nearest neighbor |
| **processingState** | Derived from novaGroup | - |
| **categories** | Computed from foodType + novaGroup | - |
| **transportCooling** | Rules based on foodType + novaGroup | Nearest neighbor |
| **cropGroup** | Pattern-based (foodType + keywords) | Nearest neighbor matcher |
| **density** | Text match with word verification | FoodType default |
| **inediblePart** | Text match, feature similarity | - |
| **rawToCookedRatio** | Text match, feature similarity | - |

## Category Computation

Categories are computed directly from `foodType + novaGroup`:

```python
def _compute_category(food_type, nova_group):
    is_raw = nova_group == 1

    if food_type in {"vegetable", "fruit"}:
        return "vegetable_fresh" if is_raw else "vegetable_processed"
    elif food_type == "grain":
        return "grain_raw" if is_raw else "grain_processed"
    elif food_type == "nut_oilseed":
        return "nut_oilseed_raw" if is_raw else "nut_oilseed_processed"
    elif food_type in {"meat", "fish_seafood"}:
        return "animal_product"
    elif food_type == "dairy":
        return "dairy_product"
    elif food_type == "spice_condiment":
        return "spice_condiment_additive"
    else:
        return "misc"
```

## transportCooling Rules

transportCooling is determined by rules based on `foodType + novaGroup`:

```
1. Packaging detection (frozen → always, dried/canned → none)
2. NOVA 1 (raw) + perishable type (vegetable, fruit, meat, fish, dairy) → always
3. Non-perishable types (grain, nut_oilseed, spice_condiment) → none
4. Fallback: nearest neighbor matching
```

This rule-based approach handles ~97% of items (220/228), with only edge cases falling back to the matcher.

## cropGroup Prediction

cropGroup assigns French agricultural categories to plant-based ingredients. It uses pattern-based inference with a minimal CSV fallback.

### FoodType to CropGroup Mapping

| FoodType | Default CropGroup |
|----------|-------------------|
| fruit | VERGERS |
| vegetable | LEGUMES-FLEURS |
| grain | AUTRES CEREALES |
| nut_oilseed | FRUITS A COQUES |
| legume | LEGUMINEUSES A GRAIN |
| spice_condiment | DIVERS |
| beverage | DIVERS |

### Keyword Pattern Overrides

Specific patterns override the foodType default:

```
Edge cases (checked first):
├─ cocoa, cacao, coffee, café → DIVERS
└─ prickly pear, figue de barbarie → VERGERS

Grains:
├─ wheat, flour, bread, pasta, biscuit, cake → BLE TENDRE
├─ rice, basmati → RIZ
├─ corn, maize, polenta, popcorn → MAIS GRAIN ET ENSILAGE
├─ barley, malt, beer → ORGE
└─ (other grains) → AUTRES CEREALES

Oilseeds/Nuts:
├─ sunflower, tournesol → TOURNESOL
├─ rapeseed, canola, colza → COLZA
├─ olive → OLIVIERS
├─ grape, wine, vinegar, raisin → VIGNES
├─ soy, sesame, flax, palm → AUTRES OLEAGINEUX
└─ (other nuts) → FRUITS A COQUES

Legumes (word boundary regex to avoid false positives):
├─ lentil, chickpea, haricot, fève, flageolet, lupin
├─ (red|white|lima|mung|broad|french|fava|kidney) bean
├─ (split|spring|winter|snow|garden) pea
└─ peas (but not "peaches")
```

### Reference File

`reference/cropgroup.csv` contains ~30 representative entries for matcher fallback. Most cases (~97%) are handled by pattern-based inference; the matcher is rarely used.

Animal products (meat, fish_seafood, dairy) do not have a cropGroup.

## Density Prediction

Density uses a hybrid approach to avoid false matches:

```
1. Text Match (exact or word boundary)
   ├─ "Apple" matches "apple" in density.csv → 0.9
   └─ Verify: query and match share a word → accept

2. Feature Similarity (cosine on 48-dim vector)
   ├─ "Amaranth" matches "Lard" with similarity 1.0
   └─ Verify: "amaranth" ∩ "lard" = ∅ → reject!

3. FoodType Default (when no valid text match)
   └─ "Amaranth" → grain → 0.75
```

### The Sparse Vector Problem

Items without FoodOn/regex matches get identical sparse feature vectors (only 2 dimensions non-zero). This causes unrelated items to match with 1.0 cosine similarity.

**Solution**: Before accepting a match, verify that query and match share at least one significant word (>3 chars). If not, fall back to foodType-based defaults:

| FoodType | Default Density |
|----------|-----------------|
| vegetable | 0.90 |
| fruit | 0.85 |
| grain | 0.75 |
| meat | 1.05 |
| fish_seafood | 1.05 |
| dairy | 1.03 |
| nut_oilseed | 0.60 |
| spice_condiment | 0.50 |

Current distribution: ~46% direct matches, ~54% foodType defaults.

## InediblePart Prediction

InediblePart uses a 3-tier approach:

```
1. Keyword Detection (processing indicators)
   ├─ "fillet", "boneless" → 0.0 (bones removed)
   ├─ "shelled", "peeled" → 0.0 (shell/peel removed)
   ├─ "canned", "frozen" → 0.0 (pre-processed)
   ├─ "with shell", "in shell" → 0.50 (shell is inedible)
   └─ "with bone" → 0.20 (bone is inedible)

2. Matcher with Semantic Validation
   └─ Only accept if query and match share a word

3. FoodType + NovaGroup Defaults
   ├─ NOVA 2-4 (processed): 0.0 for most types
   └─ NOVA 1 (raw): foodType-specific values
```

### Default Values by FoodType (NOVA 1)

| FoodType | Default | Source |
|----------|---------|--------|
| meat | 0.05 | Mostly boneless (AGB ~0) |
| fish_seafood | 0.40 | AGB whole fish average |
| dairy | 0.0 | Always edible |
| grain | 0.0 | Always edible |
| vegetable | 0.20 | inedible_part.csv "fresh vegetable" |
| fruit | 0.20 | inedible_part.csv "fresh fruit" |
| nut_oilseed | 0.50 | AGB nuts in shell |
| spice_condiment | 0.0 | Always edible |

### NovaGroup Adjustment

Processed items (NOVA 2-4) typically have inedible parts removed:
- Fish fillet (NOVA 3) → 0.0 (not 0.40)
- Shelled nuts (NOVA 2) → 0.0 (not 0.50)
- Canned vegetables → 0.0 (not 0.15)

Current distribution: ~23% matcher matches, ~77% rules/defaults.

## rawToCookedRatio Prediction

rawToCookedRatio (cooked weight / raw weight) uses a 3-tier approach:

```
1. Keyword Detection (special cases)
   ├─ "dried", "dehydrated" → 4.0 (absorbs water)
   ├─ poultry (chicken, turkey, duck, broiler) → 0.755
   └─ offal (liver, kidney) → 0.730

2. Matcher with Semantic Validation
   └─ Only accept if query and match share a word

3. FoodType Defaults (Agribalyse/CIQUAL values)
```

### Default Values by FoodType

| FoodType | Ratio | Meaning | Source |
|----------|-------|---------|--------|
| vegetable | 0.856 | -14% weight | Agribalyse |
| fruit | 0.856 | -14% weight | Agribalyse |
| fish_seafood | 0.819 | -18% weight | Agribalyse |
| meat | 0.792 | -21% weight | Agribalyse (red meat) |
| grain | 2.259 | +126% weight | Agribalyse (cereals) |
| dairy | 1.0 | No change | - |
| nut_oilseed | 1.0 | No change | - |
| spice_condiment | 1.0 | No change | - |

Current distribution: ~15% matcher matches, ~85% rules/defaults.

## Example

Input: `{"name": "Salmon fillet", "activityName": "Salmon, fillet, at plant {NO}"}`

```
Step 1: Translate → "Salmon fillet" (already English)

Step 2: For each field, find best match:
  ├─ foodType:         fish_seafood (regex: is_fish pattern)
  ├─ novaGroup:        1 (rule: fresh_at_plant = minimal processing)
  ├─ processingState:  raw (derived from NOVA 1)
  ├─ categories:       animal_product (computed from foodType)
  ├─ cropGroup:        N/A (animal product)
  ├─ transportCooling: always (rule: NOVA 1 + fish = perishable)
  ├─ density:          1.05 (text match: "salmon" in "salmon")
  ├─ inediblePart:     0.0 (keyword: "fillet" detected)
  └─ rawToCookedRatio: 0.819 (foodType default: fish_seafood)

Output: {
  "foodType": "fish_seafood",
  "novaGroup": 1,
  "novaGroupMatch": {"rule": "fresh_at_plant → NOVA 1", "confidence": 0.85},
  "processingState": "raw",
  "transportCooling": "always",
  "density": 1.05,
  "inediblePart": 0.0,
  "rawToCookedRatio": 0.819
}
```

### Example: Sparse Vector Fallback

Input: `{"name": "Amaranth", "activityName": "Durum wheat grain, at farm gate {FR}"}`

```
Step 1: Translate → "Amaranth" (already English)

Step 2: For each field:
  ├─ foodType:         grain (from food_type.csv)
  ├─ novaGroup:        1 (rule: at_farm = unprocessed)
  ├─ processingState:  raw (derived from NOVA 1)
  ├─ categories:       grain_raw (computed)
  ├─ cropGroup:        AUTRES CEREALES (text match)
  ├─ transportCooling: none (rule: grain = non-perishable)
  ├─ density:          0.75 (foodType default - no text match found)
  │                    ↳ Matcher returned "Lard" but no shared words!
  ├─ inediblePart:     0.0 (foodType+NOVA default: grain=0.0)
  └─ rawToCookedRatio: 2.259 (foodType default: grain absorbs water)
```
