FROM aimili-vpngate:2.1.5-local
RUN apt-get update \
    && apt-get install -y --no-install-recommends softether-vpnclient isc-dhcp-client \
    && rm -rf /var/lib/apt/lists/*
COPY native/start.py native/dhcp.py /opt/native/
COPY native/backend.py /app/softether_backend.py
COPY vpngate_manager.py /app/vpngate_manager.py
RUN chmod 755 /opt/native/dhcp.py
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import socket; s=socket.create_connection(('127.0.0.1',8787),3); s.close()"
ENTRYPOINT ["python3", "-u", "/opt/native/start.py"]
CMD []
