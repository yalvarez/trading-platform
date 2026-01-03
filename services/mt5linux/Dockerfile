# Clean MT5Linux Dockerfile for MetaTrader 5 automation
# Based on Ubuntu 22.04 LTS (Jammy)


FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive


# Enable i386 architecture and install Wine (both 64 and 32 bit)
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wine wine32 wine64 winbind xvfb xauth x11-utils wget curl ca-certificates gnupg2 \
        python3 python3-pip supervisor unzip haveged && \
    rm -rf /var/lib/apt/lists/*




# Set up 64-bit Wine prefix and install full Windows Python 3.9 (silent mode)
ENV WINEARCH=win64
ENV WINEPREFIX=/root/.wine64
RUN rm -rf /root/.wine64 && wine64 wineboot --init
RUN wget -O /tmp/python-installer.exe https://www.python.org/ftp/python/3.9.13/python-3.9.13-amd64.exe \
    && wine64 /tmp/python-installer.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 TargetDir=C:\\Python39 \
    && rm /tmp/python-installer.exe


# Install MetaTrader5 and mt5linux in Windows Python
RUN wine64 C:\\Python39\\python.exe -m pip install --upgrade pip && \
    wine64 C:\\Python39\\python.exe -m pip install MetaTrader5 mt5linux

# Install MetaTrader5 and mt5linux in Linux Python (for client usage)
RUN pip3 install MetaTrader5 mt5linux

# Copy RPyC server launcher script (optional, if you want to customize startup)
# COPY services/mt5_extended/mt5_rpyc_server.py /opt/mt5_rpyc_server.py
# RUN chmod +x /opt/mt5_rpyc_server.py

# Copy supervisord config (optional, if you want to use supervisor)
# COPY services/mt5_extended/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 18812

# Default command: start the mt5linux rpyc server
CMD ["wine64", "C:\\Python39\\python.exe", "-m", "mt5linux"]
