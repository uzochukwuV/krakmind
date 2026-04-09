# Use official Python 3.11 slim runtime as a parent image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Install system dependencies (wget, tar, Node.js for frontend build)
RUN apt-get update && apt-get install -y wget tar curl \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Download and install Kraken CLI
# Using the official krakenfx/kraken-cli v0.3.0 release
RUN wget https://github.com/krakenfx/kraken-cli/releases/download/v0.3.0/kraken-cli-x86_64-unknown-linux-gnu.tar.gz \
    && tar -xzf kraken-cli-x86_64-unknown-linux-gnu.tar.gz \
    && chmod +x kraken-cli-x86_64-unknown-linux-gnu/kraken \
    && mv kraken-cli-x86_64-unknown-linux-gnu/kraken /usr/local/bin/ \
    && rm -rf kraken-cli-x86_64-unknown-linux-gnu.tar.gz kraken-cli-x86_64-unknown-linux-gnu

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Build the React frontend
# Ensure package.json exists before attempting to install and build
RUN if [ -d "frontend" ]; then cd frontend && npm install && npm run build; fi

# Expose the dashboard API port
EXPOSE 8000

# Set environment variables for the container
ENV PYTHONUNBUFFERED=1

# Command to run the agent
CMD ["python", "main.py"]