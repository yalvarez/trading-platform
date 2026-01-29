import re
from collections import defaultdict

# Configuración de la modalidad propuesta
def simulate_modalidad(log_path):
    with open(log_path, encoding='utf-8') as f:
        lines = f.readlines()

    trades = {}
    resultados = []
    ticket_to_entry = {}
    ticket_to_sl = {}
    ticket_to_tp1 = {}
    ticket_to_tp2 = {}
    ticket_to_lot = {}
    ticket_to_dir = {}

    # Parseo simple de logs
    for line in lines:
        # Apertura
        m = re.search(r"order_send OK -> Cuenta .* ticket=(\d+) price=([\d.]+) lot=([\d.]+)", line)
        if m:
            ticket, entry, lot = int(m.group(1)), float(m.group(2)), float(m.group(3))
            ticket_to_entry[ticket] = entry
            ticket_to_lot[ticket] = lot
            continue
        # SL
        m = re.search(r"SL actualizado .* Ticket: (\d+).*SL: ([\d.]+)", line)
        if m:
            ticket, sl = int(m.group(1)), float(m.group(2))
            ticket_to_sl[ticket] = sl
            continue
        # TP
        m = re.search(r"TPs: \[([\d.]+), ([\d.]+)\]", line)
        if m:
            tp1, tp2 = float(m.group(1)), float(m.group(2))
            # Buscar ticket en línea
            tkt = re.search(r"Ticket: (\d+)", line)
            if tkt:
                ticket = int(tkt.group(1))
                ticket_to_tp1[ticket] = tp1
                ticket_to_tp2[ticket] = tp2
            continue
        # Dirección
        m = re.search(r"(BUY|SELL) XAUUSD", line)
        if m:
            dir = m.group(1)
            tkt = re.search(r"Ticket: (\d+)", line)
            if tkt:
                ticket = int(tkt.group(1))
                ticket_to_dir[ticket] = dir
            continue


    # Simulación de modalidad con debug
    ignorados = []
    for ticket in ticket_to_entry:
        entry = ticket_to_entry.get(ticket)
        sl = ticket_to_sl.get(ticket)
        tp1 = ticket_to_tp1.get(ticket)
        tp2 = ticket_to_tp2.get(ticket)
        lot = ticket_to_lot.get(ticket, 0.02)
        dir = ticket_to_dir.get(ticket, 'BUY')
        if not (entry and sl and tp1 and tp2):
            ignorados.append({
                'ticket': ticket,
                'entry': entry,
                'sl': sl,
                'tp1': tp1,
                'tp2': tp2,
                'motivo': 'Faltan datos (entry/sl/tp1/tp2)'
            })
            continue
        cierre_tp1 = any(f"Cierre PARCIAL | Ticket: {ticket}" in l and "50%" in l for l in lines)
        cierre_total = any(f"Cierre TOTAL | Ticket: {ticket}" in l for l in lines)
        if cierre_tp1:
            if dir == 'BUY':
                ganancia_tp1 = lot * (tp1 - entry)
                runner = 0.01 * (tp2 - tp1)
            else:
                ganancia_tp1 = lot * (entry - tp1)
                runner = 0.01 * (tp1 - tp2)
            resultados.append({
                'ticket': ticket,
                'entry': entry,
                'sl': sl,
                'tp1': tp1,
                'tp2': tp2,
                'dir': dir,
                'ganancia_tp1': ganancia_tp1,
                'runner': runner,
                'resultado': ganancia_tp1 + runner,
                'tipo': 'TP1+runner'
            })
        else:
            if dir == 'BUY':
                perdida = lot * (entry - sl)
            else:
                perdida = lot * (sl - entry)
            resultados.append({
                'ticket': ticket,
                'entry': entry,
                'sl': sl,
                'tp1': tp1,
                'tp2': tp2,
                'dir': dir,
                'ganancia_tp1': 0,
                'runner': 0,
                'resultado': -perdida,
                'tipo': 'SL'
            })
    # Resumen
    total = sum(r['resultado'] for r in resultados)
    print(f"Total simulado modalidad propuesta: {total:.2f} (en lotes XAUUSD)")
    print("\n--- Detalle de resultados simulados ---")
    for r in resultados:
        print(r)
    print("\n--- Tickets ignorados (faltan datos) ---")
    for i in ignorados:
        print(i)

if __name__ == "__main__":
    simulate_modalidad("log.txt")
