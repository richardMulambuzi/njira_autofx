FROM python:3.12-slim

WORKDIR /app

# Install runtime dependencies only (no compiler needed on glibc with manylinux wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python3 -m venv venv
ENV VIRTUAL_ENV=/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Copy requirements and install dependencies (wheels available for all packages on glibc)
COPY requirements.txt .
# Work around two pre-existing dependency conflicts in requirements.txt:
# 1. ctrader-open-api 0.9.2 pins Twisted==24.3.0, but Twisted==26.4.0 is required
# 2. pyOpenSSL==26.2.0 requires cryptography<49, but platform marker selects cryptography>=49.0.0 on Linux
# Strategy: install cryptography first (gets >=49.0.0), then pyOpenSSL & ctrader-open-api with --no-deps,
# then the remaining requirements.
RUN pip install --no-cache-dir "cryptography>=49.0.0" && \
    pip install --no-cache-dir --no-deps pyOpenSSL==26.2.0 && \
    pip install --no-cache-dir --no-deps ctrader-open-api==0.9.2 && \
    grep -v -E '^(ctrader-open-api|pyOpenSSL==|cryptography)' requirements.txt > /tmp/reqs-clean.txt && \
    pip install --no-cache-dir -r /tmp/reqs-clean.txt

# Copy the entire project into the container
COPY . .

# Set the environment variables
ENV VIRTUAL_ENV=/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV PORT=9090
ENV DJANGO_SETTINGS_MODULE=forex.settings

# Expose the port for the web app
EXPOSE ${PORT}

# Command to run the Django app with Gunicorn
CMD gunicorn --bind :${PORT} --workers $(($(nproc) * 2 + 1)) forex.wsgi:application