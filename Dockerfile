# One image for both apps. The command picks which one runs:
#   onboarding portal: python react_onboard.py   (port 8000)
#   HR review portal:  python hr_info.py         (port 8001)
FROM python:3.12-slim

WORKDIR /app

COPY requirements-aws.txt .
RUN pip install --no-cache-dir -r requirements-aws.txt

COPY react_onboard.py hr_info.py Bank_details.py policies.py storage.py hr_tables.py sync_tables.py ./

# Not root. The data folders exist so local mode (STORAGE_BACKEND=local) can write to them.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p submissions forms uploads hr_tables \
    && chown -R app:app /app
USER app

ENV HOST=0.0.0.0 \
    STORAGE_BACKEND=mongo_s3 \
    PYTHONUNBUFFERED=1

# Secrets (GEMINI_API_KEY, MONGODB_URI, HR_USER, HR_PASSWORD) and S3_BUCKET come from the environment,
# never from the image: .env is not copied in.
EXPOSE 8000 8001

# Checks whichever portal this container runs, on its own PORT (8000 unless set)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)"

CMD ["python", "react_onboard.py"]
