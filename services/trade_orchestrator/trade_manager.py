# trade_manager.py
from __future__ import annotations

import asyncio
import time
import re
from dataclasses import dataclass, field
from typing import Optional

import mt5_constants as mt5

@dataclass
class ManagedTrade:
    account_name: str
    ticket: int
    symbol: str
    direction: str
    provider_tag: str
    group_id: int

    tps: list[float] = field(default_factory=list)
    planned_sl: Optional[float] = None
    tp_hit: set[int] = field(default_factory=set)  # indices 1..N

    mfe_peak_price: Optional[float] = None
    runner_enabled: bool = False
    initial_volume: Optional[float] = None
    entry_price: Optional[float] = None

    addon_done: bool = False
    opened_ts: float = field(default_factory=lambda: time.time())

    last_trailing_sl: Optional[float] = None
    last_trailing_ts: float = 0.0

    # ✅ dedup acciones por trade (gestión por mensajes)
    actions_done: set[str] = field(default_factory=set)


class TradeManager:
    def __init__(
        self,
        mt5_exec,
        *,
        magic: int = 987654,
        loop_sleep_sec: float = 1.0,

        scalp_tp1_percent: int = 60,
        scalp_tp2_percent: int = 100,

        long_tp1_percent: int = 50,
        long_tp2_percent: int = 50,
        runner_retrace_pips: float = 10,
        buffer_pips: float = 2.0,

        enable_be_after_tp1: bool = True,
        be_offset_pips: float = 3.0,

        enable_trailing: bool = True,
        trailing_activation_after_tp2: bool = True,
        trailing_activation_pips: float = 30.0,
        trailing_stop_pips: float = 15.0,

        trailing_min_change_pips: float = 1.0,
        trailing_cooldown_sec: float = 2.0,

        deviation: int = 20,

        # ✅ addon midpoint entry–SL
        enable_addon: bool = True,
        addon_max: int = 1,
        addon_lot_factor: float = 0.5,
        addon_min_seconds_from_open: int = 5,
        addon_entry_sl_ratio: float = 0.5,  # 0.5 = mitad entre entry y SL

        # ✅ TOROFX management defaults
        torofx_partial_default_percent: int = 30,  # “tomar parcial…” sin %
        torofx_partial_min_pips: float = 50.0,     # “+50/60” -> usa 50 por defecto
        torofx_close_entry_tolerance_pips: float = 10.0,  # para “cierro mi entrada 4330”
        torofx_provider_tag_match: str = "TOROFX",  # substring en provider_tag

        notifier=None,
        notify_connect: bool | None = None,  # compat
    ):
        self.mt5 = mt5_exec
        self.magic = magic
        self.loop_sleep_sec = loop_sleep_sec

        self.scalp_tp1_percent = scalp_tp1_percent
        self.scalp_tp2_percent = scalp_tp2_percent

        self.long_tp1_percent = long_tp1_percent
        self.long_tp2_percent = long_tp2_percent
        self.runner_retrace_pips = runner_retrace_pips
        self.buffer_pips = buffer_pips

        self.enable_be_after_tp1 = enable_be_after_tp1
        self.be_offset_pips = be_offset_pips

        self.enable_trailing = enable_trailing
        self.trailing_activation_after_tp2 = trailing_activation_after_tp2
        self.trailing_activation_pips = trailing_activation_pips
        self.trailing_stop_pips = trailing_stop_pips

        self.trailing_min_change_pips = trailing_min_change_pips
        self.trailing_cooldown_sec = trailing_cooldown_sec

        self.deviation = deviation

        self.enable_addon = enable_addon
        self.addon_max = int(addon_max)
        self.addon_lot_factor = float(addon_lot_factor)
        self.addon_min_seconds_from_open = int(addon_min_seconds_from_open)
        self.addon_entry_sl_ratio = float(addon_entry_sl_ratio)

        self.torofx_partial_default_percent = int(torofx_partial_default_percent)
        self.torofx_partial_min_pips = float(torofx_partial_min_pips)
        self.torofx_close_entry_tolerance_pips = float(torofx_close_entry_tolerance_pips)
        self.torofx_provider_tag_match = (torofx_provider_tag_match or "TOROFX").upper()

        self.notifier = notifier
        self.trades: dict[int, ManagedTrade] = {}

        self.group_addon_count: dict[tuple[str, int], int] = {}

    # ----------------------------
    # Notifier
    # ----------------------------
    def _notify_bg(self, account_name: str, message: str):
        if not self.notifier:
            return
        try:
            asyncio.create_task(self.notifier.notify(account_name, message))
        except RuntimeError:
            print(f"[NOTIFY][NO_LOOP] {account_name}: {message}")

    # ----------------------------
    # Register
    # ----------------------------
    def register_trade(
        self,
        *,
        account_name: str,
        ticket: int,
        symbol: str,
        direction: str,
        provider_tag: str,
        tps: list[float],
        planned_sl: Optional[float] = None,
        group_id: Optional[int] = None,
    ) -> None:
        tkt = int(ticket)

        gid: int
        if group_id is None and self._looks_like_recovery(provider_tag):
            gid = self._infer_group_for_recovery(account_name, symbol, direction) or tkt
        else:
            gid = int(group_id) if group_id is not None else tkt

        self.trades[tkt] = ManagedTrade(
            account_name=account_name,
            ticket=tkt,
            symbol=symbol,
            direction=direction,
            provider_tag=provider_tag,
            group_id=gid,
            tps=list(tps or []),
            planned_sl=float(planned_sl) if planned_sl is not None else None,
        )

        self.group_addon_count.setdefault((account_name, gid), 0)

        print(f"[TM] ✅ registered ticket={tkt} acct={account_name} group={gid} provider={provider_tag} tps={tps} planned_sl={planned_sl}")

    def update_trade_signal(self, *, ticket: int, tps: list[float], planned_sl: Optional[float], provider_tag: Optional[str] = None):
        t = self.trades.get(int(ticket))
        if not t:
            return
        t.tps = list(tps or [])
        t.planned_sl = float(planned_sl) if planned_sl is not None else None
        if provider_tag:
            t.provider_tag = provider_tag

    def _looks_like_recovery(self, provider_tag: str) -> bool:
        up = (provider_tag or "").upper()
        return ("RECOVERY" in up) or (up.startswith("REC")) or (" REC " in up)

    def _infer_group_for_recovery(self, account_name: str, symbol: str, direction: str) -> Optional[int]:
        candidates = [
            t for t in self.trades.values()
            if t.account_name == account_name and t.symbol == symbol and t.direction == direction
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x.opened_ts, reverse=True)
        return int(candidates[0].group_id)

    # ----------------------------
    # Loop
    # ----------------------------
    async def run_forever(self):
        while True:
            await self._tick_once()
            await asyncio.sleep(self.loop_sleep_sec)

    async def _tick_once(self):
        for account in [a for a in self.mt5.accounts if a.get("active")]:
            if not self.mt5.connect_to_account(account):
                continue

            positions = self.mt5.positions_get()
            if not positions:
                for ticket in list(self.trades.keys()):
                    if self.trades[ticket].account_name == account["name"]:
                        del self.trades[ticket]
                continue

            pos_by_ticket = {p.ticket: p for p in positions}

            # remove closed
            for ticket in list(self.trades.keys()):
                t = self.trades[ticket]
                if t.account_name != account["name"]:
                    continue
                if ticket not in pos_by_ticket:
                    del self.trades[ticket]

            # manage
            for ticket, t in list(self.trades.items()):
                if t.account_name != account["name"]:
                    continue

                pos = pos_by_ticket.get(ticket)
                if not pos:
                    continue
                if pos.magic != self.mt5.magic:
                    continue

                info = self.mt5.symbol_info(t.symbol)
                tick = self.mt5.symbol_info_tick(t.symbol)
                if not info or not tick:
                    continue

                point = float(info.point)
                is_buy = (t.direction == "BUY")
                current = float(pos.price_current)

                if t.entry_price is None:
                    t.entry_price = float(pos.price_open)
                if t.initial_volume is None:
                    t.initial_volume = float(pos.volume)

                # 1) TP management
                self._maybe_take_profits(account, pos, point, is_buy, current, t)

                # 2) Addon midpoint
                if self.enable_addon:
                    self._maybe_addon_midpoint(account, pos, point, is_buy, current, t)

                # 3) Trailing
                if self.enable_trailing:
                    self._maybe_trailing(account, pos, point, is_buy, current, t)

    # ----------------------------
    # Helpers
    # ----------------------------
    def _is_long_mode(self, t: ManagedTrade) -> bool:
        return len(t.tps) >= 3

    def _tp_hit(self, is_buy: bool, current: float, tp: float, buffer_price: float) -> bool:
        if is_buy:
            return current >= (tp - buffer_price)
        return current <= (tp + buffer_price)

    def _do_be(self, account: dict, ticket: int, point: float, is_buy: bool):
        pos_list = self.mt5.positions_get(ticket=int(ticket))
        if not pos_list:
            return
        pos = pos_list[0]
        entry = float(pos.price_open)
        offset = self.be_offset_pips * point
        be = (entry + offset) if is_buy else (entry - offset)

        req = {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket), "sl": float(be), "tp": 0.0}
        res = self.mt5.order_send(req)
        ok = bool(res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL))
        if ok:
            self._notify_bg(account["name"], f"✅ BE aplicado | Ticket: {int(ticket)} | SL: {be:.5f}")
        else:
            self._notify_bg(
                account["name"],
                f"❌ BE falló | Ticket: {int(ticket)}\nretcode={getattr(res,'retcode',None)} {getattr(res,'comment',None)}"
            )

    def _effective_close_percent(self, ticket: int, desired_percent: int) -> int:
        if desired_percent >= 100:
            return 100

        pos_list = self.mt5.positions_get(ticket=int(ticket))
        if not pos_list:
            return desired_percent
        pos = pos_list[0]

        info = self.mt5.symbol_info(pos.symbol)
        if not info:
            return desired_percent

        v = float(pos.volume)
        step = float(info.volume_step) if float(info.volume_step) > 0 else 0.0
        vmin = float(info.volume_min) if float(info.volume_min) > 0 else 0.0

        if v <= 0 or step <= 0 or vmin <= 0:
            return desired_percent

        raw_close = v * (float(desired_percent) / 100.0)
        close_vol = step * round(raw_close / step)

        if close_vol < vmin or close_vol <= 0:
            return 100

        remaining = v - close_vol
        if remaining > 0 and remaining < vmin:
            return 100

        return desired_percent

    def _do_partial_close(self, account: dict, ticket: int, percent: int, reason: str):
        ok = self.mt5.partial_close(account=account, ticket=int(ticket), percent=int(percent))
        if ok:
            print(f"[TM] 🎯 partial_close ticket={int(ticket)} percent={int(percent)} reason={reason}")
        else:
            print(f"[TM] ❌ partial_close FAILED ticket={int(ticket)} percent={int(percent)} reason={reason}")

    # ----------------------------
    # TP / Runner / BE
    # ----------------------------
    def _maybe_take_profits(self, account: dict, pos, point: float, is_buy: bool, current: float, t: ManagedTrade):
        if not t.tps:
            return

        buffer_price = self.buffer_pips * point
        long_mode = self._is_long_mode(t)

        if long_mode:
            if t.mfe_peak_price is None:
                t.mfe_peak_price = current
            else:
                if is_buy and current > t.mfe_peak_price:
                    t.mfe_peak_price = current
                if (not is_buy) and current < t.mfe_peak_price:
                    t.mfe_peak_price = current

        # TP1
        if 1 not in t.tp_hit and len(t.tps) >= 1 and self._tp_hit(is_buy, current, float(t.tps[0]), buffer_price):
            t.tp_hit.add(1)
            pct = self.long_tp1_percent if long_mode else self.scalp_tp1_percent
            pct_eff = self._effective_close_percent(ticket=int(pos.ticket), desired_percent=int(pct))
            self._do_partial_close(account, pos.ticket, pct_eff, reason=f"TP1 ({pct}% -> {pct_eff}%)")

            if self.enable_be_after_tp1 and pct_eff < 100:
                self._do_be(account, pos.ticket, point, is_buy)

            self._notify_bg(
                account["name"],
                f"🎯 TP1 detectado | Ticket: {int(pos.ticket)} | {t.symbol} | {t.direction}\n"
                f"TP1={float(t.tps[0]):.5f} | Cierre: {pct_eff}%"
            )

        # TP2
        if 2 not in t.tp_hit and len(t.tps) >= 2 and self._tp_hit(is_buy, current, float(t.tps[1]), buffer_price):
            t.tp_hit.add(2)

            if long_mode:
                pct = self.long_tp2_percent
                pct_eff = self._effective_close_percent(ticket=int(pos.ticket), desired_percent=int(pct))
                self._do_partial_close(account, pos.ticket, pct_eff, reason=f"TP2 ({pct}% -> {pct_eff}%)")
                t.runner_enabled = True
            else:
                pct = self.scalp_tp2_percent
                self._do_partial_close(account, pos.ticket, pct, reason=f"TP2 ({pct}%)")

        # TP3 (long)
        if long_mode and 3 not in t.tp_hit and len(t.tps) >= 3 and self._tp_hit(is_buy, current, float(t.tps[2]), buffer_price):
            t.tp_hit.add(3)
            self._do_partial_close(account, pos.ticket, 100, reason="TP3 (100%)")
            return

        # Runner retrace
        if long_mode and t.runner_enabled and t.mfe_peak_price is not None:
            retrace_price = self.runner_retrace_pips * point
            if is_buy and (t.mfe_peak_price - current) >= retrace_price:
                self._do_partial_close(account, pos.ticket, 100, reason="RUNNER retrace")
            if (not is_buy) and (current - t.mfe_peak_price) >= retrace_price:
                self._do_partial_close(account, pos.ticket, 100, reason="RUNNER retrace")

    # ----------------------------
    # ✅ Addon MIDPOINT Entry–SL (NO pirámide en ganancia)
    # ----------------------------
    def _maybe_addon_midpoint(self, account: dict, pos, point: float, is_buy: bool, current: float, t: ManagedTrade):
        if not self.enable_addon or self.addon_max <= 0:
            return

        if "-ADDON" in (t.provider_tag or "").upper():
            return

        if (time.time() - t.opened_ts) < self.addon_min_seconds_from_open:
            return

        gid = int(t.group_id)
        gkey = (account["name"], gid)
        used = int(self.group_addon_count.get(gkey, 0))
        if used >= int(self.addon_max):
            return

        entry = float(pos.price_open)
        sl_pos = float(pos.sl) if float(pos.sl) != 0.0 else 0.0
        sl = sl_pos if sl_pos != 0.0 else (float(t.planned_sl) if t.planned_sl is not None else 0.0)
        if sl == 0.0:
            return

        if is_buy and not (entry > sl):
            return
        if (not is_buy) and not (entry < sl):
            return

        r = self.addon_entry_sl_ratio
        r = max(0.0, min(1.0, r))

        addon_level = (1.0 - r) * entry + r * sl
        buffer_price = self.buffer_pips * point

        trigger = (current <= addon_level + buffer_price) if is_buy else (current >= addon_level - buffer_price)
        if not trigger:
            return

        if is_buy and current <= sl + (2.0 * buffer_price):
            return
        if (not is_buy) and current >= sl - (2.0 * buffer_price):
            return

        info = self.mt5.symbol_info(t.symbol)
        tick = self.mt5.symbol_info_tick(t.symbol)
        if not info or not tick:
            return

        base_vol = float(pos.volume)
        add_vol = base_vol * float(self.addon_lot_factor)

        step = float(info.volume_step) if float(info.volume_step) > 0 else 0.0
        vmin = float(info.volume_min) if float(info.volume_min) > 0 else 0.0
        vmax = float(info.volume_max) if float(info.volume_max) > 0 else 0.0

        if step > 0:
            add_vol = step * round(add_vol / step)

        if vmin > 0 and add_vol < vmin:
            return

        if vmax > 0:
            add_vol = min(vmax, add_vol)
        if vmin > 0:
            add_vol = max(vmin, add_vol)

        if add_vol <= 0:
            return

        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if is_buy else tick.bid)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": t.symbol,
            "volume": float(add_vol),
            "type": order_type,
            "price": price,
            "sl": float(sl) if sl else 0.0,
            "tp": 0.0,
            "deviation": int(self.deviation),
            "magic": int(self.mt5.magic),
            "comment": (f"{self.mt5.comment_prefix}-ADDON"[:31] if hasattr(self.mt5, "comment_prefix") else "YsaCopy-ADDON"),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": getattr(self.mt5, "_best_filling", lambda s: mt5.ORDER_FILLING_IOC)(t.symbol),
        }

        send = getattr(self.mt5, "_order_send_with_filling_fallback", None)
        res = send(req) if callable(send) else self.mt5.order_send(req)

        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            addon_ticket = int(res.order)

            self.group_addon_count[gkey] = used + 1
            t.addon_done = True

            self.register_trade(
                account_name=account["name"],
                ticket=addon_ticket,
                symbol=t.symbol,
                direction=t.direction,
                provider_tag=f"{t.provider_tag}-ADDON",
                tps=list(t.tps),
                planned_sl=t.planned_sl,
                group_id=t.group_id,
            )

            self._notify_bg(
                account["name"],
                f"➕ ADDON (MID) abierto | Group: {gid} ({self.group_addon_count[gkey]}/{self.addon_max})\n"
                f"Level≈{addon_level:.5f} | Current≈{current:.5f}\n"
                f"Ticket: {addon_ticket} | BaseTicket: {int(pos.ticket)} | Vol: {add_vol:.2f}"
            )

    # ----------------------------
    # Trailing
    # ----------------------------
    def _maybe_trailing(self, account: dict, pos, point: float, is_buy: bool, current: float, t: ManagedTrade):
        now = time.time()
        if (now - t.last_trailing_ts) < self.trailing_cooldown_sec:
            return

        open_price = float(pos.price_open)
        profit_pips = ((current - open_price) / point) if is_buy else ((open_price - current) / point)

        activate = profit_pips >= self.trailing_activation_pips
        if self.trailing_activation_after_tp2 and (t.runner_enabled or (2 in t.tp_hit)):
            activate = True
        if not activate:
            return

        trail_dist = self.trailing_stop_pips * point
        new_sl = (current - trail_dist) if is_buy else (current + trail_dist)

        cur_sl = float(pos.sl)
        min_change = self.trailing_min_change_pips * point

        if cur_sl != 0.0 and abs(new_sl - cur_sl) < min_change:
            return
        if t.last_trailing_sl is not None and abs(new_sl - t.last_trailing_sl) < min_change:
            return

        improved = (cur_sl == 0.0) or (is_buy and new_sl > cur_sl + min_change) or ((not is_buy) and new_sl < cur_sl - min_change)
        if not improved:
            return

        req = {"action": mt5.TRADE_ACTION_SLTP, "position": int(pos.ticket), "sl": float(new_sl), "tp": 0.0}
        res = self.mt5.order_send(req)
        ok = bool(res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL))

        if ok:
            t.last_trailing_sl = float(new_sl)
            t.last_trailing_ts = now
            print(f"[TM] 🔄 trailing update ticket={pos.ticket} sl={new_sl:.5f}")
            self._notify_bg(account["name"], f"🔄 Trailing actualizado | Ticket: {int(pos.ticket)} | SL: {new_sl:.5f}")

    # ======================================================================
    # ✅ TOROFX MANAGEMENT (mensajes de seguimiento) — NO abre trades
    # ======================================================================
    def handle_torofx_management_message(self, source_chat_id: int, raw_text: str) -> bool:
        """
        Procesa mensajes tipo:
        - "Asegurando profits... quitando riesgo..." -> BE (una vez por trade)
        - "Cerrando el 50% ... +30" -> partial 50% when >=30 pips
        - "parcial ... +50/60" -> partial default % when >=torofx_partial_min_pips
        - "cerrando mi entrada de 4330 y dejando 4325" -> close ticket por entry
        Retorna True si consumió el mensaje (aunque no ejecutara nada aún).
        """
        text = (raw_text or "").strip()
        if not text:
            return False

        up = text.upper()

        # Detectores básicos
        has_close_word = any(w in up for w in ["CERRANDO", "CERRAR", "CIERRO", "CERRAD", "CERRAD0", "CERRARÉ"])
        has_partial_word = any(w in up for w in ["PARCIAL", "PARTIAL", "RECOGER", "COGER"])
        has_be_word = any(w in up for w in ["BREAKEVEN", "BREAK EVEN", "BREAK-EVEN", "QUITANDO EL RIESGO", "SIN RIESGO", "RISK OFF", "ASEGURANDO"])

        # Extrae porcentaje explícito: "50%" / "80 %"
        m_pct = re.search(r"(\d{1,3})\s*%", text)
        pct = int(m_pct.group(1)) if m_pct else None
        if pct is not None:
            pct = max(1, min(100, pct))

        # Extrae pips: "+30" o "+50/60"
        m_pips = re.search(r"\+(\d{1,4})(?:\s*/\s*(\d{1,4}))?", text)
        pips_threshold = None
        if m_pips:
            a = float(m_pips.group(1))
            b = float(m_pips.group(2)) if m_pips.group(2) else None
            # para "+50/60" tomamos el menor como umbral
            pips_threshold = min(a, b) if b is not None else a

        # Extrae precios tipo 4330 / 4325
        prices = [float(x) for x in re.findall(r"\b(4\d{3}(?:\.\d+)?)\b", text)]

        # Define acciones deseadas
        wants_close_entry = ("ENTRADA" in up) and has_close_word and len(prices) >= 1
        wants_be = has_be_word and not has_partial_word and not wants_close_entry
        wants_partial = (has_partial_word or (has_close_word and pct is not None)) and not wants_close_entry

        # Si no parece gestión, no consumir
        if not (wants_close_entry or wants_be or wants_partial):
            return False

        # Ejecutar por cada cuenta (solo trades ya registrados TOROFX)
        any_matched_trade = False
        for account in [a for a in self.mt5.accounts if a.get("active")]:
            if not self.mt5.connect_to_account(account):
                continue

            positions = self.mt5.positions_get()
            if not positions:
                continue
            pos_by_ticket = {p.ticket: p for p in positions}

            # ---- 1) Cerrar entrada específica (por price_open) ----
            if wants_close_entry:
                close_price = prices[0]
                keep_price = prices[1] if len(prices) >= 2 else None

                for ticket, t in list(self.trades.items()):
                    if t.account_name != account["name"]:
                        continue
                    if self.torofx_provider_tag_match not in (t.provider_tag or "").upper():
                        continue

                    pos = pos_by_ticket.get(ticket)
                    if not pos or pos.magic != self.mt5.magic:
                        continue

                    info = self.mt5.symbol_info(t.symbol)
                    if not info:
                        continue
                    point = float(info.point)
                    tol = self.torofx_close_entry_tolerance_pips * point

                    entry = float(pos.price_open)

                    # No cerrar el "keep" si viene
                    if keep_price is not None and abs(entry - keep_price) <= tol:
                        continue

                    if abs(entry - close_price) <= tol:
                        action_key = f"TOROFX_CLOSE_ENTRY_{int(close_price)}"
                        if action_key in t.actions_done:
                            continue

                        any_matched_trade = True
                        t.actions_done.add(action_key)
                        self._do_partial_close(account, ticket, 100, reason=f"TOROFX close entry {close_price}")
                        self._notify_bg(
                            account["name"],
                            f"🧹 TOROFX: cerrada entrada ≈{close_price}\nTicket: {ticket} | Entry: {entry:.2f}"
                        )
                continue  # si fue “close entry”, no hacemos otras acciones en el mismo mensaje

            # ---- 2) BE / risk off ----
            if wants_be:
                for ticket, t in list(self.trades.items()):
                    if t.account_name != account["name"]:
                        continue
                    if self.torofx_provider_tag_match not in (t.provider_tag or "").upper():
                        continue

                    pos = pos_by_ticket.get(ticket)
                    if not pos or pos.magic != self.mt5.magic:
                        continue

                    action_key = "TOROFX_BE"
                    if action_key in t.actions_done:
                        continue

                    any_matched_trade = True
                    t.actions_done.add(action_key)

                    # aplica BE directo (sin offset) usando executor
                    self.mt5.set_be(account=account, ticket=int(ticket))
                continue

            # ---- 3) Parcial por pips ----
            if wants_partial:
                pct_use = pct if pct is not None else int(self.torofx_partial_default_percent)
                pct_use = max(1, min(100, pct_use))

                # Si no trae pips en el mensaje, usamos el default torofx_partial_min_pips
                pips_need = float(pips_threshold) if pips_threshold is not None else float(self.torofx_partial_min_pips)

                for ticket, t in list(self.trades.items()):
                    if t.account_name != account["name"]:
                        continue
                    if self.torofx_provider_tag_match not in (t.provider_tag or "").upper():
                        continue

                    pos = pos_by_ticket.get(ticket)
                    if not pos or pos.magic != self.mt5.magic:
                        continue

                    info = self.mt5.symbol_info(t.symbol)
                    if not info:
                        continue
                    point = float(info.point)

                    is_buy = (t.direction == "BUY")
                    entry = float(pos.price_open)
                    current = float(pos.price_current)

                    profit_pips = ((current - entry) / point) if is_buy else ((entry - current) / point)

                    # gate por pips
                    if profit_pips < pips_need:
                        continue

                    action_key = f"TOROFX_PARTIAL_{pct_use}_AT_{int(pips_need)}"
                    if action_key in t.actions_done:
                        continue

                    any_matched_trade = True
                    t.actions_done.add(action_key)

                    # cierre parcial con fallback min-lot (lo resuelve executor)
                    self._do_partial_close(account, ticket, pct_use, reason=f"TOROFX partial {pct_use}% @ +{pips_need}")
                    self._notify_bg(
                        account["name"],
                        f"✂️ TOROFX parcial ejecutado\nTicket: {ticket} | {t.symbol} | {t.direction}\n"
                        f"Profit≈{profit_pips:.1f} pips | Cierre: {pct_use}%"
                    )

        # Consumimos el mensaje si era de gestión TOROFX (aunque no haya match en ese instante)
        return True

