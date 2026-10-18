FROM python:3-slim

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
RUN pip install --no-cache-dir -r requirements.txt

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