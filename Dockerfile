FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /opt/tinyhat-hermes-runtime

COPY . /tmp/tinyhat-hermes-runtime-src
RUN bash /tmp/tinyhat-hermes-runtime-src/install.sh \
    --source-dir /tmp/tinyhat-hermes-runtime-src \
    --prefix /opt/tinyhat-hermes-runtime \
    --state-dir /var/lib/tinyhat-hermes-runtime \
    --ref local-dev \
    --no-systemd \
    && rm -rf /tmp/tinyhat-hermes-runtime-src

CMD ["/opt/tinyhat-hermes-runtime/bin/tinyhat-hermes-runtime"]
