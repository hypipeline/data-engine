FROM python:3.12-slim

# whois binary is used by scrapers/whois_lookup.py; the browser scrapers connect to a
# REMOTE browser over CDP (Browserbase), so no local Chromium is installed here.
RUN apt-get update && apt-get install -y --no-install-recommends whois \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# Run the FastAPI app (bypasses server.py's __main__ so the port is ours to set)
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
