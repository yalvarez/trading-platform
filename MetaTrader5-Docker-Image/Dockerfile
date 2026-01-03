FROM gmag11/metatrader5_vnc:latest

COPY server_rpyc.py /opt/server_rpyc.py
COPY 99-rpyc.sh /etc/cont-init.d/99-rpyc.sh
RUN chmod +x /etc/cont-init.d/99-rpyc.sh
