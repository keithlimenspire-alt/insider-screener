FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY dashboard.py .

EXPOSE 8501

# Default command runs the dashboard; the compose file overrides this for the
# daily-ingest sidecar.
CMD ["streamlit", "run", "dashboard.py", "--server.address=0.0.0.0", "--server.headless=true"]
