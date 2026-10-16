# Stage 1: Builder (to install dependencies)
FROM python:3-alpine AS builder

# Install system dependencies
# RUN apt-get update && apt-get install -y cron

WORKDIR /app

# Install build dependencies (for compiling Python packages with C extensions)
RUN apk add --no-cache gcc musl-dev libffi-dev

# Create virtual environment
RUN python3 -m venv venv
ENV VIRTUAL_ENV=/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Optionally remove build dependencies to reduce image size
RUN apk del gcc musl-dev libffi-dev

# Stage 2: Runner (final image to run the app)
FROM python:3-alpine AS runner

WORKDIR /app

# Copy the virtual environment from the builder stage
COPY --from=builder /app/venv venv

# Copy the entire project into the container
COPY . .

# Set the environment variables
ENV VIRTUAL_ENV=/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV PORT=9090

# Expose the port for the web app
EXPOSE ${PORT}
ENV DJANGO_SETTINGS_MODULE=forex.settings

# Command to run the Django app with Gunicorn
CMD gunicorn --bind :${PORT} --workers $(($(nproc) * 2 + 1)) forex.wsgi:application
