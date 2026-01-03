FROM gmag11/metatrader5_vnc:latest

USER root

# Remove broken WineHQ repo from base image
RUN rm -f /etc/apt/sources.list.d/winehq.list || true
RUN sed -i '/wine-builds/d' /etc/apt/sources.list || true

# Install tools
RUN apt-get update && apt-get install -y wget gnupg2

# Add WineHQ key and repo (correct way for Debian Bookworm)
RUN mkdir -p /etc/apt/keyrings && \
    wget -O /etc/apt/keyrings/winehq.gpg https://dl.winehq.org/wine-builds/winehq.key && \
    echo "deb [signed-by=/etc/apt/keyrings/winehq.gpg] https://dl.winehq.org/wine-builds/debian bookworm main" \
    > /etc/apt/sources.list.d/winehq.list

RUN apt-get update

# Install Python + supervisor
RUN apt-get install -y python3 python3-pip supervisor

# Install MT5 Python API + RPyC
RUN pip3 install MetaTrader5 rpyc

# Copy RPyC server
COPY mt5_rpyc_server.py /opt/mt5_rpyc_server.py
RUN chmod +x /opt/mt5_rpyc_server.py

# Copy supervisord config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 18812

CMD ["/usr/bin/supervisord"]