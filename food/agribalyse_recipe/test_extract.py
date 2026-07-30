# /// script
# requires-python = ">=3.10"
# dependencies = ["pyvolca>=0.8.2", "openpyxl"]
# ///
"""Self-check for the row builders — no engine, no network.

    uv run test_extract.py

Fixtures are read off Agribalyse 3.2: the chilled pizza packed in a 450 g
system, so 1/0,45 = 2,2222 systems per kg of pizza, which is the one division
the packaging sheet hangs on. `stage_bill` takes its resolver as an argument, so
a dict's `get` and no client at all is enough to check it.
"""

from dataclasses import dataclass, field

from extract_agribalyse_recipes import (
    ingredient_targets, packaging_of, product_columns, stage_bill, system_rows)


@dataclass
class Exchange:
    """The four fields the row builder reads off a technosphere exchange."""
    flow_name: str
    amount: float
    unit: str
    target_process_id: str = ""
    is_reference: bool = False


@dataclass
class Detail:
    """The fields the stage walk reads off a fetched activity."""
    process_id: str
    activity_name: str
    classifications: dict
    product_amount: float
    technosphere_inputs: list = field(default_factory=list)
    product_name: str | None = None
    product_unit: str | None = None
    unit: str | None = None
    location: str = "FR"


PREFIX = ["pizza_pid", "Pizza, vegetables or pizza 4 seasons, at plant {FR}", "FR", 1.0, "kg"]
SYSTEM = "Pizzas, chilled, 450g | Packaging System, N0, All, Cardboard support with plastic bag {FR} U"
BAGS_PER_KG = 1 / 0.45


CHEESE = "Abondance cheese production, from cow's milk, hard cheese, French production mix"


def test_co_products_are_told_apart_by_their_product_name():
    """One multi-output activity, three processes: only the flow name differs.

    Labelling the rows with the activity name gave three products called
    "Abondance cheese production, …" with different amounts and no way to tell
    the cheese from the cream.
    """
    cheese = Detail("cheese_pid", CHEESE, RECIPE, 1.0,
                    product_name=f"{CHEESE}, at plant, 1 kg of Abondance cheese (PGi) {{FR}} U",
                    product_unit="kg")
    cream = Detail("cream_pid", CHEESE, RECIPE, 0.0686462,
                   product_name=f"{CHEESE}, at plant, 1 kg of cream (PGi) {{FR}} U",
                   product_unit="kg")
    assert product_columns(cheese)[1] != product_columns(cream)[1]
    assert product_columns(cream) == ["cream_pid", cream.product_name, "FR", 0.0686462, "kg"]
    # No product flow name (single-output export): the activity name still names it.
    assert product_columns(Detail("p", CHEESE, RECIPE, 1.0, product_unit="kg"))[1] == CHEESE


def test_targets_name_the_co_product_not_the_activity():
    """The biscuit's palm oil, as the engine really answers it.

    `targetProcessId` is the activity's, and this activity has two co-products,
    so it comes back as the palm *kernel* oil process. The right process id is
    the join of the activity link and the product flow the exchange names.
    """
    activity = "1b4df3b8-0019-5f36-9599-80eff1c2eaeb"
    palm_oil = "15f14424-e40e-5eeb-a615-788e58bdae22"
    palm_kernel_oil = "41e9c0e8-6667-5323-9fb2-735f96ec8b67"
    payload = {"activity": {"exchanges": [{
        "exchange": {"tag": "TechnosphereExchange", "role": "Input",
                     "flowId": palm_oil, "amount": 0.0597,
                     "activityLinkId": activity},
        "flowName": "Palm oil, crude, consumption mix {FR} U",
        "targetProcessId": f"{activity}_{palm_kernel_oil}",
    }]}}
    targets = ingredient_targets(payload)
    assert targets["Palm oil, crude, consumption mix {FR} U"] == f"{activity}_{palm_oil}"


def test_targets_skip_what_carries_no_link():
    """Biosphere exchanges and unlinked technosphere inputs name no product."""
    payload = {"activity": {"exchanges": [
        {"exchange": {"tag": "BiosphereExchange", "flowId": "f", "amount": 1.0,
                      "role": "Input"},
         "flowName": "Carbon dioxide"},
        {"exchange": {"tag": "TechnosphereExchange", "flowId": "f",
                      "amount": 1.0, "role": "Input", "activityLinkId": None},
         "flowName": "Transport, freight"},
    ]}}
    assert ingredient_targets(payload) == {}
    assert ingredient_targets({}) == {}


def test_the_reference_product_never_wins_over_the_input():
    """A self-consuming recipe, as Agribalyse writes the nuoc mam sauce.

    Its own flow appears twice under one name: the reference product, linked to
    the all-zero activity, and the 0,183 kg input linked to the recipe itself.
    Keyed by name they collide, and the reference product must never be the one
    that lands in the sheet — whatever order the exchanges arrive in.
    """
    activity, flow = "ae9e9916-3ec3-51d6-9a58-6b61cc0d30ff", "4ce102de-3da6-5caf-a6aa-481f8a55034f"
    name = "Nuoc mam sauce or fish sauce, prepacked, recipe, at plant {FR} U"
    reference = {"exchange": {"tag": "TechnosphereExchange", "role": "ReferenceProduct",
                              "flowId": flow, "amount": 1.0,
                              "activityLinkId": "00000000-0000-0000-0000-000000000000"},
                 "flowName": name}
    consumed = {"exchange": {"tag": "TechnosphereExchange", "role": "Input",
                             "flowId": flow, "amount": 0.183,
                             "activityLinkId": activity},
                "flowName": name}
    for order in ([reference, consumed], [consumed, reference]):
        assert ingredient_targets({"activity": {"exchanges": order}}) == {
            name: f"{activity}_{flow}"}


# A stage is filed under a food family; a system or element under a dotted
# segment. That dot is the only thing telling a packaging from a packed food,
# both living under Agricultural\Food\Packaging.
STAGE_CAT = {"Category": "Agricultural\\Food\\Packaging\\Cereal products\\Pizzas"}
SYSTEM_CAT = {"Category": "Agricultural\\Food\\Packaging\\Cereal products\\.Packaging systems"}
RECIPE = {"Category": "Agricultural\\Food\\Recipes"}


def packaging_stage(*inputs) -> dict:
    """A stage packing 1 kg of pizza in a 450 g system, plus whatever is asked.

    The keys are the corrected process-ids the stage's exchanges resolve to, so
    a test only has to hand `stage_bill` the cache's `get`.
    """
    return {
        "system_pid": Detail("system_pid", SYSTEM, SYSTEM_CAT, 0.45),
        "pizza_pid": Detail("pizza_pid", "Pizza, at plant {FR}", RECIPE, 1.0),
        "stage_pid": Detail("stage_pid", "Pizza | at packaging {FR}", STAGE_CAT, 1.0,
                            list(inputs)),
    }


def bill(cache, targets=None):
    """`stage_bill` on the fixture, with the flow names resolving to themselves."""
    stage = cache["stage_pid"]
    if targets is None:
        targets = {e.flow_name: e.target_process_id
                   for e in stage.technosphere_inputs}
    return stage_bill(stage, targets, cache.get)


def test_stage_divides_by_the_food_it_packs():
    cache = packaging_stage(
        Exchange(SYSTEM, 1.0, "kg", "system_pid"),
        Exchange("Pizza, at plant {FR}", 1.0, "kg", "pizza_pid"),
    )
    food_pid, entries = bill(cache)
    assert food_pid == "pizza_pid"
    assert entries[0][0] is cache["system_pid"]
    assert abs(entries[0][1] - BAGS_PER_KG) < 1e-12  # 2,2222 systems per kg of pizza


def test_an_extra_input_does_not_become_the_divisor():
    """The food is an agricultural input; anything else is not."""
    cache = packaging_stage(
        Exchange(SYSTEM, 1.0, "kg", "system_pid"),
        Exchange("Pizza, at plant {FR}", 1.0, "kg", "pizza_pid"),
        Exchange("Electricity, low voltage {FR}", 0.5, "kWh", "electricity_pid"),
    )
    cache["electricity_pid"] = Detail(
        "electricity_pid", "Electricity", {"Category": "Energy"}, 1.0)
    _, entries = bill(cache)
    assert abs(entries[0][1] - BAGS_PER_KG) < 1e-12  # not 1/0,5/0,45 = 4,44


def test_a_stage_packing_nothing_gives_nothing():
    """'No pack', raw fruit sold loose: a stage, but an empty bill."""
    cache = packaging_stage(Exchange("Fruit, at plant {FR}", 1.0, "kg", "pizza_pid"))
    assert bill(cache) == ("pizza_pid", [])


def test_a_packaging_system_is_not_a_stage():
    """An N0 system holds its elements and no food, so it packs nothing.

    Read as a stage it would come out packed in itself: 236 such rows appeared
    the day the extraction was run over every process of the database, each
    claiming a packaging system is packed in a packaging system.
    """
    cache = packaging_stage()
    cache["stage_pid"] = Detail(
        "stage_pid", SYSTEM, SYSTEM_CAT, 0.45,
        [Exchange("Plastic bag element {FR}", 1.0, "p", "element_pid")])
    cache["element_pid"] = Detail("element_pid", "Plastic bag element {FR}",
                                  SYSTEM_CAT, 1.0)
    assert bill(cache) is None


def test_the_food_can_itself_be_a_packaging_stage():
    """Agribalyse packs the wholemeal sandwich by consuming the French-bread one.

    The proxy food is itself an "at packaging" process, so "under Packaging"
    does not mean "is a packaging" — telling them apart by the dotted segment is
    what keeps this stage from losing its food, and its row.
    """
    cache = packaging_stage(
        Exchange(SYSTEM, 1.0, "kg", "system_pid"),
        Exchange("Sandwich, French bread | at packaging {FR}", 1.0, "kg", "proxy_pid"),
    )
    cache["proxy_pid"] = Detail("proxy_pid", "Sandwich, French bread | at packaging {FR}",
                                STAGE_CAT, 1.0)
    food_pid, entries = bill(cache)
    assert food_pid == "proxy_pid"
    assert abs(entries[0][1] - BAGS_PER_KG) < 1e-12


def test_an_unlinked_input_is_no_food():
    """An all-zero activity link names no process, so it cannot be the divisor."""
    zero = "00000000-0000-0000-0000-000000000000_f00d"
    cache = packaging_stage(
        Exchange(SYSTEM, 1.0, "kg", "system_pid"),
        Exchange("Unlinked something {FR}", 0.5, "kg", zero),
        Exchange("Pizza, at plant {FR}", 1.0, "kg", "pizza_pid"),
    )
    _, entries = bill(cache)
    assert abs(entries[0][1] - BAGS_PER_KG) < 1e-12


CONSUMER_CAT = {"Category": "Agricultural\\Food\\Preparation\\Sauces"}
RETAIL_CAT = {"Category": "Agricultural\\Food\\Retail\\Sauces"}
DISTRIB_CAT = {"Category": "Agricultural\\Food\\Distribution\\Sauces"}


def ciqual_chain() -> dict:
    """The aioli as Agribalyse models it, from the shelf back to the plant.

    at consumer <- at retail <- at distribution <- at packaging <- recipe,
    each step consuming a little more than a kilo for the losses ahead of it.
    """
    return {
        "consumer_pid": Detail("consumer_pid", "Aioli | at consumer {FR} [Ciqual code: 11007]",
                               CONSUMER_CAT, 1.0,
                               [Exchange("Aioli | at retail {FR}", 1.05, "kg", "retail_pid"),
                                Exchange("Tap water {FR}", 0.7, "kg", "water_pid")]),
        "retail_pid": Detail("retail_pid", "Aioli | at retail {FR}", RETAIL_CAT, 1.0,
                             [Exchange("Aioli | at distribution {FR}", 1.01, "kg",
                                       "distribution_pid")]),
        "distribution_pid": Detail("distribution_pid", "Aioli | at distribution {FR}",
                                   DISTRIB_CAT, 1.0,
                                   [Exchange("Aioli | at packaging {FR}", 1.0, "kg",
                                             "stage_pid")]),
        "stage_pid": Detail("stage_pid", "Aioli | at packaging {FR}", STAGE_CAT, 1.0,
                            [Exchange(SYSTEM, 1.0, "kg", "system_pid"),
                             Exchange("Aioli, recipe, at plant {FR}", 1.0, "kg", "recipe_pid")]),
        "system_pid": Detail("system_pid", SYSTEM, SYSTEM_CAT, 0.45),
        "recipe_pid": Detail("recipe_pid", "Aioli, recipe, at plant {FR}", RECIPE, 1.0),
        "water_pid": Detail("water_pid", "Tap water {FR}", {"Category": "Others"}, 1.0),
    }


def fetcher(cache):
    """`(detail, targets)` for a fixture, the flow names resolving to themselves."""
    def fetch(pid):
        det = cache[pid]
        return det, {e.flow_name: e.target_process_id for e in det.technosphere_inputs}
    return fetch


def test_a_ciqual_product_finds_its_packaging_down_the_chain():
    """The Ciqual product is three stages downstream of what packs it."""
    cache = ciqual_chain()
    bills = ({"stage_pid": [(cache["system_pid"], BAGS_PER_KG)]},
             {"recipe_pid": [(cache["system_pid"], BAGS_PER_KG)]})
    fetch = fetcher(cache)
    entries = packaging_of(fetch, bills, "consumer_pid", fetch("consumer_pid"))
    assert entries == bills[0]["stage_pid"]
    # Per kilo of packed food, the losses downstream left out: 2,35 and not 2,49.
    assert abs(entries[0][1] - BAGS_PER_KG) < 1e-12


def test_a_recipe_never_inherits_the_packaging_of_an_ingredient():
    """A recipe with no stage of its own comes out unpacked, not packed in oil.

    Walking an ingredient link would hang the olive-oil bottle on the aioli;
    only the lifecycle links are followed, and a recipe carries none.
    """
    cache = ciqual_chain()
    cache["unpacked_pid"] = Detail(
        "unpacked_pid", "Light aioli, recipe, at plant {FR}", RECIPE, 1.0,
        [Exchange("Aioli, recipe, at plant {FR}", 0.9, "kg", "recipe_pid")])
    bills = ({"stage_pid": [(cache["system_pid"], BAGS_PER_KG)]},
             {"recipe_pid": [(cache["system_pid"], BAGS_PER_KG)]})
    fetch = fetcher(cache)
    assert packaging_of(fetch, bills, "unpacked_pid", fetch("unpacked_pid")) is None


def test_a_ciqual_product_gets_its_own_format_not_its_cousins():
    """28 breakfast cereals share one recipe, each in its own bag.

    Asking the recipe gives the whole family's formats; a Ciqual product must
    ask its own stage, or every cereal would come out in 28 packagings at once.
    """
    cache = ciqual_chain()
    other = Detail("other_pid", "Aioli, 1kg | Packaging System", SYSTEM_CAT, 1.0)
    bills = ({"stage_pid": [(cache["system_pid"], BAGS_PER_KG)]},
             {"recipe_pid": [(cache["system_pid"], BAGS_PER_KG), (other, 1.0)]})
    fetch = fetcher(cache)
    entries = packaging_of(fetch, bills, "consumer_pid", fetch("consumer_pid"))
    assert [s.process_id for s, _ in entries] == ["system_pid"]
    # The recipe itself, asked directly, still stands for the whole family.
    recipe_node = fetch("recipe_pid")
    assert len(packaging_of(fetch, bills, "recipe_pid", recipe_node)) == 2


def test_a_product_packed_in_nothing_is_not_a_product_with_no_stage():
    """'No pack' answers with an empty bill; unknown answers with None."""
    cache = ciqual_chain()
    fetch = fetcher(cache)
    node = fetch("consumer_pid")
    assert packaging_of(fetch, ({"stage_pid": []}, {}), "consumer_pid", node) == []
    assert packaging_of(fetch, ({}, {}), "consumer_pid", node) is None


def test_rows_scale_by_the_functional_unit():
    """Maize is billed by the ton: 10 bags per kg is 10 000 per functional unit."""
    prefix = ["maize_pid", "Dried grain maize, at processing {FR} U", "FR", 1000.0, "kg"]
    system = Detail("bag_pid", "Pop corn, 100g | Packaging System", SYSTEM_CAT, 0.1,
                    product_unit="kg")
    rows = system_rows(prefix, [(system, 10.0)])
    assert rows == [prefix + ["Pop corn, 100g | Packaging System", "bag_pid",
                              10000.0, 0.1, "kg"]]
    assert system_rows(prefix, []) == []


if __name__ == "__main__":
    for name, case in sorted(globals().items()):
        if name.startswith("test_"):
            case()
            print(f"ok  {name}")
    print("all good")
