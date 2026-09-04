"""
trade_state_store.py
Persiste el estado de gestion de TradeManager (memoria que no existe en
MT5: tp1_price, tp2_price, be_applied, peak_multiple) para que un reinicio
del contenedor pueda reconstruirlo, en vez de dejar grupos huerfanos.

Dos capas de persistencia:
- Redis (primaria, rapida, ya en el stack del proyecto).
- Archivo JSON Lines (respaldo, bind-mounted fuera del contenedor —
  sobrevive aunque el volumen de Redis se pierda por completo).

Ver docs/superpowers/specs/2026-09-04-trade-state-persistence-design.md.
"""
import asyncio
import json
import logging
import os
from typing import Optional

log = logging.getLogger("trade_orchestrator.trade_state_store")

REDIS_KEY_PREFIX = "trade_groups:"


class TradeStateStore:
    def __init__(self, redis_client, file_path: str):
        self.redis = redis_client
        self.file_path = file_path

    def _redis_key(self, group_id: int) -> str:
        return f"{REDIS_KEY_PREFIX}{group_id}"

    async def save_group(self, doc: dict) -> None:
        """
        Persiste el documento completo de un grupo (Redis + archivo). Nunca
        lanza: un fallo aqui no debe interrumpir la operacion de trading en
        curso — se loguea como warning y se continua.
        """
        group_id = doc["group_id"]
        line = json.dumps(doc)
        try:
            await self.redis.set(self._redis_key(group_id), line)
        except Exception as e:
            log.warning("[STORE] fallo escribiendo group_id=%s en Redis: %s", group_id, e)
        try:
            await asyncio.to_thread(self._append_line, line)
        except Exception as e:
            log.warning("[STORE] fallo escribiendo group_id=%s en archivo: %s", group_id, e)

    async def close_group(self, group_id: int) -> None:
        """Marca un grupo como cerrado: borra de Redis, agrega marcador de cierre al archivo."""
        try:
            await self.redis.delete(self._redis_key(group_id))
        except Exception as e:
            log.warning("[STORE] fallo borrando group_id=%s de Redis: %s", group_id, e)
        try:
            line = json.dumps({"group_id": group_id, "closed": True})
            await asyncio.to_thread(self._append_line, line)
        except Exception as e:
            log.warning("[STORE] fallo escribiendo cierre de group_id=%s en archivo: %s", group_id, e)

    async def load_group(self, group_id: int) -> tuple[Optional[dict], str]:
        """
        Redis primero; si no esta (o Redis falla), cae al archivo (ultima
        entrada no-cierre). Retorna (doc, "redis"), (doc, "file"), o
        (None, "none"). El string de fuente es lo que
        TradeManager.reconcile_from_mt5 usa para reportar
        recovered_from_redis vs recovered_from_file por separado.
        """
        try:
            raw = await self.redis.get(self._redis_key(group_id))
            if raw:
                return json.loads(raw), "redis"
        except Exception as e:
            log.warning("[STORE] fallo leyendo group_id=%s de Redis, probando archivo: %s", group_id, e)

        try:
            doc = await asyncio.to_thread(self._load_from_file, group_id)
            return (doc, "file") if doc is not None else (None, "none")
        except Exception as e:
            log.warning("[STORE] fallo leyendo group_id=%s de archivo: %s", group_id, e)
            return None, "none"

    async def load_all_group_ids(self) -> set[int]:
        """
        Todo group_id con CUALQUIER entrada (abierta o de cierre) en el
        archivo, unido con todo group_id presente como key en Redis. Usado
        para reconciliar _next_group_id — un grupo cerrado sigue contando,
        porque MT5 podria todavia tener un comment viejo que lo referencia.
        """
        ids: set[int] = set()
        try:
            keys = await self.redis.keys(f"{REDIS_KEY_PREFIX}*")
            for k in keys:
                try:
                    ids.add(int(k[len(REDIS_KEY_PREFIX):]))
                except ValueError:
                    continue
        except Exception as e:
            log.warning("[STORE] fallo listando keys de Redis: %s", e)

        try:
            file_ids = await asyncio.to_thread(self._all_group_ids_from_file)
            ids |= file_ids
        except Exception as e:
            log.warning("[STORE] fallo leyendo group_ids del archivo: %s", e)

        return ids

    async def compact(self, active_group_ids: set[int]) -> None:
        """
        Reescribe el archivo de forma atomica (tmp + rename), conservando
        solo la ultima entrada de cada group_id en active_group_ids. Grupos
        cerrados/inactivos se descartan por completo.
        """
        try:
            await asyncio.to_thread(self._compact_file, active_group_ids)
        except Exception as e:
            log.warning("[STORE] fallo compactando archivo: %s", e)

    # ---- Helpers sincronos (corren en threads via asyncio.to_thread) ----

    def _append_line(self, line: str) -> None:
        with open(self.file_path, "a") as f:
            f.write(line + "\n")

    def _read_last_entries(self) -> dict[int, dict]:
        """Recorre el archivo y retorna {group_id: ultima_entrada} (incluye cierres)."""
        if not os.path.exists(self.file_path):
            return {}
        last: dict[int, dict] = {}
        with open(self.file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                gid = doc.get("group_id")
                if gid is None:
                    continue
                last[gid] = doc
        return last

    def _load_from_file(self, group_id: int) -> Optional[dict]:
        last = self._read_last_entries()
        doc = last.get(group_id)
        if doc is None or doc.get("closed"):
            return None
        return doc

    def _all_group_ids_from_file(self) -> set[int]:
        return set(self._read_last_entries().keys())

    def _compact_file(self, active_group_ids: set[int]) -> None:
        last = self._read_last_entries()
        tmp_path = self.file_path + ".tmp"
        with open(tmp_path, "w") as f:
            for gid in active_group_ids:
                doc = last.get(gid)
                if doc and not doc.get("closed"):
                    f.write(json.dumps(doc) + "\n")
        os.replace(tmp_path, self.file_path)
