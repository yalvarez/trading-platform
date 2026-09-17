# Direct Close-Now Shortcut Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recognize the literal "TRADE INVALID ... Close now" pattern in `router_parser` with a regex, execute `close_now` directly against `trade_orchestrator` without going through n8n/Ollama, and add a `direction_hint` filter to `close_now` so a message naming one direction never closes a group of the opposite direction.

**Architecture:** `router_parser`'s existing raw-message loop gains a fourth branch (sibling to `DUPLICATE_SIGNAL`, `sig`, and "unrecognized → n8n"): a regex match on "TRADE INVALID...Close now" triggers a direct authenticated POST to `trade_orchestrator`'s `/mgmt/action`, with retries, and never reaches n8n. `apply_mgmt_action` gains an optional `direction_hint` parameter used only inside the `close_now` branch to filter which groups get closed.

**Tech Stack:** Python 3, FastAPI, httpx, pytest + pytest-asyncio, Redis Streams (existing `n8n_event_retry_queue`).

**Spec:** `docs/superpowers/specs/2026-09-17-direct-close-now-shortcut-design.md`

## Global Constraints

- `CLOSE_NOW_RE` must require both `TRADE INVALID` and `CLOSE NOW`, in that order, case-insensitive, matching across newlines (spec §4).
- `direction_hint` sent over the wire must be upper-cased `"BUY"` or `"SELL"`, never lowercase or any other value (spec §4, §5).
- `TRADE_ORCHESTRATOR_MGMT_URL` default value is `http://trade_orchestrator:8200/mgmt/action` — port 8200, not 8000 (spec §5, verified against `services/trade_orchestrator/app.py:205` and `docker-compose.yml`).
- The direction filter applies **only** inside the `close_now` branch of `apply_mgmt_action` — never before the action switch, never touching `signal_correction` or `move_sl_be_now` (spec §6).
- On 3 failed retries (or a 4xx), `router_parser` enqueues a notification via `services.trade_orchestrator.n8n_retry_worker.enqueue(redis_client, envelope)` — it must never POST directly to `N8N_EVENT_WEBHOOK_URL` and never fall back to `forward_to_n8n` (spec §5).
- `validate_router_parser()` must require `N8N_ACTION_API_KEY` (spec §5).
- A message that matches `CLOSE_NOW_RE` must never be forwarded to n8n, whether the direct call succeeds or exhausts its retries (spec §2, §5).

---

## File Structure

| File | Change |
|---|---|
| `services/router_parser/parsers_management.py` | **Create.** `CLOSE_NOW_RE`, `DIRECTION_RE`, `match_close_now(text)`. |
| `services/router_parser/test_parsers_management.py` | **Create.** Unit tests for the regex/extraction function. |
| `services/router_parser/app.py` | **Modify.** New `execute_close_now_directly()` function; new branch in the `main()` loop. |
| `services/router_parser/test_app.py` | **Modify.** Tests for the new branch and the direct-call function. |
| `services/common/env_validator.py` | **Modify.** `validate_router_parser()` requires `N8N_ACTION_API_KEY`. |
| `services/common/test_env_validator.py` | **Modify or create** (check first — see Task 5). Test for the new requirement. |
| `.env.example` | **Modify.** Add `TRADE_ORCHESTRATOR_MGMT_URL`. |
| `docker-compose.yml` | No change needed — `router_parser` already loads `env_file: .env`, which already has `N8N_ACTION_API_KEY`. |
| `services/trade_orchestrator/mgmt_api.py` | **Modify.** `MgmtActionRequest` gains `direction_hint`. |
| `services/trade_orchestrator/trade_manager.py` | **Modify.** `apply_mgmt_action` gains `direction_hint` param; `_filter_groups_by_direction` helper; filter applied inside `close_now`; `mgmt_direction_filtered` notification. |
| `services/trade_orchestrator/test_mgmt_action_endpoint.py` | **Modify.** Tests for `direction_hint` validation and end-to-end filtering, including the 149/150 incident reproduction. |

---

## Task 1: Regex recognition module in router_parser

**Files:**
- Create: `services/router_parser/parsers_management.py`
- Test: `services/router_parser/test_parsers_management.py`

**Interfaces:**
- Produces: `match_close_now(text: str) -> Optional[dict]` returning `{"action": "close_now", "direction_hint": "SELL" | "BUY" | None}` or `None`.

- [ ] **Step 1: Write the failing tests**

```python
# services/router_parser/test_parsers_management.py
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from parsers_management import match_close_now


def test_matches_the_real_incident_message():
    text = "XAUUSD SELL TRADE INVALID ❌\n\nClose now"
    result = match_close_now(text)
    assert result == {"action": "close_now", "direction_hint": "SELL"}


def test_matches_buy_variant():
    text = "XAUUSD BUY TRADE INVALID\nClose now all"
    result = match_close_now(text)
    assert result == {"action": "close_now", "direction_hint": "BUY"}


def test_direction_hint_is_none_when_text_names_no_direction():
    text = "TRADE INVALID\nClose now"
    result = match_close_now(text)
    assert result == {"action": "close_now", "direction_hint": None}


def test_direction_hint_is_uppercased_even_if_text_is_lowercase():
    text = "trade invalid please close now the buy position"
    result = match_close_now(text)
    assert result == {"action": "close_now", "direction_hint": "BUY"}


def test_does_not_match_close_now_alone():
    assert match_close_now("Close now") is None


def test_does_not_match_trade_invalid_alone():
    assert match_close_now("TRADE INVALID") is None


def test_does_not_match_reversed_order():
    assert match_close_now("Close now because TRADE INVALID") is None


def test_does_not_match_fast_signal():
    assert match_close_now("XAUUSD SELL NOW") is None


def test_takes_first_direction_when_two_are_present():
    text = "XAUUSD SELL TRADE INVALID, the BUY stays\nClose now"
    result = match_close_now(text)
    assert result == {"action": "close_now", "direction_hint": "SELL"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/router_parser/test_parsers_management.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'parsers_management'`

- [ ] **Step 3: Write the implementation**

```python
# services/router_parser/parsers_management.py
"""
parsers_management.py
Reconocimiento sin LLM del patron "TRADE INVALID ... Close now" que llega
por Telegram como mensaje de gestion. A diferencia de parsers_tradepulse.py,
no produce una senal de apertura: produce una accion de gestion que
router_parser ejecuta directo contra /mgmt/action, sin pasar por n8n/Ollama
(ver docs/superpowers/specs/2026-09-17-direct-close-now-shortcut-design.md).
"""
import re
from typing import Optional

CLOSE_NOW_RE = re.compile(r'TRADE\s+INVALID.*?CLOSE\s+NOW', re.IGNORECASE | re.DOTALL)
DIRECTION_RE = re.compile(r'\b(BUY|SELL)\b', re.IGNORECASE)


def match_close_now(text: str) -> Optional[dict]:
    """
    Retorna {"action": "close_now", "direction_hint": "SELL"|"BUY"|None} si
    el texto es una orden de cierre total reconocible sin LLM (exige ambas
    frases, TRADE INVALID y CLOSE NOW, en ese orden), o None si no lo es --
    el texto sigue su curso normal hacia n8n.
    """
    if not CLOSE_NOW_RE.search(text):
        return None
    direction_m = DIRECTION_RE.search(text)
    direction_hint = direction_m.group(1).upper() if direction_m else None
    return {"action": "close_now", "direction_hint": direction_hint}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/router_parser/test_parsers_management.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add services/router_parser/parsers_management.py services/router_parser/test_parsers_management.py
git commit -m "$(cat <<'EOF'
feat(router_parser): recognize TRADE INVALID/Close now without an LLM

Adds match_close_now(), a regex-based recognizer for the one management
pattern that's literal enough not to need Ollama classification. Extracts
an optional direction hint from the text. Standalone module, not yet wired
into the main loop.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `direction_hint` on `MgmtActionRequest`

**Files:**
- Modify: `services/trade_orchestrator/mgmt_api.py`
- Test: `services/trade_orchestrator/test_mgmt_action_endpoint.py`

**Interfaces:**
- Consumes: none new.
- Produces: `MgmtActionRequest.direction_hint: Optional[str]`, validated to be exactly `"BUY"` or `"SELL"` when present. `mgmt_action()` passes it through to `apply_mgmt_action(..., direction_hint=req.direction_hint)`.

- [ ] **Step 1: Write the failing tests**

Add to `services/trade_orchestrator/test_mgmt_action_endpoint.py`:

```python
@pytest.mark.parametrize("bad_direction", ["sell", "buy", "LONG", "BOTH", ""])
def test_mgmt_action_rejects_invalid_direction_hint(tm_and_client, bad_direction):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={
        "action": "close_now", "chat_id": CHAT_ID,
        "raw_text": "close now", "correction": None,
        "direction_hint": bad_direction,
    })
    assert resp.status_code == 422


@pytest.mark.parametrize("good_direction", ["BUY", "SELL"])
def test_mgmt_action_accepts_valid_direction_hint(tm_and_client, good_direction):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={
        "action": "close_now", "chat_id": CHAT_ID,
        "raw_text": "close now", "correction": None,
        "direction_hint": good_direction,
    })
    assert resp.status_code == 200


def test_mgmt_action_still_accepts_omitted_direction_hint(tm_and_client):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={
        "action": "close_now", "chat_id": CHAT_ID,
        "raw_text": "close now", "correction": None,
    })
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_mgmt_action_endpoint.py -k direction_hint -v`
Expected: FAIL — `bad_direction` cases get 200 instead of 422 (field doesn't exist yet, extra fields are ignored by default Pydantic config), OR a `TypeError` if `apply_mgmt_action` doesn't accept the kwarg yet. Confirm the actual failure mode before proceeding.

- [ ] **Step 3: Implement**

In `services/trade_orchestrator/mgmt_api.py`, modify `MgmtActionRequest`:

```python
class MgmtActionRequest(BaseModel):
    action: str
    chat_id: str
    raw_text: str
    correction: Optional[Correction] = None
    percent: Optional[float] = Field(default=None, gt=0, lt=100)
    # Extraido por el regex de router_parser (patron "TRADE INVALID/Close
    # now") o, en el futuro, por el flujo n8n/Ollama. Filtra que grupos
    # toca close_now cuando el texto nombra una direccion (ver
    # docs/superpowers/specs/2026-09-17-direct-close-now-shortcut-design.md
    # seccion 6 para por que el filtro vive solo en close_now).
    direction_hint: Optional[str] = Field(default=None, pattern="^(BUY|SELL)$")
```

And in `mgmt_action()`:

```python
result = await trade_manager.apply_mgmt_action(
    action=req.action, chat_id=req.chat_id, raw_text=req.raw_text,
    correction=correction, percent=req.percent, direction_hint=req.direction_hint,
)
```

This will fail until Task 3 adds `direction_hint` to `apply_mgmt_action`'s signature — that's expected; the two tasks land together before the tests can pass. Proceed to Task 3 before running Step 4.

- [ ] **Step 4: Run tests to verify they pass (after Task 3 lands)**

Run: `pytest services/trade_orchestrator/test_mgmt_action_endpoint.py -k direction_hint -v`
Expected: PASS (7 cases)

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/mgmt_api.py services/trade_orchestrator/test_mgmt_action_endpoint.py
git commit -m "$(cat <<'EOF'
feat(mgmt_api): accept optional direction_hint on /mgmt/action

Validated to exactly BUY or SELL at the HTTP boundary, same pattern as
percent's bounds check -- this value can originate from free-text
extraction, so a garbage value is a realistic input, not a theoretical one.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `direction_hint` filter inside `apply_mgmt_action`'s `close_now` branch

**Files:**
- Modify: `services/trade_orchestrator/trade_manager.py`
- Test: `services/trade_orchestrator/test_mgmt_action_endpoint.py`

**Interfaces:**
- Consumes: `MgmtActionRequest.direction_hint` (Task 2).
- Produces: `TradeManager.apply_mgmt_action(..., direction_hint: Optional[str] = None)`. New private helper `TradeManager._filter_groups_by_direction(group_ids: list[int], direction_hint: str) -> tuple[list[int], list[int]]` returning `(kept, excluded)`.

- [ ] **Step 1: Write the failing tests**

Add to `services/trade_orchestrator/test_mgmt_action_endpoint.py`:

```python
@pytest.mark.asyncio
async def test_direction_hint_filters_out_opposite_direction_group(tm_and_client):
    """
    Reproduces the 2026-09-17 incident: group 149 (SELL) and group 150 (BUY)
    both active in the same chat_id. A close_now with direction_hint=SELL
    (extracted from "XAUUSD SELL TRADE INVALID") must close only the SELL
    group and leave the BUY group untouched.
    """
    tm, client = tm_and_client
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="SELL", sl=2510.0, tp1=2490.0, tp2=2470.0, chat_id=CHAT_ID)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    resp = client.post("/mgmt/action", headers=HEADERS, json={
        "action": "close_now", "chat_id": CHAT_ID, "raw_text": "XAUUSD SELL TRADE INVALID / Close now",
        "correction": None, "direction_hint": "SELL",
    })

    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    remaining_directions = {t.direction for t in tm.trades.values()}
    assert remaining_directions == {"BUY"}


@pytest.mark.asyncio
async def test_close_now_without_direction_hint_closes_all_groups_of_the_chat(tm_and_client):
    """Baseline: omitting direction_hint keeps today's behavior (close everything)."""
    tm, client = tm_and_client
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="SELL", sl=2510.0, tp1=2490.0, tp2=2470.0, chat_id=CHAT_ID)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    resp = client.post("/mgmt/action", headers=HEADERS, json={
        "action": "close_now", "chat_id": CHAT_ID, "raw_text": "close now", "correction": None,
    })

    assert resp.status_code == 200
    assert len(tm.trades) == 0


@pytest.mark.asyncio
async def test_direction_hint_filter_to_zero_groups_returns_no_active_trade(tm_and_client):
    """A SELL hint when only a BUY group is open means the message applies to nothing."""
    tm, client = tm_and_client
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    resp = client.post("/mgmt/action", headers=HEADERS, json={
        "action": "close_now", "chat_id": CHAT_ID, "raw_text": "SELL TRADE INVALID / Close now",
        "correction": None, "direction_hint": "SELL",
    })

    assert resp.status_code == 200
    assert resp.json()["status"] == "no_active_trade"
    assert len(tm.trades) == 1  # the BUY group is untouched, not closed


@pytest.mark.asyncio
async def test_signal_correction_ignores_direction_hint_and_still_targets_most_recent_group(tm_and_client):
    """
    signal_correction must keep using group_ids[-1] regardless of
    direction_hint -- the filter lives only inside close_now (spec section
    6). Two groups of opposite directions; the correction targets the most
    recently opened one (BUY) even though direction_hint says SELL.
    """
    tm, client = tm_and_client
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="SELL", sl=2510.0, tp1=2490.0, tp2=2470.0, chat_id=CHAT_ID)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    most_recent_group_id = max(t.group_id for t in tm.trades.values())

    resp = client.post("/mgmt/action", headers=HEADERS, json={
        "action": "signal_correction", "chat_id": CHAT_ID, "raw_text": "tp1 es 2520",
        "correction": {"field": "tp1", "value": 2520.0}, "direction_hint": "SELL",
    })

    assert resp.status_code == 200
    assert resp.json()["group_id"] == most_recent_group_id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/trade_orchestrator/test_mgmt_action_endpoint.py -k "direction_hint or signal_correction_ignores" -v`
Expected: FAIL — `apply_mgmt_action() got an unexpected keyword argument 'direction_hint'` (from Task 2's wiring) or, once that's tolerated, the SELL/BUY groups both close because there's no filter yet.

- [ ] **Step 3: Implement**

In `services/trade_orchestrator/trade_manager.py`, add the helper near `find_active_groups_for_chat` (after line 584):

```python
    def _filter_groups_by_direction(self, group_ids: list[int], direction_hint: str) -> tuple[list[int], list[int]]:
        """
        Separa group_ids en (kept, excluded) segun si la direccion de sus
        legs coincide con direction_hint. Las dos piernas de un grupo
        comparten direccion, asi que basta inspeccionar cualquiera. Usado
        exclusivamente por la rama close_now de apply_mgmt_action -- ver
        docs/superpowers/specs/2026-09-17-direct-close-now-shortcut-design.md
        seccion 6 para por que no se aplica a otras acciones.
        """
        kept, excluded = [], []
        for group_id in group_ids:
            leg = next((t for t in self.trades.values() if t.group_id == group_id), None)
            if leg is not None and leg.direction == direction_hint:
                kept.append(group_id)
            else:
                excluded.append(group_id)
        return kept, excluded
```

Modify `apply_mgmt_action`'s signature (line 1298):

```python
    async def apply_mgmt_action(self, *, action: str, chat_id: str, raw_text: str, correction: Optional[dict], percent: Optional[float] = None, direction_hint: Optional[str] = None) -> dict:
```

Modify the `close_now` branch (starts at line 1316) to filter right after entering it, before the `results = []` loop:

```python
        if action == "close_now":
            if direction_hint:
                group_ids, excluded = self._filter_groups_by_direction(group_ids, direction_hint)
                if excluded:
                    await self._notify(
                        "mgmt_direction_filtered",
                        chat_id=chat_id, direction_hint=direction_hint, excluded_group_ids=excluded, action=action,
                        message=(f"Acción '{action}' limitada a grupos {direction_hint}. "
                                 f"Grupos excluidos por dirección opuesta: {excluded}."),
                    )
                if not group_ids:
                    log.info("[TM][MGMT] direction_hint=%s dejo cero grupos para chat_id=%s", direction_hint, chat_id)
                    await self._notify(
                        "mgmt_no_active_trade",
                        message=f"Acción '{action}' recibida pero no hay trades activos de dirección {direction_hint} para este chat. Texto: {raw_text!r}",
                        chat_id=chat_id, action=action,
                    )
                    return {"status": "no_active_trade"}
            results = []
            for group_id in group_ids:
                ...  # existing loop body unchanged
```

Do not touch `move_sl_be_now`, `note_sl_hit`, or `signal_correction` — they must keep using the original `group_ids` from `find_active_groups_for_chat`, unfiltered.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/trade_orchestrator/test_mgmt_action_endpoint.py -v`
Expected: PASS (all tests in the file, including the new ones and the pre-existing ones — confirm nothing regressed)

- [ ] **Step 5: Commit**

```bash
git add services/trade_orchestrator/trade_manager.py services/trade_orchestrator/test_mgmt_action_endpoint.py
git commit -m "$(cat <<'EOF'
fix(trade_manager): filter close_now by direction_hint

Reproduces and fixes the 2026-09-17 incident: a "SELL TRADE INVALID/Close
now" message closed both the SELL group it named AND an unrelated BUY
group opened minutes later in the same chat, because close_now closes
every active group of a chat_id with no direction filter.

The filter lives only inside the close_now branch, not before the action
switch -- signal_correction relies on group_ids[-1] targeting the most
recently opened group regardless of any direction hint, and filtering
earlier both breaks that semantics and can leave group_ids empty where
signal_correction doesn't guard against IndexError.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `TRADE_ORCHESTRATOR_MGMT_URL` config and `.env.example`

**Files:**
- Modify: `.env.example`

**Interfaces:**
- Produces: `TRADE_ORCHESTRATOR_MGMT_URL` documented in `.env.example`, read at runtime via `config.get("TRADE_ORCHESTRATOR_MGMT_URL", "")` (no code change needed in `services/common/config.py` — `ConfigProvider.get` already reads any environment variable by name).

- [ ] **Step 1: Check current `.env.example` structure**

Run: `grep -n "N8N_ACTION_API_KEY\|N8N_EVENT_WEBHOOK_URL" .env.example`

Confirm both exist so the new variable is added next to related config, following the file's existing grouping.

- [ ] **Step 2: Add the variable**

Add near `N8N_ACTION_API_KEY` in `.env.example`:

```
# URL interna de /mgmt/action en trade_orchestrator, usada por router_parser
# para ejecutar close_now directo cuando reconoce el patron "TRADE INVALID /
# Close now" sin pasar por n8n/Ollama (evita la ventana de carrera descrita
# en docs/superpowers/specs/2026-09-17-direct-close-now-shortcut-design.md).
# Puerto 8200: el que expone MGMT_API_PORT en trade_orchestrator/app.py y
# que docker-compose.yml publica como 8200:8200.
TRADE_ORCHESTRATOR_MGMT_URL=http://trade_orchestrator:8200/mgmt/action
```

- [ ] **Step 3: Verify the real `.env` on the VPS will need this too (manual note, not a code step)**

This is a deployment note, not a test: before this ships, `/root/apps/trading-platform/.env` on the VPS needs `TRADE_ORCHESTRATOR_MGMT_URL` added. Flag this in the PR description; do not edit the VPS `.env` from this task.

- [ ] **Step 4: Commit**

```bash
git add .env.example
git commit -m "$(cat <<'EOF'
docs(config): document TRADE_ORCHESTRATOR_MGMT_URL

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `N8N_ACTION_API_KEY` required by `validate_router_parser()`

**Files:**
- Modify: `services/common/env_validator.py`
- Test: check for an existing test file first (see Step 1)

**Interfaces:**
- Consumes: none new.
- Produces: `validate_router_parser()` raises `EnvError` if `N8N_ACTION_API_KEY` is unset or blank.

- [ ] **Step 1: Find the existing test file for env_validator**

Run: `find services/common -iname "*env_validator*"`

If a test file exists, add tests there. If none exists, create `services/common/test_env_validator.py` following the pattern of other `services/common` tests (check `services/common/test_signal_dedup.py` or similar for import style if present).

- [ ] **Step 2: Write the failing test**

```python
import os
import pytest

from services.common.env_validator import validate_router_parser, EnvError


def test_validate_router_parser_requires_n8n_action_api_key(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("N8N_ACTION_API_KEY", raising=False)
    with pytest.raises(EnvError, match="N8N_ACTION_API_KEY"):
        validate_router_parser()


def test_validate_router_parser_passes_with_n8n_action_api_key(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("N8N_ACTION_API_KEY", "some-key")
    validate_router_parser()  # must not raise
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest services/common/test_env_validator.py -k router_parser -v`
Expected: FAIL — `validate_router_parser` doesn't check for `N8N_ACTION_API_KEY` yet, so the first test doesn't raise.

- [ ] **Step 4: Implement**

In `services/common/env_validator.py`, modify `validate_router_parser()`:

```python
def validate_router_parser() -> None:
    """Valida variables requeridas por router_parser."""
    errors = []
    for name in ("REDIS_URL", "N8N_ACTION_API_KEY"):
        try:
            _require(name)
        except EnvError as e:
            errors.append(str(e))
    try:
        _require_positive_float("DEDUP_TTL_SECONDS", 120.0)
    except EnvError as e:
        errors.append(str(e))
    _report(errors, "router_parser")
```

(Only the `for name in (...)` tuple changes — `"REDIS_URL"` becomes `("REDIS_URL", "N8N_ACTION_API_KEY")`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest services/common/test_env_validator.py -k router_parser -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/common/env_validator.py services/common/test_env_validator.py
git commit -m "$(cat <<'EOF'
fix(env_validator): require N8N_ACTION_API_KEY for router_parser

router_parser is about to call trade_orchestrator's /mgmt/action directly
(direct close-now shortcut) and needs this key to authenticate. Fail at
startup rather than on the first real close attempt with a 401.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Direct-call function in router_parser with retries and failure notification

**Files:**
- Modify: `services/router_parser/app.py`
- Test: `services/router_parser/test_app.py`

**Interfaces:**
- Consumes: `match_close_now` (Task 1), `services.trade_orchestrator.n8n_retry_worker.enqueue` (existing).
- Produces: `async def execute_close_now_directly(chat_id: str, text: str, direction_hint: Optional[str], mgmt_url: str, action_api_key: str, redis_client) -> bool` — returns `True` if the direct call succeeded, `False` if it exhausted retries (notification already enqueued in that case).

**Cross-service import feasibility (verified, not an open question):**
`services/trade_orchestrator/` has no `__init__.py` (implicit namespace
package). `n8n_retry_worker.py` only imports stdlib (`asyncio`, `json`,
`logging`, `time`) plus a relative `from .audit_log import mark_dead_letter`,
and `audit_log.py` only imports `json`/`os`. Importing
`services.trade_orchestrator.n8n_retry_worker.enqueue` from `router_parser`
does **not** pull in `fastapi`, `pydantic`, or `mt5linux` — none of which are
in `services/router_parser/requirements.txt`. The Dockerfile already `COPY
services/ ./services/`, so the module is present in the built image; no
Dockerfile or requirements.txt change is needed for this import to work.

- [ ] **Step 1: Write the failing tests**

Add to `services/router_parser/test_app.py`:

```python
import asyncio
import json

from services.router_parser.app import execute_close_now_directly


class FakeRedisWithQueue(FakeRedis):
    def __init__(self):
        super().__init__()
        self.queue = []

    async def rpush(self, key, value):
        self.queue.append((key, value))


@pytest.mark.asyncio
async def test_execute_close_now_directly_posts_expected_payload(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    ok = await execute_close_now_directly(
        chat_id="-1003321565807", text="XAUUSD SELL TRADE INVALID / Close now",
        direction_hint="SELL", mgmt_url="http://trade_orchestrator:8200/mgmt/action",
        action_api_key="test-key", redis_client=FakeRedisWithQueue(),
    )

    assert ok is True
    assert captured["url"] == "http://trade_orchestrator:8200/mgmt/action"
    assert captured["json"]["action"] == "close_now"
    assert captured["json"]["chat_id"] == "-1003321565807"
    assert captured["json"]["direction_hint"] == "SELL"
    assert captured["headers"]["X-N8N-Action-Key"] == "test-key"


@pytest.mark.asyncio
async def test_execute_close_now_directly_retries_then_succeeds(monkeypatch):
    calls = {"count": 0}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        calls["count"] += 1
        class R:
            status_code = 200 if calls["count"] == 3 else 500
        return R()

    async def fake_sleep(seconds):
        pass  # don't actually wait in tests

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    ok = await execute_close_now_directly(
        chat_id="-1", text="TRADE INVALID / Close now", direction_hint=None,
        mgmt_url="http://x/mgmt/action", action_api_key="k", redis_client=FakeRedisWithQueue(),
    )

    assert ok is True
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_execute_close_now_directly_does_not_retry_on_4xx(monkeypatch):
    calls = {"count": 0}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        calls["count"] += 1
        class R:
            status_code = 401
        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    redis = FakeRedisWithQueue()
    ok = await execute_close_now_directly(
        chat_id="-1", text="TRADE INVALID / Close now", direction_hint=None,
        mgmt_url="http://x/mgmt/action", action_api_key="wrong-key", redis_client=redis,
    )

    assert ok is False
    assert calls["count"] == 1
    assert len(redis.queue) == 1


@pytest.mark.asyncio
async def test_execute_close_now_directly_enqueues_notification_after_exhausting_retries(monkeypatch):
    async def fake_post(self, url, json=None, headers=None, timeout=None):
        class R:
            status_code = 500
        return R()

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    redis = FakeRedisWithQueue()
    ok = await execute_close_now_directly(
        chat_id="-1003321565807", text="TRADE INVALID / Close now", direction_hint="SELL",
        mgmt_url="http://x/mgmt/action", action_api_key="k", redis_client=redis,
    )

    assert ok is False
    assert len(redis.queue) == 1
    key, raw = redis.queue[0]
    assert key == "n8n_event_retry_queue"
    item = json.loads(raw)
    envelope = item["envelope"]
    assert envelope["event_type"] == "mgmt_direct_close_failed"
    assert envelope["channel"] == "both"
    assert "-1003321565807" in envelope["message"]
    assert "REVISAR LA CUENTA MANUALMENTE" in envelope["message"]
    assert envelope["payload"]["chat_id"] == "-1003321565807"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/router_parser/test_app.py -k execute_close_now_directly -v`
Expected: FAIL with `ImportError: cannot import name 'execute_close_now_directly'`

- [ ] **Step 3: Implement**

Add to `services/router_parser/app.py`, near `forward_to_n8n` (after line 45):

```python
import asyncio
import uuid
from datetime import datetime, timezone

from services.trade_orchestrator.n8n_retry_worker import enqueue as enqueue_n8n_event

CLOSE_NOW_RETRY_BACKOFF_SECONDS = [1, 2, 4]


async def execute_close_now_directly(
    chat_id: str, text: str, direction_hint, mgmt_url: str, action_api_key: str, redis_client,
) -> bool:
    """
    Ejecuta close_now directo contra /mgmt/action de trade_orchestrator,
    sin pasar por n8n/Ollama -- el patron "TRADE INVALID/Close now" es
    literal y no requiere clasificacion. Reintenta ante error de red,
    timeout o 5xx; NO reintenta ante 4xx (error de configuracion). Si se
    agotan los reintentos o llega un 4xx, encola una notificacion en la
    misma cola de reintentos que usa EventBus, para que el worker que ya
    corre en trade_orchestrator la entregue -- nunca cae a n8n (ver
    docs/superpowers/specs/2026-09-17-direct-close-now-shortcut-design.md
    seccion 5).
    """
    payload = {"action": "close_now", "chat_id": chat_id, "raw_text": text}
    if direction_hint:
        payload["direction_hint"] = direction_hint
    headers = {"X-N8N-Action-Key": action_api_key}

    # 3 intentos totales, con backoff SOLO entre intentos (no antes del
    # primero): intento 1 inmediato, intento 2 tras 1s, intento 3 tras 2s.
    # CLOSE_NOW_RETRY_BACKOFF_SECONDS[attempt - 1] indexa el gap que
    # PRECEDE al intento actual -- por eso el loop nunca consume el 4s
    # final de la constante con solo 3 intentos; ese tercer valor queda
    # disponible si el numero de intentos crece en el futuro.
    last_error = None
    for attempt in range(3):
        if attempt > 0:
            await asyncio.sleep(CLOSE_NOW_RETRY_BACKOFF_SECONDS[attempt - 1])
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(mgmt_url, json=payload, headers=headers, timeout=10.0)
            if 200 <= resp.status_code < 300:
                return True
            last_error = f"HTTP {resp.status_code}"
            if 400 <= resp.status_code < 500:
                break  # config error, retrying won't help
        except Exception as e:
            last_error = str(e)

    await _enqueue_close_now_failure(redis_client, chat_id=chat_id, raw_text=text, direction_hint=direction_hint, error=last_error)
    return False


async def _enqueue_close_now_failure(redis_client, *, chat_id: str, raw_text: str, direction_hint, error: str) -> None:
    envelope = {
        "event_id": str(uuid.uuid4()),
        "event_type": "mgmt_direct_close_failed",
        "channel": "both",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": (
            f"\U0001F6A8 CIERRE AUTOMÁTICO FALLIDO — Canal: {chat_id}\n"
            f"Motivo: \"{raw_text}\"\n"
            f"No se pudo ejecutar el cierre tras 3 intentos: {error}\n"
            f"REVISAR LA CUENTA MANUALMENTE — las posiciones pueden seguir abiertas."
        ),
        "payload": {"chat_id": chat_id, "raw_text": raw_text, "direction_hint": direction_hint, "error": error},
    }
    try:
        await enqueue_n8n_event(redis_client, envelope)
    except Exception as e:
        log.error("[CLOSE_NOW_DIRECT] no se pudo encolar la notificacion de fallo: %s", e)
```

This gives attempt 1 immediately, attempt 2 after 1s, attempt 3 after 2s — 2
backoff gaps for 3 attempts, matching
`test_execute_close_now_directly_retries_then_succeeds`'s expectation of
exactly 3 calls to `httpx.AsyncClient.post` before the third one succeeds.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/router_parser/test_app.py -k execute_close_now_directly -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add services/router_parser/app.py services/router_parser/test_app.py
git commit -m "$(cat <<'EOF'
feat(router_parser): direct close_now call with retries and failure queue

execute_close_now_directly() posts straight to trade_orchestrator's
/mgmt/action, bypassing n8n entirely for the TRADE INVALID/Close now
pattern -- 3 attempts with 1s/2s backoff, no retry on 4xx. On exhausted
retries or a 4xx, enqueues a notification via the existing n8n event retry
queue rather than posting directly (EventBus writes the audit log
synchronously before enqueueing, and router_parser doesn't mount ./data,
so it can't write that log itself) or falling back to n8n (which would
reintroduce the exact race this shortcut exists to close).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Wire the new branch into the main loop, excluding n8n

**Files:**
- Modify: `services/router_parser/app.py`
- Test: `services/router_parser/test_app.py`

**Interfaces:**
- Consumes: `match_close_now` (Task 1), `execute_close_now_directly` (Task 6).
- Produces: updated `main()` loop behavior — no new public interface (this task changes control flow, not exposed functions).

- [ ] **Step 1: Write the failing tests**

Since `main()`'s loop isn't directly unit-testable (it's an infinite `async for` over Redis Streams), extract the per-message dispatch into a testable function first. Add to `services/router_parser/test_app.py`:

```python
from services.router_parser.app import dispatch_raw_message, DUPLICATE_SIGNAL


class RecordingRouter:
    """Stand-in for SignalRouter that returns a fixed process_raw_signal result."""
    def __init__(self, result):
        self.result = result

    async def process_raw_signal(self, chat_id, text):
        return self.result


@pytest.mark.asyncio
async def test_dispatch_raw_message_runs_close_now_directly_and_skips_n8n(monkeypatch):
    forwarded = []
    direct_calls = []

    async def fake_forward(text, chat_id, webhook_url):
        forwarded.append((text, chat_id, webhook_url))

    async def fake_execute(chat_id, text, direction_hint, mgmt_url, action_api_key, redis_client):
        direct_calls.append((chat_id, text, direction_hint))
        return True

    monkeypatch.setattr("services.router_parser.app.forward_to_n8n", fake_forward)
    monkeypatch.setattr("services.router_parser.app.execute_close_now_directly", fake_execute)

    router = RecordingRouter(None)  # not a recognized signal
    redis = FakeRedis()
    await dispatch_raw_message(
        router=router, redis_client=redis, chat_id="-1003321565807",
        text="XAUUSD SELL TRADE INVALID ❌\n\nClose now",
        n8n_webhook_url="https://n8n.example.com/in",
        mgmt_url="http://trade_orchestrator:8200/mgmt/action", action_api_key="test-key",
    )

    assert direct_calls == [("-1003321565807", "XAUUSD SELL TRADE INVALID ❌\n\nClose now", "SELL")]
    assert forwarded == []


@pytest.mark.asyncio
async def test_dispatch_raw_message_forwards_unrecognized_text_to_n8n(monkeypatch):
    forwarded = []
    direct_calls = []

    async def fake_forward(text, chat_id, webhook_url):
        forwarded.append((text, chat_id, webhook_url))

    async def fake_execute(**kwargs):
        direct_calls.append(kwargs)
        return True

    monkeypatch.setattr("services.router_parser.app.forward_to_n8n", fake_forward)
    monkeypatch.setattr("services.router_parser.app.execute_close_now_directly", fake_execute)

    router = RecordingRouter(None)
    redis = FakeRedis()
    await dispatch_raw_message(
        router=router, redis_client=redis, chat_id="-1",
        text="HIT SL. GET READY FOR RECOVERY",
        n8n_webhook_url="https://n8n.example.com/in",
        mgmt_url="http://trade_orchestrator:8200/mgmt/action", action_api_key="test-key",
    )

    assert direct_calls == []
    assert forwarded == [("HIT SL. GET READY FOR RECOVERY", "-1", "https://n8n.example.com/in")]


@pytest.mark.asyncio
async def test_dispatch_raw_message_skips_close_now_check_for_recognized_signals(monkeypatch):
    """A text that already parses as a signal must never be evaluated as a close_now candidate."""
    forwarded = []
    direct_calls = []

    async def fake_forward(text, chat_id, webhook_url):
        forwarded.append((text, chat_id, webhook_url))

    async def fake_execute(**kwargs):
        direct_calls.append(kwargs)
        return True

    monkeypatch.setattr("services.router_parser.app.forward_to_n8n", fake_forward)
    monkeypatch.setattr("services.router_parser.app.execute_close_now_directly", fake_execute)

    sig = {"symbol": "XAUUSD", "direction": "SELL", "provider_tag": "TRADE_PULSE", "format_tag": "TRADEPULSE"}
    router = RecordingRouter(sig)
    redis = FakeRedis()

    published = []
    async def fake_xadd(r, stream, fields):
        published.append((stream, fields))
    monkeypatch.setattr("services.router_parser.app.xadd", fake_xadd)

    await dispatch_raw_message(
        router=router, redis_client=redis, chat_id="-1",
        text="XAUUSD SELL NOW",
        n8n_webhook_url="https://n8n.example.com/in",
        mgmt_url="http://trade_orchestrator:8200/mgmt/action", action_api_key="test-key",
    )

    assert direct_calls == []
    assert forwarded == []
    assert len(published) == 1


@pytest.mark.asyncio
async def test_dispatch_raw_message_skips_duplicate_signal_without_forwarding_or_direct_call(monkeypatch):
    forwarded = []
    direct_calls = []

    async def fake_forward(text, chat_id, webhook_url):
        forwarded.append((text, chat_id, webhook_url))

    async def fake_execute(**kwargs):
        direct_calls.append(kwargs)
        return True

    monkeypatch.setattr("services.router_parser.app.forward_to_n8n", fake_forward)
    monkeypatch.setattr("services.router_parser.app.execute_close_now_directly", fake_execute)

    router = RecordingRouter(DUPLICATE_SIGNAL)
    redis = FakeRedis()
    await dispatch_raw_message(
        router=router, redis_client=redis, chat_id="-1", text="XAUUSD SELL NOW",
        n8n_webhook_url="https://n8n.example.com/in",
        mgmt_url="http://trade_orchestrator:8200/mgmt/action", action_api_key="test-key",
    )

    assert direct_calls == []
    assert forwarded == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/router_parser/test_app.py -k dispatch_raw_message -v`
Expected: FAIL with `ImportError: cannot import name 'dispatch_raw_message'`

- [ ] **Step 3: Implement**

In `services/router_parser/app.py`, add the import for `match_close_now` near the top (with the other `parsers_*` imports):

```python
from parsers_management import match_close_now
```

Extract the per-message body of the `main()` loop into a new function, placed after `execute_close_now_directly`/`_enqueue_close_now_failure` and before `main()`:

```python
async def dispatch_raw_message(
    *, router: "SignalRouter", redis_client, chat_id: str, text: str,
    n8n_webhook_url: str, mgmt_url: str, action_api_key: str,
) -> None:
    """
    Un mensaje crudo de Streams.RAW, ya sea senal o gestion. Cuatro casos,
    mutuamente excluyentes:
      1. Señal reconocida pero duplicada -- ya se proceso, no reenviar a n8n.
      2. Señal reconocida -- publicar a Streams.SIGNALS.
      3. Patron "TRADE INVALID/Close now" -- ejecutar close_now directo,
         NUNCA reenviar a n8n (ver spec 2026-09-17).
      4. Cualquier otro texto no vacio -- reenviar a n8n/Ollama.
    """
    sig = await router.process_raw_signal(chat_id, text)
    if sig is DUPLICATE_SIGNAL:
        return
    if sig:
        trace_id = uuid.uuid4().hex[:8]
        sig["chat_id"] = chat_id
        sig["raw_text"] = text
        sig["trace"] = trace_id
        await xadd(redis_client, Streams.SIGNALS, sig)
        log.info(f"[SIGNAL] trace={trace_id} {sig['provider_tag']} {sig['direction']} {sig['symbol']}")
        return

    close_now = match_close_now(text)
    if close_now:
        if mgmt_url:
            await execute_close_now_directly(
                chat_id=chat_id, text=text, direction_hint=close_now["direction_hint"],
                mgmt_url=mgmt_url, action_api_key=action_api_key, redis_client=redis_client,
            )
        else:
            log.error("[CLOSE_NOW_DIRECT] TRADE_ORCHESTRATOR_MGMT_URL no configurada — reenviando a n8n como fallback: %r", text[:80])
            if n8n_webhook_url:
                await forward_to_n8n(text, chat_id, n8n_webhook_url)
        return

    if text.strip():
        if n8n_webhook_url:
            await forward_to_n8n(text, chat_id, n8n_webhook_url)
        else:
            log.warning("[N8N_FORWARD] N8N_INBOUND_WEBHOOK_URL no configurada — mensaje descartado: %r", text[:80])
```

Now replace the body of the `xreadgroup_loop` block in `main()` (lines 156-179) to call it:

```python
            async for msg_id, fields in xreadgroup_loop(r, Streams.RAW, group, consumer):
                text = fields.get("text", "")
                chat_id = fields.get("chat_id", "")
                try:
                    await dispatch_raw_message(
                        router=router, redis_client=r, chat_id=chat_id, text=text,
                        n8n_webhook_url=n8n_webhook_url, mgmt_url=mgmt_url, action_api_key=action_api_key,
                    )
                finally:
                    await xack(r, Streams.RAW, group, msg_id)
```

And in `main()`, read the two new config values near where `n8n_webhook_url` is read (line 150):

```python
    n8n_webhook_url = _config.get("N8N_INBOUND_WEBHOOK_URL", "")
    mgmt_url = _config.get("TRADE_ORCHESTRATOR_MGMT_URL", "")
    action_api_key = _config.get("N8N_ACTION_API_KEY", "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/router_parser/test_app.py -v`
Expected: PASS (entire file — confirm no regressions in the pre-existing tests, which don't reference `dispatch_raw_message` and must keep passing unchanged)

- [ ] **Step 5: Commit**

```bash
git add services/router_parser/app.py services/router_parser/test_app.py
git commit -m "$(cat <<'EOF'
feat(router_parser): wire close_now shortcut into the main loop

dispatch_raw_message() extracts the per-message branch from main()'s loop
into a testable function with a fourth case: a TRADE INVALID/Close now
match now runs execute_close_now_directly() and returns without ever
calling forward_to_n8n, mirroring the DUPLICATE_SIGNAL precedent already in
this loop ("already handled, don't forward as unrecognized noise").

Falls back to forwarding to n8n only if TRADE_ORCHESTRATOR_MGMT_URL isn't
configured -- the sole path by which a matching message still reaches n8n,
and it's a configuration gap, not a runtime failure.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Full-suite regression check and manual VPS dry-run note

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite for both touched services**

Run: `pytest services/router_parser/ services/trade_orchestrator/ services/common/ -v`
Expected: PASS, zero failures, zero errors.

- [ ] **Step 2: Run the full repo test suite excluding integration tests**

Run: `pytest -m "not integration" -v`
Expected: PASS. If any unrelated test fails, stop and investigate before proceeding — do not assume it's pre-existing without checking `git stash` + re-run to confirm.

- [ ] **Step 3: Grep for any other caller of `apply_mgmt_action` that might need `direction_hint` awareness**

Run: `grep -rn "apply_mgmt_action(" --include="*.py" .`

Confirm the only callers are `mgmt_api.py` (updated in Task 2) and test files (updated in Task 3). If `tests/e2e/scenarios/_management_common.py` or similar calls it directly, check whether it needs updating to pass `direction_hint=None` explicitly (it shouldn't need to, since the parameter is optional with a default — but confirm no positional-argument assumption breaks).

- [ ] **Step 4: Write the deployment checklist as a plain note (not a file) for the PR description**

Confirm these two manual steps are called out when this ships (do not perform them from this task — they touch the production VPS):
1. Add `TRADE_ORCHESTRATOR_MGMT_URL=http://trade_orchestrator:8200/mgmt/action` to `/root/apps/trading-platform/.env` on the VPS.
2. Restart `atp-router-parser` (and `atp-trade-orchestrator` if `mgmt_api.py`/`trade_manager.py` changed) after deploying, since `validate_router_parser()` will now fail closed if `N8N_ACTION_API_KEY` is somehow missing from that `.env` (it already isn't, per the earlier verification, so this should be a no-op check, not an expected failure).

- [ ] **Step 5: No commit for this task** — it's verification-only. If Step 3 uncovers a real gap, create a follow-up task before merging rather than silently patching here.

---

## Self-Review Notes

**Spec coverage:**
- §4 (regex + extraction) → Task 1.
- §5 (direct call, config, retries, failure notification, env validator) → Tasks 4, 5, 6, 7.
- §6 (direction_hint contract + close_now-only filter + rationale for excluding other branches) → Tasks 2, 3.
- §7 (incident reproduction) → Task 3's `test_direction_hint_filters_out_opposite_direction_group`.
- §8 (testing) → covered across Tasks 1, 2, 3, 6, 7.
- §9 (backwards compatibility, `N8N_ACTION_API_KEY` requirement) → Task 5; the "no code change needed in `docker-compose.yml`" note is captured in the File Structure table.

**Type consistency check:** `match_close_now` returns `direction_hint` as `Optional[str]` (Task 1) → `execute_close_now_directly` takes `direction_hint` positionally-by-keyword as the same type (Task 6) → `MgmtActionRequest.direction_hint: Optional[str]` (Task 2) → `apply_mgmt_action(..., direction_hint: Optional[str] = None)` (Task 3). Names match throughout: `direction_hint` everywhere, never renamed.

**Placeholder scan:** no TBD/TODO; every step has runnable code or an exact grep/pytest command.
