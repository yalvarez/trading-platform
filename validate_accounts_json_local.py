import json
import re

# Copia el valor de ACCOUNTS_JSON aquí para validarlo
ACCOUNTS_JSON = '[{"name":"Ysaias Vantage","host":"mt5_acct1","port":8001,"active":false,"fixed_lot":0.03,"chat_id":8592452414,"allowed_channels":[-5250557024,-1003209803455],"trading_mode":"general"},{"name":"Ysaias TickMill","host":"mt5_acct2","port":8001,"active":false,"fixed_lot":0.02,"chat_id":8592452414,"allowed_channels":[-5250557024,-1003209803455],"trading_mode":"general"},{"name":"Jesenia","host":"mt5_acct3","port":8001,"active":false,"fixed_lot":0.03,"chat_id":8592452414,"allowed_channels":[-5250557024,-1003209803455],"trading_mode":"general"},{"name":"Demo Leclerc","host":"mt5_acct4","port":8001,"active":true,"fixed_lot":0.03,"chat_id":8439707982,"allowed_channels":[-5250557024,-1003209803455,-1002293184715],"trading_mode":"reentry"},{"name":"Demo StarTrader","host":"mt5_acct5","port":8001,"active":true,"fixed_lot":0.03,"chat_id":8592452414,"allowed_channels":[-5250557024,-1003209803455,-1002293184715],"trading_mode":"general"},{"name":"Vantage Demo","host":"mt5_acct6","port":8001,"active":true,"fixed_lot":0.03,"chat_id":8592452414,"allowed_channels":[-5250557024,-1003209803455,-1002293184715],"trading_mode":"be_pips"},{"name":"Demo Jesenia","host":"mt5_acct7","port":8001,"active":true,"fixed_lot":0.03,"chat_id":8439707982,"allowed_channels":[-5250557024,-1003209803455,-1002293184715],"trading_mode":"be_pnl"},{"name":"Ysaias Demo 2","host":"mt5_acct8","port":8001,"active":true,"fixed_lot":0.03,"chat_id":8592452414,"allowed_channels":[-5250557024,-1003209803455],"trading_mode":"reentry"}]'

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
