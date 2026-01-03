FROM gmag11/metatrader5_vnc:latest

USER root

# Remove broken WineHQ repo from base image
RUN rm -f /etc/apt/sources.list.d/winehq.list || true
RUN sed -i '/wine-builds/d' /etc/apt/sources.list || true




# Remove WineHQ repo from base image, then install tools and Wine from default Debian repositories
RUN rm -f /etc/apt/sources.list.d/winehq.list || true && \
	sed -i '/wine-builds/d' /etc/apt/sources.list || true && \
	apt-get update && \
	apt-get install -y wget gnupg2 ca-certificates software-properties-common \
	wine winbind xvfb xauth x11-utils

# Install Python + supervisor
RUN apt-get install -y python3 python3-pip supervisor

# Install MT5 Python API + RPyC
RUN pip3 install MetaTrader5 rpyc

# Copy RPyC server
COPY services/mt5_extended/mt5_rpyc_server.py /opt/mt5_rpyc_server.py
RUN chmod +x /opt/mt5_rpyc_server.py

# Copy supervisord config
COPY services/mt5_extended/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

COPY services/mt5_extended/server_rpyc.py /opt/server_rpyc.py
COPY services/mt5_extended/99-rpyc.sh /etc/cont-init.d/99-rpyc.sh
RUN chmod +x /etc/cont-init.d/99-rpyc.sh

EXPOSE 18812

CMD ["/usr/bin/supervisord"]