# Observer for any host that runs Docker natively (NAS, home server, another cloud).
# The container uses the host's clock, so the HOST must run NTP (chrony / systemd-timesyncd).
# Not for Docker Desktop on Windows: it runs on WSL2, whose clock drifts.
FROM python:3.12-slim
# checkov:skip=CKV_DOCKER_2:No HTTP endpoint to probe; end-to-end liveness is the observer-missing CloudWatch alarm

ARG WITH_AWS=0
RUN useradd --system --uid 10001 --create-home --home-dir /app tvtg
WORKDIR /app
COPY requirements.txt requirements-aws.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_AWS" = "1" ]; then pip install --no-cache-dir -r requirements-aws.txt; fi
COPY observer.py ./

USER tvtg
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "observer.py"]
CMD ["run"]
