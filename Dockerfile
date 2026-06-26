FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY . .

# Seed the synthetic dataset at build time so the container is demo-ready
RUN python data/generate_dataset.py

EXPOSE 8000

# Single command brings up API + dashboard at http://localhost:8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
