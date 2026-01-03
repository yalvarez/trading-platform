#!/bin/sh
# Script de arranque secundario para MT5 + rpyc
LOGFILE=/config/entrypoint.log
echo "[entrypoint] Script iniciado $(date)" > $LOGFILE 2>&1
env >> $LOGFILE 2>&1
echo "[entrypoint] Esperando entorno gráfico..." >> $LOGFILE 2>&1
sleep 10
echo "[entrypoint] Lanzando terminal MT5..." >> $LOGFILE 2>&1
wine 'C:\Program Files\MetaTrader 5\terminal64.exe' >> $LOGFILE 2>&1 &
echo "[entrypoint] Esperando que MT5 arranque..." >> $LOGFILE 2>&1
sleep 30
echo "[entrypoint] Lanzando servidor rpyc..." >> $LOGFILE 2>&1
cd /opt
wine python.exe server_rpyc.py >> $LOGFILE 2>&1
cd -
echo "[entrypoint] Script finalizado $(date)" >> $LOGFILE 2>&1
