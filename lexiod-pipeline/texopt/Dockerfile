ARG LEXOID_IMAGE=pdftotex-runtime:local
FROM ${LEXOID_IMAGE}

ARG DEBIAN_MIRROR=""
ARG DEBIAN_SECURITY_MIRROR=""

# The Lexoid image has `ENTRYPOINT ["lexoid"]`. This combined image is driven by
# texopt-pipeline instead, while the `lexoid` executable remains available on PATH.
ENTRYPOINT []

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Optional regional APT mirror. Debian 12+ container images use the DEB822
# /etc/apt/sources.list.d/debian.sources format. Empty args keep official mirrors.
RUN set -eux; \
    sources=/etc/apt/sources.list.d/debian.sources; \
    if [ -n "$DEBIAN_MIRROR" ]; then \
      sed -i "s#http://deb.debian.org/debian#$DEBIAN_MIRROR#g; s#https://deb.debian.org/debian#$DEBIAN_MIRROR#g" "$sources"; \
    fi; \
    if [ -n "$DEBIAN_SECURITY_MIRROR" ]; then \
      sed -i "s#http://deb.debian.org/debian-security#$DEBIAN_SECURITY_MIRROR#g; s#https://deb.debian.org/debian-security#$DEBIAN_SECURITY_MIRROR#g; s#http://security.debian.org/debian-security#$DEBIAN_SECURITY_MIRROR#g; s#https://security.debian.org/debian-security#$DEBIAN_SECURITY_MIRROR#g" "$sources"; \
    fi; \
    cat "$sources"

RUN set -eux; \
    success=0; \
    for attempt in 1 2 3; do \
      apt-get -o Acquire::Retries=5 \
              -o Acquire::http::Timeout=120 \
              -o Acquire::https::Timeout=120 update && \
      DEBIAN_FRONTEND=noninteractive \
      apt-get -o Acquire::Retries=5 \
              -o Acquire::http::Timeout=120 \
              -o Acquire::https::Timeout=120 \
              install -y --no-install-recommends \
                texlive-xetex \
                texlive-latex-extra \
                texlive-lang-chinese \
                poppler-utils && success=1 && break; \
      rm -rf /var/lib/apt/lists/*; \
      sleep 5; \
    done; \
    test "$success" = 1; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

WORKDIR /app
COPY . /app
RUN python -m pip install /app
# Lexoid currently has no CLI option for custom LaTeX instructions. Patch its bundled
# page prompt at image-build time so checkbox semantics are generated at the source.
RUN python /app/patch_lexoid_checkbox_prompt.py

VOLUME ["/data"]
CMD ["texopt-pipeline", "--once"]
