## Concentré de suspension de soufre micronisé par broyage humide

## 1. Objet du modèle
Ce document décrit un **procédé ACV** représentant la fabrication d’un **soufre micronisé formulé en Suspension Concentrate (SC)** de type *Thiopron Rainfree*, afin de pouvoir l’utiliser ensuite comme intrant (épandage) dans un itinéraire technique agricole.

Le procédé est construit avec des **données publiques** (composition, densité) et des **hypothèses de formulation** (dispersant, agents mouillants, rhéologie, antimousse, conservateur) basées sur des **recettes industrielles publiées pour des SC** et des **fiches techniques** (voir Bibliographie).

---

## 2. Produit de référence (output du procédé)
**Output du procédé :** `Thiopron-like sulfur SC, at plant`  
**Quantité de référence :** **1 L** de SC (produit fini, en vrac, sans emballage)

### 2.1. Composition de référence du produit
D’après la notice/fiches produit, Thiopron Rainfree est une **SC** contenant **825 g/L de soufre micronisé**, soit **57,3 % p/p**.  
- 825 g/L = **0,825 kg de soufre par litre de SC**  
Source : notice / fiche produit UPL.

### 2.2. Masse d’1 L de SC (densité)
La **densité** du produit (ou densité relative) est indiquée à **1,44 g/mL** (à 20 °C) dans la FDS, soit :  
- **1 L de SC = 1,44 kg de produit**

Cette densité sert uniquement à vérifier la cohérence entre “g/L” et “% p/p” :  
- fraction massique = 0,825 / 1,44 = **0,573** (57,3 % p/p)

---

## 3. Conséquences pour la modélisation “au champ”
Ce point est crucial pour ne pas mélanger “kg de produit SC” et “kg de soufre actif”.

- **1 L de SC contient 0,825 kg de soufre**
- donc pour apporter **1 kg de soufre actif**, il faut :  
  **1 / 0,825 = 1,212 L de SC**

Formules à réutiliser dans l’itinéraire technique :
- Soufre actif apporté (kg) = SC épandu (L) × 0,825
- SC nécessaire (L) = Soufre actif visé (kg) / 0,825

---

## 4. Frontière du système (system boundary)
Procédé de fabrication **“at plant gate”** (sortie usine), incluant :
- matières premières (soufre, eau, additifs),
- énergie de micronisation / dispersion (électricité).

Exclu par défaut (optionnel si besoin) :
- emballage (bidons, palettes),
- nettoyage/CIP et effluents,
- bâtiments/infrastructure de l’atelier,
- transports spécifiques (déjà inclus dans les `market for …` ecoinvent).

---

## 5. Choix d’inventaire (technosphère) — pour 1 L de SC produit

### 5.1. Principe
Les additifs sont représentatifs d’une **SC “wet milled”** :
- dispersant (stabilisation de la suspension),
- agent(s) mouillant(s) (mouillage du soufre, aide au broyage),
- système rhéologique (anti-sédimentation, stabilité) : smectite/bentonite + biopolymère type xanthan (proxy),
- antimousse (faible dose),
- conservateur/biocide (faible dose),
- glycol (humectant/antigel, contrôle évaporation).

Les niveaux d’additifs sont cohérents avec des **recettes industrielles publiées pour SC** et la pratique de formulation (voir références Vanderbilt/Stepan/Croda/Evonik).

### 5.2. Liste des activités ecoinvent (inputs)
> Unités : kg sauf l’électricité (kWh).  
> Les quantités ci-dessous sont calibrées pour obtenir ~**1,44 kg** de mélange total par litre (densité cible), dont **0,825 kg de soufre**.

- `sulfur//[GLO] market for sulfur` : **0.825 kg**  
- `tap water//[GLO] market group for tap water` : **0.489 kg**  
- `Propylene glycol, liquid {GLO}| market for | Cut-off, U` : **0.0825 kg**  
- `Sodium cumenesulfonate {GLO}| market for sodium cumenesulfonate | Cut-off, U` : **0.0330 kg**  
- `Bentonite {GLO}| market for bentonite | Cut-off, U` : **0.00413 kg**  
- `Ethoxylated alcohol (AE11) {GLO}| market for ethoxylated alcohol (AE11) | Cut-off, U` : **0.00248 kg**  
- `Non-ionic surfactant {GLO}| market for non-ionic surfactant | Cut-off, U` : **0.000825 kg**  *(proxy antimousse)*  
- `Polyester-complexed starch biopolymer {GLO}| market for polyester-complexed starch biopolymer | Cut-off, U` : **0.00165 kg**  *(proxy rhéologie type xanthan)*  
- `Chemical, organic {GLO}| chemical production, organic | Cut-off, U` : **0.000825 kg**  *(proxy conservateur/biocide, type BIT)*  
- `Electricity, medium voltage {GLO}| market group for electricity, medium voltage | Cut-off, U` : **0.495 kWh**

**Contrôle bilan masse (hors électricité)** :  
Somme ≈ 1.44 kg par L (attendu d’après la densité).

---

## 6. Justification des choix

### 6.1. Pourquoi “SC” implique un broyage humide et des additifs
Une SC est une **suspension de solides insolubles** dans l’eau, nécessitant des **agents mouillants/dispersants** et des **agents de stabilisation** (rhéologie, anti-sédimentation).  
- Définition/description SC : Stepan + Croda.

### 6.2. Pourquoi utiliser `tap water` (et pas “river/lake”)
Dans ecoinvent, l’eau de procédé achetée est modélisée via les marchés `tap water`, qui incluent traitement + distribution + pertes.  
- Source : ecoinvent Knowledge Base “Water Supply”.

### 6.3. Pourquoi `market for …` (soufre, eau, etc.)
Un dataset “market” représente le **mix de consommation** et le transfert du produit vers l’utilisateur (incluant, si pertinent, des pertes de transport).  
- Source : ecoinvent Knowledge Base “Market activities”.

### 6.4. Origine des ordres de grandeur d’additifs
- La recette “wet milled SC” publiée par Vanderbilt (intro SC) fournit un **exemple quantifié** (dispersant, glycol, smectite/clay, xanthan, conservateur, surfactants).  
- Vanderbilt “Formulations No. 921” documente aussi la pratique : **clay + gum**, et l’ajout de la gomme **en fin de broyage** pour éviter la dégradation au cisaillement.

Ces documents ne sont pas “Thiopron”, mais servent de **base de formulation industrielle** pour construire un proxy transparent.

### 6.5. Pourquoi bentonite comme proxy “magnesium aluminum silicate”
Des SDS/communications Vanderbilt associent les “magnesium aluminum silicate” commerciaux (smectite clay) à des clays de type **bentonite/smectite**, justifiant l’usage de `market for bentonite` comme proxy.

### 6.6. Pourquoi un antimousse séparé
Les fiches techniques d’antimousses moléculaires de type **acetylenic diol** (ex. Surfynol DF-110 D) recommandent des niveaux d’usage typiquement de l’ordre de **0,1–1,0%** selon formulation; ici, on retient une dose faible (proxy) car l’antimousse est rarement l’agent dominant.  
- Source : Evonik Surfynol DF-110 D TDS.

### 6.7. Pourquoi un conservateur/biocide
Les SC aqueuses peuvent nécessiter un **conservateur** pour éviter la dégradation microbienne des ingrédients organiques; les exemples SC (Vanderbilt) l’incluent, et des FDS Thiopron mentionnent un biocide de type **BIT**.  
- Sources : Vanderbilt “Intro SC” + FDS Thiopron Rainfree.

### 6.8. Électricité de broyage
L’électricité (0,495 kWh/L) est une hypothèse centrale correspondant à ~0,6 kWh/kg soufre (conversion via 0,825 kg S/L).  
La littérature industrielle sur le broyage humide indique des ordres de grandeur “centaines de kWh/tonne” selon la finesse visée et la ligne de production; la valeur doit être traitée en **sensibilité** (plus fin = plus d’énergie).  
- Sources : NETZSCH ProPhi (exemples d’énergie spécifique) + littérature sur stirred milling (Patino et al., 2022).

---

## 7. Points de sensibilité (à documenter)
1. **Électricité de micronisation** (kWh/L) : paramètre le plus influent et le plus incertain.  
2. **Choix du proxy rhéologie** : idéalement remplacer le “starch biopolymer” par une activité “xanthan gum” si disponible.
3. **Identité/quantité du dispersant** : sodium cumenesulfonate = proxy; alternatives possibles (lignosulfonates, naphthalene sulfonate condensate) si mieux disponibles.  
4. **Emballage** : à ajouter si le périmètre inclut “produit conditionné”.

---

## 8. Bibliographie (URLs)
- UPL France — Thiopron Rainfree (composition 825 g/L; 57,3% p/p; Formulation SC)  
  https://www.uplcorp.com/fr/produit-d%C3%A9tails/thiopron-rainfree

- UPL France — Notice Thiopron Rainfree 2025 (825 g/L; 57,3% p/p; contexte technique)  
  https://fr.uplcorp.com/download_links/CM6JCI5Fknm6YZtMTCJOcUrnaXpGlhTCOAwEksxr.pdf

- FDS Thiopron Rainfree (densité relative 1,44 g/mL; informations physico-chimiques)  
  https://idfmarketplace.blob.core.windows.net/public/FDS/sv/THIOPRON_RAINFREE_FDS.pdf

- Stepan — Suspension Concentrates (SC) (description/definition des SC)  
  https://www.stepan.com/content/dam/stepan-dot-com/webdam/website-product-documents/literature/agricultural-solutions/Copy-of-SCv.14.pdf

- Croda Agriculture — Suspension concentrate (SC) (description SC / flowables)  
  https://www.crodaagriculture.com/en-gb/applications/suspension-concentrate

- ecoinvent Knowledge Base — Market activities (définition “market dataset”)  
  https://support.ecoinvent.org/market-activities

- ecoinvent Knowledge Base — Water supply (tap water markets: infrastructure, pertes, distribution)  
  https://support.ecoinvent.org/water-supply

- Vanderbilt Minerals — Introduction to Suspension Concentrates (recette/exemple SC “wet milled”)  
  https://www.vanderbiltminerals.com/resources/Intro_Suspension_Concentrates_Web.pdf

- Vanderbilt Minerals — Formulations No. 921 (SC : clay + gum; ajout de la gomme en fin de broyage)  
  https://www.vanderbiltminerals.com/resources/921_Crop_Protection_Web.pdf

- Evonik — SURFYNOL DF-110 D TDS (antimousse moléculaire; recommandations d’usage)  
  EN: https://products.evonik.com/assets/or/ld/SURFYNOL_DF_110_D_TDS_EN_EN_TDS_PV_52042444_en_GB_WORLD.pdf  
  FR: https://products.evonik.com/assets/or/ld/SURFYNOL_DF_110_D_TDS_FR_FR_TDS_PV_52042444_fr_FR_WORLD.pdf

- NETZSCH — ProPhi pre-grinding unit (exemples d’énergie spécifique en broyage humide)  
  https://grinding.netzsch.com/en/products-and-solutions/wet-grinding/pre-grinding-mill-prophi

- Patino et al., 2022 (stirred milling; ordres de grandeur kWh/t et tailles micrométriques)  
  https://www.sciencedirect.com/science/article/abs/pii/S0032591022002881

