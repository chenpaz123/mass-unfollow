FROM python:3.11-slim

WORKDIR /srv

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY frontend ../frontend

# Cache-busted only by the two COPY layers above, so this changes exactly
# when the actual app code changes — used to detect "a newer version is
# deployed than what the browser/phone currently has loaded".
RUN date +%s > VERSION

ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
