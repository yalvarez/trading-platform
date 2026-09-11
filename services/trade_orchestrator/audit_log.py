"""
audit_log.py
Escritura del log de auditoria local (JSONL append-only, nunca
compactado) que sirve como fuente de verdad primaria de todo evento de
negocio -- independiente de que Redis o n8n esten disponibles.
Ver docs/superpowers/specs/2026-09-10-audit-log-and-telegram-notifications-design.md
"""
import json
import os


def append_event(path: str, envelope: dict) -> None:
    """Agrega `envelope` como una linea JSON al final de `path`. Crea el
    archivo y cualquier directorio padre faltante si no existen."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(envelope, ensure_ascii=False) + "\n")


def mark_dead_letter(path: str, event_id: str) -> bool:
    """Reescribe la linea cuyo event_id coincide, agregando
    delivery_status='dead_letter'. Retorna True si la encontro.
    Usa escritura atómica (archivo temporal + os.replace) para evitar
    corrupción si el proceso se cae durante la reescritura."""
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    found = False
    new_lines = []
    for line in lines:
        record = json.loads(line)
        if record.get("event_id") == event_id:
            record["delivery_status"] = "dead_letter"
            found = True
        new_lines.append(json.dumps(record, ensure_ascii=False) + "\n")
    if found:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        os.replace(tmp_path, path)
    return found
