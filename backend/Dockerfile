# Throwaway stage, never part of the final image: resolves the exact commit
# this build was made from. .git's contents change deterministically on
# every single commit (a new commit object, an updated ref), so Docker's
# layer cache is guaranteed to invalidate this COPY on every commit — unlike
# a build-time `date +%s`, which relied on correctly inheriting cache
# invalidation from layers earlier in the (single) final image instead of
# being tied to something that's unambiguously different every time.
FROM python:3.11-slim AS version
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY .git ./.git
RUN git rev-parse --short HEAD > /VERSION

FROM python:3.11-slim

WORKDIR /srv

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY frontend ../frontend

# The running app's version is the actual git commit it was built from —
# used to detect "a newer version is deployed than what the browser/phone
# currently has loaded". Only the single resulting line crosses over from
# the throwaway stage above; .git's history itself never touches this image.
COPY --from=version /VERSION ./VERSION

ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
