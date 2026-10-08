# The application, as Cloudflare Containers runs it (cloudflare/wrangler.jsonc).
# Also a perfectly ordinary image: `docker run -p 8080:8080 --env-file .env`.
FROM python:3.12-slim

# LibreOffice Writer converts the legacy .doc and .rtf files that still arrive
# attached to deals (src/secondeye/convert.py). It is most of this image's
# size, and refusing those files is the friction the product exists to remove.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer-nogui \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# The dependencies in a layer of their own, built from pyproject.toml with an
# empty package, so a deploy that changes only our code ships only our code.
# With the source copied first, every push rebuilt and re-shipped the whole
# dependency tree, and the first container to wake after a deploy pulled it.
COPY pyproject.toml README.md ./
RUN mkdir -p src/secondeye && touch src/secondeye/__init__.py \
 && pip install --no-cache-dir . \
 && pip uninstall -y second-eye \
 && rm -rf src
# Editable, and with agents/ and skills/ beside src/. managed.py, skillsync.py
# and playbook.py find those folders from their own file (parents[2]); from a
# normal install that is site-packages' parent, where neither exists, so
# every live review failed to load its rubric.
COPY src ./src
COPY agents ./agents
COPY skills ./skills
RUN pip install --no-cache-dir --no-deps -e .

# Nothing is written to this disk that matters. A container's disk is wiped
# whenever it sleeps, which is why rows live in D1 and documents in R2.
RUN useradd --create-home --uid 10001 secondeye
USER secondeye
ENV HOME=/home/secondeye PYTHONUNBUFFERED=1

EXPOSE 8080
# On Cloudflare the container has no internet of its own: every HTTPS request
# is opened by the Worker's egress allowlist (CONTAINER_EGRESS, index.ts) and
# re-signed under a CA Cloudflare mounts at start, for that instance only. So
# it cannot be baked in here. When it is there, it is added to certifi's roots
# for httpx (the Anthropic SDK and the document-system token exchange) and
# anything else that reads SSL_CERT_FILE. Elsewhere (docker run, CONTAINER_EGRESS=open)
# the file is absent and nothing changes.
CMD ["sh", "-c", "CA=/etc/cloudflare/certs/cloudflare-containers-ca.crt; if [ -r \"$CA\" ]; then cat \"$(python -m certifi)\" \"$CA\" > /tmp/ca-bundle.pem && export SSL_CERT_FILE=/tmp/ca-bundle.pem REQUESTS_CA_BUNDLE=/tmp/ca-bundle.pem; fi; exec uvicorn secondeye.main:app --host 0.0.0.0 --port 8080"]
