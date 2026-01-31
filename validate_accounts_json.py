import os
import json

def validate_accounts_json():
    accounts_env = os.getenv('ACCOUNTS_JSON', '[]')
    try:
        accounts = json.loads(accounts_env)
    except Exception as e:
        print(f"[ERROR] ACCOUNTS_JSON inválido: {e}")
        return
    ok = True
    for acct in accounts:
        name = acct.get('name')
        chat_id = acct.get('chat_id')
        if not chat_id or not str(chat_id).lstrip('-').isdigit():
            print(f"[INVALID] Cuenta '{name}': chat_id inválido: '{chat_id}' (debe ser numérico)")
            ok = False
        else:
            print(f"[OK] Cuenta '{name}': chat_id={chat_id}")
    if ok:
        print("\nTodos los chat_id son válidos.")
    else:
        print("\nCorrige los chat_id inválidos en tu ACCOUNTS_JSON.")

if __name__ == "__main__":
    validate_accounts_json()
