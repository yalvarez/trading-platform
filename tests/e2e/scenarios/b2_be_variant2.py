"""B2 (spec section 5): same as B1 but with different phrasing, to confirm
Ollama generalizes intent rather than matching a fixed keyword."""
from tests.e2e.scenarios import b1_be_variant1
from tests.e2e.scenarios.base import ScenarioContext, ScenarioResult

MESSAGE = "Make sure you adjust your sl to Entry for zero risk"


async def run(ctx: ScenarioContext) -> ScenarioResult:
    b1_be_variant1.MESSAGE = MESSAGE
    result = await b1_be_variant1.run(ctx)
    result.name = "b2_be_variant2"
    return result
