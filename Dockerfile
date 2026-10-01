# python:3.12-slim is enough on purpose: the only compiled dependency is numpy, which
# ships manylinux wheels, so nothing is built here. Decoding rasters with numpy directly
# instead of reaching for rasterio/GDAL is what keeps this image around 250 MB rather
# than 1.5 GB - keep it that way.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py ./

# Run as a non-root user: this process needs no filesystem writes at all.
RUN useradd --create-home --shell /usr/sbin/nologin worker
USER worker

# No EXPOSE and no HEALTHCHECK, both deliberate. This is a worker, not a server: nothing
# connects to it, so there is no port, and there is no endpoint a health probe could call.
# A probe that cannot be answered while numpy is busy would restart the container during
# legitimate work. Monitor satellite.farm_status.last_run_at instead - if it stops moving,
# the worker is stuck. Pair with `restart: unless-stopped`.
CMD ["python", "run.py"]
