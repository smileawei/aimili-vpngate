FROM debian:bookworm-slim

ARG BUILD_VERSION=dev
LABEL org.opencontainers.image.title="AimiliVPN SoftEther" \
      org.opencontainers.image.description="VPNGate proxy with SoftEther hot standby" \
      org.opencontainers.image.source="https://github.com/smileawei/aimili-vpngate" \
      org.opencontainers.image.version="${BUILD_VERSION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates tzdata curl iproute2 iptables procps psmisc python3 \
        softether-vpnclient isc-dhcp-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY VERSION README.md LICENSE ./
COPY vpngate_manager.py vpn_utils.py proxy_server.py snapshot_utils.py switch_history.py ./
COPY mirror ./mirror
COPY native/start.py native/dhcp.py /opt/native/
COPY native/backend.py /app/softether_backend.py
COPY native/hotstandby.py /app/hotstandby.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEPLOYMENT_MODE=docker \
    VPN_BACKEND=softether \
    VPNGATE_DATA_DIR=/data \
    UI_HOST=0.0.0.0 \
    UI_PORT=8787 \
    LOCAL_PROXY_HOST=0.0.0.0 \
    LOCAL_PROXY_PORT=7929

RUN mkdir -p /data /run/softether \
    && chmod 755 /opt/native/dhcp.py \
    && python3 -m py_compile *.py /opt/native/*.py
VOLUME ["/data"]
EXPOSE 8787/tcp 7929/tcp
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import os,socket; s=socket.create_connection(('127.0.0.1',int(os.environ.get('UI_PORT','8787'))),3); s.close()"
ENTRYPOINT ["python3", "-u", "/opt/native/start.py"]
CMD []
