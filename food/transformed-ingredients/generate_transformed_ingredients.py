"""Generate transformed ingredient variants by substituting raw ingredient activities.

Strategy
--------
Ecobalyse tracks raw ingredients in three variants per ingredient family:
  - FR  (alias suffix -fr):      French production, Agribalyse reference activity
  - OI  (alias suffix -default): Import origin, may use Ecoinvent or other DB
  - BIO (alias suffix -organic): Organic, Agribalyse or Ginko organic activity

Agribalyse also contains many *transformed* products (frozen vegetables, purees,
juices, at-plant products ...) that use one of these raw ingredient activities
somewhere in their upstream supply chain — either directly or via an intermediate
market/consumption-mix activity.

The goal is to generate new transformed ingredient variants:
for every transformed product T that uses raw variant V_src, we create a new
version of T that uses each other raw variant V_tgt instead of V_src.

Algorithm:
1. Walk lci_catalog/<source-slug>/<alias>.json (the per-activity layout
   that replaced the monolithic activities.json) to build ingredient
   groups: for each alias prefix (e.g. "radish") collect all known
   variants (radish-fr, radish-organic, radish-default, …) with their
   upstream LCA activityName and database.
2. For each variant in each group, call VoLCA get_consumers with
   include_edges=True and no server-side preset. This returns the full
   BFS subgraph of transitive consumers plus every technosphere edge
   between reachable nodes. Transformed-product filtering happens
   client-side from ConsumerResult.classifications (Category ∈
   TRANSFORMED_CATEGORIES + Category type = material). Keeping the
   unfiltered response preserves consumption-mix metadata needed when
   walking paths.
3. Collect the union of all transformed consumers across all variants in
   the group. For each consumer C, record which variant(s) it uses.
4. For each consumer C and each variant V_tgt that C does NOT yet use:
   - Walk the per-supplier edge subgraph locally (BFS from V_src.process_id
     to C) to reconstruct the shortest path C → ... → V_src. No extra
     HTTP round-trip.
   - Derive the from_existing block: existingActivity=path[0],
     upstreamPath=path[1:-1], replace.from=path[-1]=V_src, replace.to=V_tgt.
   - Special case: if a hop on the path has
     Category = "Agricultural\\Food\\Consumption mixes", replace the mix
     itself (not the leaf ingredient inside it), so the whole sourcing
     blend is swapped for V_tgt.
5. Also generate an activities.json entry for each new transformed activity.
   Physical metadata (ingredientDensity, transportCooling, cropGroup,
   ingredientCategories) is *predicted* from the transformed-product name
   via ../metadata/predict.py (FoodOn ontology + nearest-neighbour). The
   English activity name is translated to French with Helsinki-NLP/opus-mt
   for the displayName. inediblePart is hardcoded to 0 (transformation has
   already removed the inedible fraction) and rawToCookedRatio to 1.0.
   Only scenario and defaultOrigin come from the target raw variant.

Outputs two files:
  generated_activities_to_create.json  — from_existing blocks to merge into activities_to_create.json
  generated_activities.json            — new entries to merge into activities.json

Usage:
    python generate_transformed_ingredients.py \\
        --activities /path/to/ecobalyse-data/lci_catalog \\
        --output-dir /path/to/output \\
        [--volca-url http://localhost:8080]
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from volca import Client
from volca.types import ConsumerResult, SupplyChainEdge

# predict.py and lci_catalog.py live in a sibling package that is not
# installed as a module; expose it via sys.path so we can import them.
_METADATA_DIR = Path(__file__).resolve().parent.parent / "metadata"
if str(_METADATA_DIR) not in sys.path:
    sys.path.insert(0, str(_METADATA_DIR))
from predict import Predictor  # noqa: E402
from lci_catalog import load_lci_catalog, merge_activities  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# VoLCA database name for the main Agribalyse database (where transformed products live)
AGRIBALYSE_DB = "agribalyse-3-2"

# Cached predictor pickle (sibling to this script, git-ignored)
PREDICTOR_CACHE = Path(__file__).resolve().parent / ".predictor.pkl"

# Mapping from activities.json "source" field to VoLCA database name
DB_MAP: dict[str, str] = {
    "Agribalyse 3.2": AGRIBALYSE_DB,
    "Ecoinvent 3.9.1": "ecoinvent-3-9-1-adapted",
    "Ecoinvent 3.11": "ecoinvent-3-11-adapted",
    "Ginko 2025": "ginko-2025-v2",
    "WFLDB": "wfldb",
    "PastoEco": "pastoeco",
}


# Per-variant info keyed by alias suffix (the only field unique across variants;
# scenario collapses UE/OI/NUE into "import"). Order matters: longest suffix
# first so endswith() matches "-non-eu" before "-eu".
# (alias_suffix, short_suffix_for_new_aliases, French display suffix)
VARIANT_INFO: list[tuple[str, str, str]] = [
    ("-non-eu", "non-eu", " HORS UE"),
    ("-organic", "organic", " Bio"),
    ("-default", "default", " Origine Inconnue"),
    ("-eu", "eu", " UE"),
    ("-fr", "fr", " FR"),
]
VARIANT_SUFFIXES = [s for s, _, _ in VARIANT_INFO]


def variant_short_suffix(alias: str) -> str:
    """e.g. 'carrot-non-eu' -> 'non-eu'."""
    for s, short, _ in VARIANT_INFO:
        if alias.endswith(s):
            return short
    raise ValueError(f"alias {alias!r} has no known variant suffix")


def variant_display_suffix(alias: str) -> str:
    """e.g. 'carrot-non-eu' -> ' HORS UE'."""
    for s, _, disp in VARIANT_INFO:
        if alias.endswith(s):
            return disp
    return ""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class VariantInfo:
    alias: str  # e.g. "carrot-organic"
    scenario: str  # "reference", "organic", "import"
    default_origin: str  # e.g. "France"
    activity_name: (
        str  # top-level activityName (same for all metadata within one entry)
    )
    source: str  # e.g. "Agribalyse 3.2", "Ecoinvent 3.9.1"
    location: str | None  # top-level location field (used when searching VoLCA)
    raw_meta: dict  # full metadata dict (for copying physical properties)
    process_id: str | None = None  # resolved after VoLCA lookup


# ---------------------------------------------------------------------------
# Alias / group helpers
# ---------------------------------------------------------------------------


def parse_ecoinvent_name(full_name: str) -> tuple[str, str | None]:
    """Parse Ecoinvent long format 'Product {Geo}| activity name | Cut-off, U'.
    Returns (search_name, geo) where search_name is the activity name part,
    or the full name if it doesn't match the pattern."""
    m = re.match(r"^.+\{(.+?)\}\|\s*(.+?)\s*\|\s*Cut-off,\s*\w+$", full_name)
    if m:
        return m.group(2), m.group(1)
    return full_name, None


def strip_variant_suffix(alias: str) -> str | None:
    """Return the base ingredient name by stripping a known variant suffix, or None."""
    for suffix in VARIANT_SUFFIXES:
        if alias.endswith(suffix):
            return alias[: -len(suffix)]
    return None


CORRECTIONS_CSV = Path(__file__).parent / "translation_corrections.csv"
TRANSLATION_CACHE = Path(__file__).parent / ".translation_cache.json"


def _load_corrections() -> dict[str, str]:
    """Load post-translation corrections from CSV."""
    import csv

    if not CORRECTIONS_CSV.exists():
        return {}
    with CORRECTIONS_CSV.open(newline="", encoding="utf-8") as f:
        return {row["wrong"]: row["correct"] for row in csv.DictReader(f)}


def _load_translation_cache() -> dict[str, str]:
    if not TRANSLATION_CACHE.exists():
        return {}
    with TRANSLATION_CACHE.open(encoding="utf-8") as f:
        return json.load(f)


def _save_translation_cache(cache: dict[str, str]) -> None:
    with TRANSLATION_CACHE.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False, sort_keys=True)


def translate_en_to_fr(names: list[str]) -> list[str]:
    """Translate English food product names to French using Helsinki-NLP/opus-mt-en-fr.

    Raw translations (pre-corrections) are persisted to .translation_cache.json
    so repeat runs only load MarianMT for genuinely new names. Post-translation
    corrections from translation_corrections.csv are re-applied every run, so
    tweaking the CSV takes effect without invalidating the cache.
    """
    if not names:
        return []

    cache = _load_translation_cache()
    missing = [n for n in names if n not in cache]

    if missing:
        from transformers import MarianMTModel, MarianTokenizer

        model_name = "Helsinki-NLP/opus-mt-en-fr"
        tokenizer = MarianTokenizer.from_pretrained(model_name)
        model = MarianMTModel.from_pretrained(model_name)
        tokens = tokenizer(missing, return_tensors="pt", padding=True, truncation=True)
        translated = model.generate(**tokens)
        new_results = tokenizer.batch_decode(translated, skip_special_tokens=True)
        for en, fr in zip(missing, new_results):
            cache[en] = fr
        _save_translation_cache(cache)

    corrections = _load_corrections()
    return [corrections.get(cache[n], cache[n]) for n in names]


# Patterns for LCA jargon segments to drop from activity names
_JARGON_RE = re.compile(
    r"^at (processing|plant|industrial mill|orchard|farm)"
    r"|production"
    r"|^for "
    r"|^from "
    r"|^NFC$|^1L$|^1kg of |^conventional$|^national average$",
    re.IGNORECASE,
)


def extract_short_name(activity_name: str) -> str:
    """Extract a human-readable product name from an Agribalyse activity name.

    Strips LCA jargon (location, production method, packaging) and geo codes,
    keeping only segments that describe the product itself."""
    clean = re.sub(r"\s*\{[^}]+\}\s*U$", "", activity_name).strip()
    segments = [s.strip() for s in clean.split(",")]
    kept = [s for s in segments if not _JARGON_RE.search(s)]
    return ", ".join(kept) if kept else segments[0]


# `for X` / `from X` qualifiers normally count as jargon and get stripped, but
# they're often the only thing distinguishing a transformed product from its
# raw ingredient (e.g. "Lemon, for grated carrots" vs. plain "Lemon"). Used
# only as a collision fallback in `extract_long_name`.
_QUALIFIER_RE = re.compile(r"^(for|from) ", re.IGNORECASE)


def extract_long_name(activity_name: str) -> str:
    """Like extract_short_name but keeps `for X` / `from X` qualifier segments.

    Use as a fallback when the short name's translation would collide with an
    existing displayName in lci_catalog/ — the qualifier disambiguates the
    transformed product from its raw counterpart.
    """
    clean = re.sub(r"\s*\{[^}]+\}\s*U$", "", activity_name).strip()
    segments = [s.strip() for s in clean.split(",")]
    kept = [
        s for s in segments
        if not _JARGON_RE.search(s) or _QUALIFIER_RE.match(s)
    ]
    return ", ".join(kept) if kept else segments[0]


def is_proxy(base_name: str, activity_name: str) -> bool:
    """True if the activity is a proxy (its name doesn't match the ingredient group)."""
    first_word = base_name.split("-")[0].lower()
    act_first_segment = activity_name.split(",")[0].lower()
    return first_word not in act_first_segment


def slugify(text: str) -> str:
    """Convert an activity name to a simple hyphenated slug for use in aliases."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


# ---------------------------------------------------------------------------
# Step 1: Parse activities.json
# ---------------------------------------------------------------------------


def parse_ingredient_groups(
    activities_json: list[dict],
) -> dict[str, list[VariantInfo]]:
    """Return {base_name: [VariantInfo, ...]} for all food ingredient variants."""
    variants: list[VariantInfo] = []

    for entry in activities_json:
        if "food" not in entry.get("scopes", []):
            continue
        if "ingredient" not in entry.get("categories", []):
            continue

        activity_name = entry.get("activityName", "")
        source = entry.get("source", "Agribalyse 3.2")
        location = entry.get("location")

        for meta in entry.get("metadata", []):
            alias = meta.get("alias", "")
            scenario = meta.get("scenario")
            if not scenario:
                continue
            if strip_variant_suffix(alias) is None:
                continue  # skip -2025 and other non-standard aliases
            if "food" not in meta.get("scopes", []):
                continue
            variants.append(
                VariantInfo(
                    alias=alias,
                    scenario=scenario,
                    default_origin=meta.get("defaultOrigin", ""),
                    activity_name=activity_name,
                    source=source,
                    location=location,
                    raw_meta=meta,
                )
            )

    groups: dict[str, list[VariantInfo]] = defaultdict(list)
    for v in variants:
        base = strip_variant_suffix(v.alias)
        groups[base].append(v)  # type: ignore[index]

    return dict(groups)


def filter_actionable_groups(
    groups: dict[str, list[VariantInfo]],
) -> dict[str, list[VariantInfo]]:
    """Keep only groups that have at least one reference AND one non-reference variant
    with a DIFFERENT activityName (substitution is meaningful)."""
    result = {}
    for base, vs in groups.items():
        refs = [v for v in vs if v.scenario == "reference"]
        non_refs = [v for v in vs if v.scenario != "reference"]
        if not refs or not non_refs:
            continue
        ref_names = {v.activity_name for v in refs}
        if any(v.activity_name not in ref_names for v in non_refs):
            result[base] = vs
    return result


# ---------------------------------------------------------------------------
# Step 2: Resolve process_ids via VoLCA
# ---------------------------------------------------------------------------


def resolve_process_ids(
    groups: dict[str, list[VariantInfo]],
    client: Client,
    native_dbs: set[str],
) -> None:
    """Resolve process_id for each variant in-place. Unresolved variants get None.
    native_dbs: databases with native naming (EcoSpold 2) — SimaPro long names
    are parsed to extract the real activity name. Other databases keep SimaPro names as-is."""
    cache: dict[tuple[str, str], str | None] = {}

    for variants in groups.values():
        for v in variants:
            key = (v.source, v.activity_name)
            if key in cache:
                v.process_id = cache[key]
                continue

            db_name = DB_MAP.get(v.source)
            if not db_name:
                print(
                    f"  [WARN] Unknown source database '{v.source}' for alias {v.alias!r}",
                    file=sys.stderr,
                )
                cache[key] = None
                v.process_id = None
                continue

            db_client = client.use(db_name)
            if db_name in native_dbs:
                search_name, parsed_geo = parse_ecoinvent_name(v.activity_name)
            else:
                search_name, parsed_geo = v.activity_name, None
            search_kwargs = {"name": search_name}
            geo = v.location or parsed_geo
            if geo:
                search_kwargs["geo"] = geo
            results = db_client.search_activities(exact=True, **search_kwargs)
            pid = results[0].process_id if results else None
            if pid is None:
                print(
                    f"  [WARN] Could not resolve '{v.activity_name}' in {db_name!r}",
                    file=sys.stderr,
                )
            cache[key] = pid
            v.process_id = pid


# ---------------------------------------------------------------------------
# Step 3c: Find transformed consumers for each variant
# ---------------------------------------------------------------------------


TRANSFORMED_CATEGORIES = {
    "Agricultural\\Food\\Transformation",
    "Agricultural\\Food\\Cheese production",
}
MIX_CATEGORY = "Agricultural\\Food\\Consumption mixes"


def _is_transformed_product(c: ConsumerResult) -> bool:
    """Client-side replication of the `transformed` preset (volca.toml):
    Category ∈ TRANSFORMED_CATEGORIES AND Category type = material."""
    cls = c.classifications
    return (
        cls.get("Category") in TRANSFORMED_CATEGORIES
        and cls.get("Category type") == "material"
    )


def collect_consumers(
    variants: list[VariantInfo],
    client: Client,
    food_transform_dbs: set[str],
    max_depth: int = 2,
) -> tuple[
    dict[str, set[str]],
    dict[str, ConsumerResult],
    dict[str, list[SupplyChainEdge]],
]:
    """Find transformed-product consumers for each variant and retain the
    traversal subgraph for local path reconstruction.

    One ``get_consumers(include_edges=True)`` call per variant, without a
    server-side preset — transformed-product filtering happens client-side
    using ConsumerResult.classifications. This keeps all intermediate
    subgraph nodes (notably consumption mixes) available for path-walking,
    which the preset-filtered list would otherwise omit.

    Returns:
      consumer_to_found_variants: {consumer_pid: {variant_alias, ...}} — only
        transformed products matching the preset classification filter.
      node_info: {pid: ConsumerResult} — every node reached in the BFS, so
        the caller can look up name + classifications for any hop on a path.
      supplier_edges: {supplier_pid: [SupplyChainEdge, ...]} — per-supplier
        subgraph used by ``shortest_path_via_edges``.
    """
    consumer_to_found_variants: dict[str, set[str]] = defaultdict(set)
    node_info: dict[str, ConsumerResult] = {}
    supplier_edges: dict[str, list[SupplyChainEdge]] = {}

    for v in variants:
        if v.process_id is None:
            continue
        db_name = DB_MAP.get(v.source)
        if db_name not in food_transform_dbs:
            continue  # pure Ecoinvent variants have no Agribalyse consumers

        db_client = client.use(db_name)
        try:
            resp = db_client.get_consumers(
                v.process_id,
                max_depth=max_depth,
                include_edges=True,
            )
        except Exception as exc:
            print(
                f"  [WARN] get_consumers failed for {v.alias!r} in {db_name!r}: {exc}",
                file=sys.stderr,
            )
            continue
        supplier_edges[v.process_id] = resp.edges
        for c in resp.consumers:
            node_info[c.process_id] = c
            if "2025" in c.name:
                continue  # skip -2025 organic variants (not stable Agribalyse activities)
            if _is_transformed_product(c):
                consumer_to_found_variants[c.process_id].add(v.alias)

    return dict(consumer_to_found_variants), node_info, supplier_edges


@dataclass
class LocalStep:
    """Step in a locally-reconstructed consumer→supplier path. Carries exactly
    what make_from_existing needs: .name for the replacement block, and
    .classifications for consumption-mix detection."""

    process_id: str
    name: str
    classifications: dict[str, str]


def shortest_path_via_edges(
    supplier_pid: str,
    consumer_pid: str,
    edges: list[SupplyChainEdge],
    node_info: dict[str, ConsumerResult],
    supplier_step: LocalStep,
) -> list[LocalStep] | None:
    """BFS from supplier_pid following edges (supplier → consumer) until
    consumer_pid is reached. Returns a path ordered consumer → … → supplier,
    matching the shape the former get_path_to endpoint produced."""
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adj[e.from_id].append(e.to_id)

    parent: dict[str, str] = {}
    frontier = [supplier_pid]
    reached = False
    while frontier and not reached:
        next_frontier: list[str] = []
        for node in frontier:
            if node == consumer_pid:
                reached = True
                break
            for nxt in adj.get(node, []):
                if nxt != supplier_pid and nxt not in parent:
                    parent[nxt] = node
                    next_frontier.append(nxt)
        frontier = next_frontier

    if consumer_pid != supplier_pid and consumer_pid not in parent:
        return None

    chain: list[str] = [consumer_pid]
    while chain[-1] != supplier_pid:
        chain.append(parent[chain[-1]])
    return [_pid_to_step(pid, node_info, supplier_step) for pid in chain]


def _pid_to_step(
    pid: str,
    node_info: dict[str, ConsumerResult],
    supplier_step: LocalStep,
) -> LocalStep:
    if pid == supplier_step.process_id:
        return supplier_step
    c = node_info[pid]
    return LocalStep(
        process_id=c.process_id,
        name=c.name,
        classifications=c.classifications,
    )


# ---------------------------------------------------------------------------
# Steps 3d-3f: Generate from_existing blocks and activities.json entries
# ---------------------------------------------------------------------------


def derive_alias(
    activity_name: str, variant_suffix: str, existing_aliases: set[str]
) -> str | None:
    """Shortest unique alias built from comma segments of the activity name."""
    segments = activity_name.split(",")
    for i in range(1, len(segments) + 1):
        base = ",".join(segments[:i]).strip()
        base = re.sub(r"\s*\{[^}]+\}\s*U$", "", base).strip()
        candidate = f"{slugify(base)}-{variant_suffix}"
        if candidate not in existing_aliases:
            return candidate
    return None


def make_from_existing(
    steps: list[LocalStep],
    source: VariantInfo,
    target: VariantInfo,
    existing_aliases: set[str],
) -> dict | None:
    """
    Build a from_existing block for activities_to_create.json.
    steps[0] = existingActivity (the transformed product)
    steps[-1] = the source raw ingredient activity

    If a consumption mix sits between the consumer and the ingredient,
    the mix itself becomes replace.from (replacing the entire sourcing blend).
    Otherwise the leaf ingredient is replace.from.
    Returns None if the alias already exists.
    """
    # Mix detection via the Category classification (robust across name variants).
    mix_index = next(
        (
            i
            for i in range(1, len(steps) - 1)
            if steps[i].classifications.get("Category") == MIX_CATEGORY
        ),
        None,
    )
    if mix_index is not None:
        upstream_steps = steps[1:mix_index]
        replace_from_step = steps[mix_index]
    else:
        upstream_steps = steps[1:-1]
        replace_from_step = steps[-1]

    variant_suffix = variant_short_suffix(target.alias)
    alias = derive_alias(steps[0].name, variant_suffix, existing_aliases)
    if alias is None:
        return None  # all prefixes taken — skip

    to_entry: dict = {"name": target.activity_name}
    if target.source != "Agribalyse 3.2":
        to_entry["database"] = target.source
    if target.location:
        to_entry["location"] = target.location

    return {
        "activityCreationType": "from_existing",
        "alias": alias,
        "comment": "auto-generated",
        "database": "Agribalyse 3.2",
        "existingActivity": {"name": steps[0].name},
        "newName": f"{steps[0].name} [{target.alias}] {{{{{alias}}}}}",
        "replacementPlan": {
            "upstreamPath": [{"name": s.name} for s in upstream_steps],
            "replace": [{"from": {"name": replace_from_step.name}, "to": to_entry}],
        },
    }


def load_or_train_predictor(training_ingredients_path: Path) -> Predictor:
    """Load the cached predictor pickle, or train one from the flat ingredients.json.

    predict.py's Predictor.fit() expects the flat ecobalyse ingredients schema
    (top-level `name`, `density`, `inediblePart`, …) shipped under
    public/data/food/ingredients.json — NOT the nested activities.json format.

    Training is expensive (FoodOn ontology + transformers) so the result is
    pickled next to the script.
    """
    if PREDICTOR_CACHE.exists():
        print(f"Loading predictor from {PREDICTOR_CACHE.name} ...")
        return Predictor.load(str(PREDICTOR_CACHE))
    print(
        f"Training predictor from {training_ingredients_path} "
        f"(slow, first run only) ..."
    )
    with training_ingredients_path.open() as f:
        training = json.load(f)
    p = Predictor()
    p.fit(training)
    p.save(str(PREDICTOR_CACHE))
    print(f"  saved to {PREDICTOR_CACHE.name}")
    return p


def make_activities_entry(
    fe_block: dict,
    target: VariantInfo,
    french_name: str,
    predictor: Predictor,
) -> dict:
    """Build an activities.json entry for a new transformed ingredient variant.

    Physical metadata (density, transportCooling, rawToCookedRatio, categories,
    cropGroup) is predicted from the transformed-product name via ../metadata/
    predict.py. Variant identity (scenario, defaultOrigin) comes from the
    target raw variant. inediblePart is hardcoded to 0 — transformation has
    already removed the inedible fraction.
    """
    meta = target.raw_meta
    alias = fe_block["alias"]
    display_name = french_name + variant_display_suffix(target.alias)

    # Pass the clean French product name (without " Bio" / " Origine Inconnue"
    # suffix) and the underlying Agribalyse activity name (without the variant
    # bracket added for substitution). The suffixes add noise to the
    # FoodOn/nearest-neighbour matchers inside predict.py.
    pred = predictor.predict({
        "name": french_name,
        "activityName": fe_block["existingActivity"]["name"],
    })

    # Predictor sometimes returns "_raw" / "_fresh" category tokens because
    # the NOVA keyword classifier does not fire on juice/puree/peeled names.
    # Transformed ingredients are always processed, so rewrite the category
    # suffix to match the ecobalyse convention (grain_processed,
    # vegetable_processed, …). Append the variant tag ("organic") last.
    base_categories = pred.get("categories") or []
    ing_categories = [
        c.replace("_raw", "_processed").replace("_fresh", "_processed")
        for c in base_categories
    ]
    if target.scenario == "organic" and "organic" not in ing_categories:
        ing_categories.append("organic")

    return {
        "activityName": fe_block["newName"],
        "alias": alias,
        "categories": ["ingredient", "material"],
        "database": "Ecobalyse",
        "displayName": display_name,
        "id": str(uuid4()),
        "metadata": [
            {
                "alias": alias,
                "scenario": target.scenario,
                "defaultOrigin": meta.get("defaultOrigin"),
                "id": str(uuid4()),
                "displayName": display_name,
                "cropGroup": pred.get("cropGroup") or meta.get("cropGroup"),
                "ingredientCategories": ing_categories,
                "inediblePart": 0,
                "ingredientDensity": pred.get("density"),
                "rawToCookedRatio": 1.0,
                "scopes": ["food", "food2"],
                "transportCooling": pred.get("transportCooling"),
                "visible": True,
            }
        ],
        "scopes": ["food", "food2"],
        "source": "Ecobalyse",
    }


def merge_by_activity_name(entries: list[dict]) -> list[dict]:
    """Collapse entries that share an activityName into one with concatenated
    metadata. Matches the convention used elsewhere in activities.json (one
    activity object hosting multiple scenario/origin metadata blocks).

    When merging, the host is whichever entry carries a reference-scenario
    metadata block (so top-level displayName/alias/id stay anchored to the
    reference variant); otherwise the first-seen entry is kept as host.
    """
    by_name: dict[str, dict] = {}
    for e in entries:
        an = e["activityName"]
        host = by_name.get(an)
        if host is None:
            by_name[an] = e
            continue
        host_has_ref = any(m.get("scenario") == "reference" for m in host["metadata"])
        new_has_ref = any(m.get("scenario") == "reference" for m in e["metadata"])
        if new_has_ref and not host_has_ref:
            e["metadata"] = [*e["metadata"], *host["metadata"]]
            by_name[an] = e
        else:
            host["metadata"] = [*host["metadata"], *e["metadata"]]
    return list(by_name.values())


def make_base_activities_entry(
    existing_activity_name: str,
    alias: str,
    target: VariantInfo,
    french_name: str,
    predictor: Predictor,
) -> dict:
    """Build an activities.json entry for the variant a consumer already uses.

    No substitution is needed — the consumer activity already exists in
    Agribalyse as-is, so we reference it directly (plain activityName,
    source="Agribalyse 3.2", no bracketed variant tag or {{alias}} UUID marker).
    """
    meta = target.raw_meta
    display_name = french_name + variant_display_suffix(target.alias)

    pred = predictor.predict({
        "name": french_name,
        "activityName": existing_activity_name,
    })

    base_categories = pred.get("categories") or []
    ing_categories = [
        c.replace("_raw", "_processed").replace("_fresh", "_processed")
        for c in base_categories
    ]
    if target.scenario == "organic" and "organic" not in ing_categories:
        ing_categories.append("organic")

    return {
        "activityName": existing_activity_name,
        "alias": alias,
        "categories": ["ingredient", "material"],
        "database": "Agribalyse 3.2",
        "displayName": display_name,
        "id": str(uuid4()),
        "metadata": [
            {
                "alias": alias,
                "scenario": target.scenario,
                "defaultOrigin": meta.get("defaultOrigin"),
                "id": str(uuid4()),
                "displayName": display_name,
                "cropGroup": pred.get("cropGroup") or meta.get("cropGroup"),
                "ingredientCategories": ing_categories,
                "inediblePart": 0,
                "ingredientDensity": pred.get("density"),
                "rawToCookedRatio": 1.0,
                "scopes": ["food", "food2"],
                "transportCooling": pred.get("transportCooling"),
                "visible": True,
            }
        ],
        "scopes": ["food", "food2"],
        "source": "Agribalyse 3.2",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--activities",
        default="lci_catalog",
        help="Path to the lci_catalog/ directory (per-activity files grouped by "
        "source slug, replacing the old activities.json). Default: lci_catalog",
    )
    parser.add_argument(
        "--activities-to-create",
        default="activities_to_create.json",
        help="Path to activities_to_create.json (used to skip existing aliases)",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output files (default: current directory)",
    )
    parser.add_argument(
        "--merge-into-catalog",
        default=None,
        help="If set, merge generated_activities.json directly into the given "
        "lci_catalog/ directory using food/metadata/lci_catalog.py:merge_activities. "
        "The catalog is rewritten in place (existing UUIDs preserved, "
        "ingredient alias collisions get the legacy '-2025' / ' (2025)' marker). "
        "Typically points at the same lci_catalog as --activities to round-trip.",
    )
    parser.add_argument(
        "--volca-url",
        default="http://localhost:8080",
        help="VoLCA API base URL (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="Max BFS depth for get_consumers (default: 2 = direct + one intermediate like market mix)",
    )
    parser.add_argument(
        "--training-ingredients",
        default=None,
        help="Path to flat ingredients.json used to train the metadata predictor "
        "(default: sibling public/data/food/ingredients.json next to --activities)",
    )
    args = parser.parse_args()

    # Load inputs
    activities_path = Path(args.activities)
    print(f"Loading {activities_path} ...")
    activities_json = load_lci_catalog(activities_path)
    print(f"  {len(activities_json)} activities loaded from catalog")

    # Seed every alias already in use so derive_alias never collides.
    # Three claim sources, all equally binding:
    #   - activities_to_create.json: pending creation blocks
    #   - lci_catalog activity-level aliases (= lci_catalog file stems)
    #   - lci_catalog ingredient-level aliases (the metadata blocks)
    # Without all three, a transformed product like "banana puree organic"
    # would emit alias "banana-organic" — which already names the raw Ginko
    # ingredient — and the merge would clobber it across source slugs.
    atc_path = Path(args.activities_to_create)
    existing_aliases: set[str] = set()
    if atc_path.exists():
        with atc_path.open() as f:
            for entry in json.load(f):
                if a := entry.get("alias"):
                    existing_aliases.add(a)
    existing_display_names: set[str] = set()
    for activity in activities_json:
        if a := activity.get("alias"):
            existing_aliases.add(a)
        for m in activity.get("metadata") or []:
            if a := m.get("alias"):
                existing_aliases.add(a)
            if dn := m.get("displayName"):
                existing_display_names.add(dn)
    print(f"  {len(existing_aliases)} aliases seeded as off-limits for derive_alias")
    print(f"  {len(existing_display_names)} displayNames seeded for collision fallback")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Connect to VoLCA
    client = Client(base_url=args.volca_url, db=AGRIBALYSE_DB)

    # Detect databases with native naming (EcoSpold 2) vs SimaPro CSV, and
    # derive the set of DBs hosting transformed food products from declared
    # topology: agribalyse itself plus anything that depends on it.
    dbs = client.list_databases()
    native_dbs = {db.name for db in dbs if db.format != "SimaPro CSV"}
    food_transform_dbs = {AGRIBALYSE_DB} | {
        db.name for db in dbs if AGRIBALYSE_DB in db.depends_on
    }
    print(f"Native-naming databases: {native_dbs}")
    print(f"Food-transform databases: {food_transform_dbs}")

    # Step 1: Parse and group
    print("Parsing ingredient groups ...")
    all_groups = parse_ingredient_groups(activities_json)
    groups = filter_actionable_groups(all_groups)
    print(
        f"  {len(all_groups)} total groups, {len(groups)} actionable (have substitutable variants)"
    )

    # Step 2: Resolve process_ids
    print("Resolving process IDs via VoLCA ...")
    resolve_process_ids(groups, client, native_dbs)
    resolvable = sum(
        1 for vs in groups.values() if any(v.process_id is not None for v in vs)
    )
    print(f"  {resolvable}/{len(groups)} groups have at least one resolved variant")

    # Step 3c-f: Generate entries
    # Each output tuple carries BOTH the short and the long English name.
    # The short form is the preferred displayName source; the long form
    # (which keeps `for X` / `from X` qualifiers normally treated as jargon)
    # is the fallback when the short form's translated displayName would
    # collide with an existing entry in lci_catalog/.
    output_fe: list[tuple[dict, VariantInfo, str, str]] = []
    output_base: list[tuple[str, str, VariantInfo, str, str]] = []
    all_generated_aliases: set[str] = set(existing_aliases)

    for base, variants in groups.items():
        resolved = [v for v in variants if v.process_id is not None]
        # Skip proxy variants: their consumers belong to a different ingredient
        genuine = [v for v in resolved if not is_proxy(base, v.activity_name)]
        if not genuine:
            continue

        # Collect consumers + per-supplier edge subgraph in one pass
        consumer_to_found, node_info, supplier_edges = collect_consumers(
            genuine, client, food_transform_dbs, max_depth=args.max_depth
        )

        # variant lookup by alias
        variant_by_alias = {v.alias: v for v in variants}

        for consumer_pid, found_aliases in consumer_to_found.items():
            # Skip consumers that belong to a different ingredient (proxy relationship)
            consumer_name = node_info[consumer_pid].name
            if is_proxy(base, consumer_name):
                continue

            missing = [v for v in genuine if v.alias not in found_aliases]
            if not missing:
                continue

            # Pick one found variant as path source
            source_alias = next(iter(found_aliases))
            source = variant_by_alias.get(source_alias)
            if source is None or source.process_id is None:
                continue

            source_step = LocalStep(
                process_id=source.process_id,
                name=source.activity_name,
                classifications={},
            )
            path = shortest_path_via_edges(
                source.process_id,
                consumer_pid,
                supplier_edges.get(source.process_id, []),
                node_info,
                source_step,
            )
            if path is None:
                print(
                    f"  [WARN] no path found from {source.alias!r} to consumer {consumer_name!r}",
                    file=sys.stderr,
                )
                continue

            existing_name = path[0].name
            base_short_name = extract_short_name(existing_name)
            base_long_name = extract_long_name(existing_name)
            for found_alias in found_aliases:
                base_target = variant_by_alias.get(found_alias)
                if base_target is None:
                    continue
                variant_suffix = variant_short_suffix(base_target.alias)
                base_alias = derive_alias(
                    existing_name, variant_suffix, all_generated_aliases
                )
                if base_alias is None:
                    continue
                all_generated_aliases.add(base_alias)
                output_base.append((
                    existing_name,
                    base_alias,
                    base_target,
                    base_short_name,
                    base_long_name,
                ))

            for target in missing:
                fe = make_from_existing(path, source, target, all_generated_aliases)
                if fe is None:
                    continue
                src_name = fe["existingActivity"]["name"]
                short_name = extract_short_name(src_name)
                long_name = extract_long_name(src_name)
                output_fe.append((fe, target, short_name, long_name))
                all_generated_aliases.add(fe["alias"])

    # Translate short AND long names in a single MarianMT batch (the cache
    # makes any duplicates free). The long form is only picked when the
    # short form's displayName would collide with an existing entry.
    all_names = list(
        dict.fromkeys(
            [sn for _, _, sn, _ in output_fe]
            + [ln for _, _, _, ln in output_fe]
            + [sn for _, _, _, sn, _ in output_base]
            + [ln for _, _, _, _, ln in output_base]
        )
    )
    print(f"Translating {len(all_names)} unique product names to French ...")
    french_translations = translate_en_to_fr(all_names)
    en_to_fr = dict(zip(all_names, french_translations))
    for en, fr in en_to_fr.items():
        print(f"  {en} → {fr}")

    # Train (or load cached) metadata predictor from existing ingredients
    if args.training_ingredients:
        training_path = Path(args.training_ingredients)
    else:
        training_path = activities_path.parent / "public/data/food/ingredients.json"
    predictor = load_or_train_predictor(training_path)

    def _pick_french(short_en: str, long_en: str, variant_alias: str) -> str:
        """Prefer the short translation; fall back to the long one if the
        resulting displayName would collide with an existing lci_catalog entry.
        Both candidates' displayNames also get reserved so within-run siblings
        of the same product end up consistently disambiguated."""
        suffix = variant_display_suffix(variant_alias)
        short_fr = en_to_fr[short_en]
        if short_fr + suffix not in existing_display_names:
            existing_display_names.add(short_fr + suffix)
            return short_fr
        long_fr = en_to_fr[long_en]
        existing_display_names.add(long_fr + suffix)
        return long_fr

    # Build final outputs
    final_fe: list[dict] = []
    final_ae: list[dict] = []
    for fe, target, short_name, long_name in output_fe:
        french = _pick_french(short_name, long_name, target.alias)
        ae = make_activities_entry(fe, target, french, predictor)
        final_fe.append(fe)
        final_ae.append(ae)
    for existing_name, alias, target, short_name, long_name in output_base:
        french = _pick_french(short_name, long_name, target.alias)
        ae = make_base_activities_entry(
            existing_name, alias, target, french, predictor
        )
        final_ae.append(ae)

    # Collapse entries sharing an activityName (happens when a consumer uses
    # several raw variants natively — e.g. both -fr and -default): keep one
    # activity object with concatenated metadata blocks.
    final_ae = merge_by_activity_name(final_ae)

    # Write outputs
    fe_path = output_dir / "generated_activities_to_create.json"
    ae_path = output_dir / "generated_activities.json"

    with fe_path.open("w") as f:
        json.dump(final_fe, f, indent=2, ensure_ascii=False)
    with ae_path.open("w") as f:
        json.dump(final_ae, f, indent=2, ensure_ascii=False)

    print(f"\nDone.")
    print(f"  {len(final_fe)} from_existing blocks → {fe_path}")
    print(f"  {len(final_ae)} activities.json entries → {ae_path}")

    if args.merge_into_catalog:
        catalog_dir = Path(args.merge_into_catalog)
        print(f"\nMerging {ae_path.name} into {catalog_dir} ...")
        merge_activities(ae_path, catalog_dir)

        # Also append the from_existing blocks to the target's
        # activities_to_create.json (sibling of lci_catalog/), de-duped on alias.
        target_atc = catalog_dir.parent / "activities_to_create.json"
        existing_atc: list[dict] = []
        if target_atc.exists():
            with target_atc.open() as f:
                existing_atc = json.load(f)
        existing_atc_aliases = {
            e.get("alias") for e in existing_atc if e.get("alias")
        }
        new_blocks = [
            b for b in final_fe if b.get("alias") not in existing_atc_aliases
        ]
        if new_blocks:
            existing_atc.extend(new_blocks)
            with target_atc.open("w") as f:
                json.dump(existing_atc, f, indent=2, ensure_ascii=False)
        skipped = len(final_fe) - len(new_blocks)
        print(
            f"  Appended {len(new_blocks)} from_existing blocks "
            f"(skipped {skipped} already present) → {target_atc}"
        )


if __name__ == "__main__":
    main()
