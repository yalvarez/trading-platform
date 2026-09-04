import json
import re

# Copia el valor de ACCOUNTS_JSON aquí para validarlo.
# Single-account operation for now: ACCOUNTS_JSON sigue siendo una lista (soporta
# multi-cuenta a futuro), pero solo se configura/activa una entrada. No hay
# allowed_channels (un solo canal TradePulse) ni trading_mode (dual-TP es el
# unico comportamiento) en el modelo actual.
ACCOUNTS_JSON = '[{"name":"Main Account","host":"mt5_acct1","port":8001,"active":true,"fixed_lot":0.01,"chat_id":1234567890}]'

def validate_accounts_json(accounts_json):
    try:
        accounts = json.loads(accounts_json)
    except Exception as e:
        print(f"[ERROR] ACCOUNTS_JSON inválido: {e}")
        return
    ok = True
    for acct in accounts:
        name = acct.get('name')
        chat_id = acct.get('chat_id')
        if chat_id is None or not re.match(r'^-?\d+$', str(chat_id)):
            print(f"[INVALID] Cuenta '{name}': chat_id inválido: '{chat_id}' (debe ser numérico)")
            ok = False
        else:
            print(f"[OK] Cuenta '{name}': chat_id={chat_id}")
    if ok:
        print("\nTodos los chat_id son válidos.")
    else:
        print("\nCorrige los chat_id inválidos en tu ACCOUNTS_JSON.")

if __name__ == "__main__":
    validate_accounts_json(ACCOUNTS_JSON)
