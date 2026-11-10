FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir uv && uv pip install --system --no-cache -r requirements.txt

COPY . .

# Run as a non-root user (backend.md §9). Secrets come from the environment, never the image.
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
