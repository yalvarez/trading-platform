"""B3 (spec section 5): same as B1/B2 with a third phrasing."""
from tests.e2e.scenarios import b1_be_variant1
from tests.e2e.scenarios.base import ScenarioContext, ScenarioResult

MESSAGE = "lock all your trades in Break Even"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    b1_be_variant1.MESSAGE = MESSAGE
    result = await b1_be_variant1.run(ctx)
    result.name = "b3_be_variant3"
    return result
