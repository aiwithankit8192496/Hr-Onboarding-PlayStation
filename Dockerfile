# One image for both apps, built for arm64 (AWS Graviton: cheaper on Lambda). Two ways to run it:
#
#   AWS Lambda   the AWS Lambda Web Adapter (copied in below) forwards each invocation to the web
#                server on PORT. One image, two functions; each function sets its own command:
#                  hr-onboarding-portal:  python -m hr_onboarding.employee_portal   PORT=8000
#                  hr-onboarding-hr:      python -m hr_onboarding.hr_portal        PORT=8001
#   Anywhere else (a laptop, docker compose, a server): the extension is not used; the same
#                command starts the same uvicorn server.
#
# Build:  docker buildx build --platform linux/arm64 --provenance=false -t <ecr-uri>:<tag> --push .
# (--provenance=false: Lambda does not accept the extra manifest that Docker adds by default)
#
# Pinned versions. Python 3.12.16 is the version the tests run on. Each base image is named by its
# version AND its digest, so a rebuild cannot silently pick up a different image. To move to a newer one:
#   docker buildx imagetools inspect python:3.12-slim          (shows the new tag and its Digest)
#   docker buildx build --build-arg PYTHON_IMAGE=python:3.12.NN-slim@sha256:<digest> ...
# (the same for ADAPTER_IMAGE). The digests below are the multi-architecture index digests.
ARG PYTHON_IMAGE=python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG ADAPTER_IMAGE=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1@sha256:46d6625e68cbbdd2efab4a20245977664513f13ffef47915b000d431adcea0b4


FROM ${ADAPTER_IMAGE} AS adapter


# ---- Stage 1: compile the pages' JavaScript (once, on the machine that builds the image) -----------
# The pages are written in JSX; a strict Content-Security-Policy forbids compiling it in the browser,
# so Babel (in V8, via mini-racer) turns it into plain files here. The output is plain text, the same
# for every CPU, so this stage runs on the build machine's own architecture and is not in the final image.
FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS assets

WORKDIR /build
COPY requirements/runtime.txt requirements/build.txt requirements/
RUN pip install --no-cache-dir -r requirements/runtime.txt -r requirements/build.txt

COPY src src
COPY scripts/build_assets.py scripts/build_assets.py
COPY scripts/vendor scripts/vendor
RUN python scripts/build_assets.py


# ---- Stage 2: the image that runs ------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

# Apply OS security patches published since the base image was built
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*

# The adapter is a small binary; it only acts when running on Lambda
COPY --from=adapter /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app

COPY requirements/runtime.txt requirements/
RUN pip install --no-cache-dir -r requirements/runtime.txt

# The code is the hr_onboarding package under src/ (not installed, put on the import path)
ENV PYTHONPATH=/app/src
COPY src src
COPY --from=assets /build/src/hr_onboarding/static/dist src/hr_onboarding/static/dist

# Not root. The data folders exist so local mode (STORAGE_BACKEND=local) can write to them.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p submissions forms uploads hr_tables \
    && chown -R app:app /app
USER app

# On Lambda the only writable folder is /tmp, so anything that wants a home folder uses it
ENV HOST=0.0.0.0 \
    STORAGE_BACKEND=dynamodb_s3 \
    PYTHONUNBUFFERED=1 \
    AWS_LWA_READINESS_CHECK_PATH=/health

# Secrets (GEMINI_API_KEY, HR_USERS, SESSION_SECRET) come from SSM Parameter Store (SSM_PARAMETER_*) or
# the environment, never from the image: .env is not copied in.
EXPOSE 8000 8001

# For docker run / compose (Lambda ignores it). Checks whichever portal this container runs.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)"

CMD ["python", "-m", "hr_onboarding.employee_portal"]
