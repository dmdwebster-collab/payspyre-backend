FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies only (build dependencies removed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir --user -e . && \
    rm -rf /root/.cache/pip

# Copy application code
COPY . .

# Create non-root user for security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default command (use gunicorn in production via override)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]