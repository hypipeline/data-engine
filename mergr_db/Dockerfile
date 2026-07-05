FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY templates ./templates
COPY static ./static

# Default command runs the unified web UI; the api/loader/gap services override it.
EXPOSE 8501
CMD ["uvicorn", "webui:app", "--host", "0.0.0.0", "--port", "8501"]
