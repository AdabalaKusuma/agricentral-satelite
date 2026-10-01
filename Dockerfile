# python:3.12-slim is enough on purpose: the only compiled dependency is numpy, which
# ships manylinux wheels, so nothing is built here. Decoding rasters with numpy directly
# instead of reaching for rasterio/GDAL is what keeps this image around 250 MB rather
# than 1.5 GB - keep it that way.
#
# Two stages. Nothing here needs a compiler, so the split is not about build tools: it is
# about what the runtime stage inherits. Dependencies are resolved into a venv in
# `builder`, and `runtime` copies that one directory - so whatever the install step leaves
# scattered around the builder (pip's own tree inside the venv, wheel metadata, anything
# pip wrote outside site-packages) never reaches a runtime layer. The runtime image ends
# up holding the base interpreter, the five libraries the worker imports, and the worker.

# One ARG, used by both FROMs, so the two stages can never drift onto different bases.
# That matters: the venv in /opt/venv symlinks the interpreter it was created from, and
# those symlinks have to resolve in the runtime stage.
ARG PYTHON_IMAGE=python:3.12-slim

# ---------------------------------------------------------------------------- builder --
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# A venv rather than the system site-packages: it gives the next stage a single directory
# to copy, with no way to miss a file installed somewhere else in /usr/local.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Only requirements.txt, so the dependency layer is rebuilt when dependencies change and
# not when app code does. This is the slow layer - everything below it is a file copy.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# The worker never shells out to pip, so the venv's own copies of pip, setuptools and
# wheel are dead weight - roughly 8 MB of the image. Dropping them HERE rather than after
# the COPY is the point: a `RUN rm` in the runtime stage would delete them from the final
# filesystem but leave them in the layer underneath, so the image would not shrink.
# This prunes the venv only; the base image's own /usr/local/bin/pip is untouched. Note
# that bare `pip` in a shell therefore resolves to the SYSTEM pip, which installs into
# /usr/local site-packages - somewhere the venv's python cannot see. So `docker exec ...
# pip install x` reports success and the worker still cannot import x. To add a package
# for a debugging session, run `python -m ensurepip` first, then `python -m pip install`.
# For anything permanent, put it in requirements.txt and rebuild.
RUN pip uninstall --yes pip setuptools wheel 2>/dev/null || true \
 && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /opt/venv -name '*.pyc' -delete

# ---------------------------------------------------------------------------- runtime --
FROM ${PYTHON_IMAGE} AS runtime

# PATH puts the venv first, which is what makes the bare `python` in CMD the venv's
# interpreter and gives it the installed packages.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /srv

COPY --from=builder /opt/venv /opt/venv

COPY app ./app
COPY run.py ./

# Run as a non-root user: this process needs no filesystem writes at all. The copies above
# stay root-owned and are only read by `worker`, so the worker cannot rewrite its own code.
RUN useradd --create-home --shell /usr/sbin/nologin worker
USER worker

# No EXPOSE and no HEALTHCHECK, both deliberate. This is a worker, not a server: nothing
# connects to it, so there is no port, and there is no endpoint a health probe could call.
# A probe that cannot be answered while numpy is busy would restart the container during
# legitimate work. Monitor satellite.farm_status.last_run_at instead - if it stops moving,
# the worker is stuck. Pair with `restart: unless-stopped`.
CMD ["python", "run.py"]
