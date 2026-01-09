## Micronized sulfur suspension concentrate produced by wet milling

## 1. Purpose of the model
This document describes an **LCA process** representing the production of a **micronized sulfur formulated as a Suspension Concentrate (SC)**, so that it can later be used as an input (field application) in an agricultural crop management plan.

The process is built from **publicly available data** (composition, density) and **formulation assumptions** (dispersant, wetting agents, rheology control, defoamer, preservative) based on **published industrial SC recipes** and **technical datasheets** (see Bibliography).

---

## 2. Reference product (process output)
**Process output:** `micronized sulfur suspension concentrate production`  
**Reference amount:** **1 L** of SC (finished product, bulk, no packaging)

### 2.1. Reference product composition
According to some product notice/datasheets, an **SC** contains **825 g/L of micronized sulfur**, i.e. **57.3% w/w**.  
- 825 g/L = **0.825 kg of sulfur per liter of SC**  
Source: UPL notice / product sheet.

### 2.2. Mass of 1 L of SC (density)
The product **density** (or relative density) is reported as **1.44 g/mL** (at 20 °C) in the SDS, i.e.:  
- **1 L of SC = 1.44 kg of product**

This density is used only to check consistency between “g/L” and “% w/w”:  
- mass fraction = 0.825 / 1.44 = **0.573** (57.3% w/w)

---

## 3. Implications for “field” modelling
This point is critical to avoid mixing “kg of SC product” and “kg of active sulfur”.

- **1 L of SC contains 0.825 kg of sulfur**
- therefore, to apply **1 kg of active sulfur**, you need:  
  **1 / 0.825 = 1.212 L of SC**

Formulas to reuse in the crop management plan:
- Active sulfur applied (kg) = SC applied (L) × 0.825
- Required SC (L) = Target active sulfur (kg) / 0.825

---

## 4. System boundary
Manufacturing process **“at plant gate”** (factory gate), including:
- raw materials (sulfur, water, additives),
- micronization/dispersion energy (electricity).

Excluded by default (optional if needed):
- packaging (jerrycans, pallets),
- cleaning/CIP and effluents,
- buildings/workshop infrastructure,
- specific transports (already included in ecoinvent `market for …` datasets).

---

## 5. Inventory choices (technosphere) — per 1 L of SC produced

### 5.1. Principle
The additives are representative of a **wet-milled SC**:
- dispersant (suspension stabilization),
- wetting agent(s) (sulfur wetting, milling assistance),
- rheology system (anti-settling, stability): smectite/bentonite + xanthan-type biopolymer (proxy),
- defoamer (low dose),
- preservative/biocide (low dose),
- glycol (humectant/antifreeze, evaporation control).

Additive levels are consistent with **published industrial SC recipes** and formulation practice (see Vanderbilt/Stepan/Croda/Evonik references).

### 5.2. List of ecoinvent activities (inputs)
> Units: kg except electricity (kWh).  
> The quantities below are calibrated to obtain ~**1.44 kg** total mixture per liter (target density), including **0.825 kg sulfur**.

- `sulfur//[GLO] market for sulfur` : **0.825 kg**  
- `tap water//[GLO] market group for tap water` : **0.489 kg**  
- `Propylene glycol, liquid {GLO}| market for | Cut-off, U` : **0.0825 kg**  
- `Sodium cumenesulfonate {GLO}| market for sodium cumenesulfonate | Cut-off, U` : **0.0330 kg**  
- `Bentonite {GLO}| market for bentonite | Cut-off, U` : **0.00413 kg**  
- `Ethoxylated alcohol (AE11) {GLO}| market for ethoxylated alcohol (AE11) | Cut-off, U` : **0.00248 kg**  
- `Non-ionic surfactant {GLO}| market for non-ionic surfactant | Cut-off, U` : **0.000825 kg**  *(defoamer proxy)*  
- `Polyester-complexed starch biopolymer {GLO}| market for polyester-complexed starch biopolymer | Cut-off, U` : **0.00165 kg**  *(xanthan-type rheology proxy)*  
- `Chemical, organic {GLO}| chemical production, organic | Cut-off, U` : **0.000825 kg**  *(preservative/biocide proxy, BIT-type)*  
- `Electricity, medium voltage {GLO}| market group for electricity, medium voltage | Cut-off, U` : **0.495 kWh**

**Mass balance check (excluding electricity):**  
Sum ≈ 1.44 kg per L (as expected from the density).

---

## 6. Rationale for the choices

### 6.1. Why “SC” implies wet milling and additives
An SC is a **suspension of insoluble solids** in water, requiring **wetting/dispersing agents** and **stabilizing agents** (rheology, anti-settling).  
- SC definition/description: Stepan + Croda.

### 6.2. Why use `tap water` (and not “river/lake”)
In ecoinvent, purchased process water is modeled via `tap water` markets, which include treatment + distribution + losses.  
- Source: ecoinvent Knowledge Base “Water Supply”.

### 6.3. Why `market for …` (sulfur, water, etc.)
A “market” dataset represents the **consumption mix** and the transfer of the product to the user (including, where relevant, transport losses).  
- Source: ecoinvent Knowledge Base “Market activities”.

### 6.4. Origin of additive order-of-magnitude values
- The Vanderbilt published “wet milled SC” recipe (Intro SC) provides a **quantified example** (dispersant, glycol, smectite/clay, xanthan, preservative, surfactants).  
- Vanderbilt “Formulations No. 921” also documents the practice: **clay + gum**, and adding the gum **after milling** to avoid shear degradation.

These documents serve as an **industrial formulation baseline** to build a transparent proxy.

### 6.5. Why bentonite as a proxy for “magnesium aluminum silicate”
Vanderbilt SDS/communications link commercial “magnesium aluminum silicate” (smectite clay) to **bentonite/smectite** clays, justifying the use of `market for bentonite` as a proxy.

### 6.6. Why a separate defoamer
Technical datasheets for molecular defoamers of the **acetylenic diol** type (e.g., Surfynol DF-110 D) recommend typical use levels on the order of **0.1–1.0%** depending on the formulation; here, a low dose is retained (proxy) because defoamers are rarely dominant ingredients.  
- Source: Evonik Surfynol DF-110 D TDS.

### 6.7. Why a preservative/biocide
Aqueous SCs may require a **preservative** to prevent microbial degradation of organic ingredients; SC examples (Vanderbilt) include one, and Thiopron SDS documents mention a BIT-type biocide.  
- Sources: Vanderbilt “Intro SC” + Thiopron Rainfree SDS.

### 6.8. Milling electricity
Electricity (0.495 kWh/L) is a central assumption corresponding to ~0.6 kWh/kg sulfur (conversion via 0.825 kg S/L).  
Industrial wet milling literature indicates “hundreds of kWh/tonne” orders of magnitude depending on target fineness and production line; the value should be treated as a **sensitivity parameter** (finer PSD = more energy).  
- Sources: NETZSCH ProPhi (specific energy examples) + stirred milling literature (Patino et al., 2022).

---

## 7. Sensitivity points (to document)
1. **Micronization electricity** (kWh/L): most influential and most uncertain parameter.  
2. **Choice of rheology proxy**: ideally replace the “starch biopolymer” with a “xanthan gum” activity if available.
3. **Dispersant identity/amount**: sodium cumenesulfonate is a proxy; possible alternatives (lignosulfonates, naphthalene sulfonate condensate) if better available.  
4. **Packaging**: add if the scope includes “packaged product”.

---

## 8. Bibliography (URLs)
- UPL France — Thiopron Rainfree (composition 825 g/L; 57.3% w/w; SC formulation)  
  https://www.uplcorp.com/fr/produit-d%C3%A9tails/thiopron-rainfree

- UPL France — Thiopron Rainfree Notice 2025 (825 g/L; 57.3% w/w; technical context)  
  https://fr.uplcorp.com/download_links/CM6JCI5Fknm6YZtMTCJOcUrnaXpGlhTCOAwEksxr.pdf

- Thiopron Rainfree SDS (relative density 1.44 g/mL; physico-chemical information)  
  https://idfmarketplace.blob.core.windows.net/public/FDS/sv/THIOPRON_RAINFREE_FDS.pdf

- Stepan — Suspension Concentrates (SC) (SC definition/description)  
  https://www.stepan.com/content/dam/stepan-dot-com/webdam/website-product-documents/literature/agricultural-solutions/Copy-of-SCv.14.pdf

- Croda Agriculture — Suspension concentrate (SC) (SC / flowables description)  
  https://www.crodaagriculture.com/en-gb/applications/suspension-concentrate

- ecoinvent Knowledge Base — Market activities (definition of “market dataset”)  
  https://support.ecoinvent.org/market-activities

- ecoinvent Knowledge Base — Water supply (tap water markets: infrastructure, losses, distribution)  
  https://support.ecoinvent.org/water-supply

- Vanderbilt Minerals — Introduction to Suspension Concentrates (wet-milled SC recipe/example)  
  https://www.vanderbiltminerals.com/resources/Intro_Suspension_Concentrates_Web.pdf

- Vanderbilt Minerals — Formulations No. 921 (SC: clay + gum; gum addition after milling)  
  https://www.vanderbiltminerals.com/resources/921_Crop_Protection_Web.pdf

- Evonik — SURFYNOL DF-110 D TDS (molecular defoamer; recommended use levels)  
  EN: https://products.evonik.com/assets/or/ld/SURFYNOL_DF_110_D_TDS_EN_EN_TDS_PV_52042444_en_GB_WORLD.pdf  
  FR: https://products.evonik.com/assets/or/ld/SURFYNOL_DF_110_D_TDS_FR_FR_TDS_PV_52042444_fr_FR_WORLD.pdf

- NETZSCH — ProPhi pre-grinding unit (wet grinding specific energy examples)  
  https://grinding.netzsch.com/en/products-and-solutions/wet-grinding/pre-grinding-mill-prophi

- Patino et al., 2022 (stirred milling; kWh/t orders of magnitude and micrometric sizes)  
  https://www.sciencedirect.com/science/article/abs/pii/S0032591022002881

