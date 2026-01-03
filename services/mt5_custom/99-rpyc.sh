#!/bin/sh
echo "[rpyc] Lanzando servidor rpyc personalizado..." >> /config/rpyc.log 2>&1
cd /opt
wine python.exe server_rpyc.py >> /config/rpyc.log 2>&1 &
