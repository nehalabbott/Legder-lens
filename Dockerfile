# Use lightweight official Python image
FROM python:3.11-slim

# Set working directory inside container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the pinned requirements file (single
# source of truth, shared with local/CI installs).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files into the container
COPY . /app/

# Expose ports for FastAPI webhook receiver and Streamlit dashboard
EXPOSE 8000
EXPOSE 8501

# Default command runs the automated pipeline or can be overridden
CMD ["python", "main.py"]