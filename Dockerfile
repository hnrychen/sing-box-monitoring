FROM python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
WORKDIR /app
COPY exporter.py /app/exporter.py
COPY tcpdiag.py /app/tcpdiag.py
COPY healthcheck.py /app/healthcheck.py
USER 65534:65534
EXPOSE 9119
ENTRYPOINT ["python3", "/app/exporter.py", "--config", "/config/config.json"]
