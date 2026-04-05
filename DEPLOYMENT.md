# ArbMind Deployment Guide

This guide covers the best platforms for deploying your ArbMind autonomous trading agent, ensuring high uptime, low latency, and proper environment configuration for the `kraken-cli` binary.

## Recommended Hosting Platforms

Since ArbMind runs continuous background loops (`asyncio`) and a FastAPI dashboard server, it cannot be deployed on serverless environments like AWS Lambda or Vercel (which kill idle processes). You need a **Virtual Private Server (VPS)** or a **Containerized PaaS**.

### 1. Railway / Render / Fly.io (Containerized PaaS - Easiest)
These platforms allow you to deploy directly from your GitHub repository using a `Dockerfile`. They handle the server infrastructure automatically.
- **Cost**: ~$5 - $10 / month
- **Pros**: Auto-deploys on `git push`, built-in metrics, easy environment variable management.
- **Cons**: Slightly less control over the underlying OS.

### 2. DigitalOcean Droplet / AWS EC2 / Hetzner (VPS - Best for Trading)
Renting a Linux server gives you full root access to install binaries and manage your networking. This is the industry standard for trading bots to ensure low latency.
- **Cost**: ~$5 - $20 / month (A 1GB or 2GB RAM instance is plenty).
- **Pros**: Full control, dedicated IP, you can choose a server location close to the exchange's data centers (e.g., AWS Tokyo or London) to reduce ping.

---

## Deployment Setup (VPS/Ubuntu Example)

If you choose a Linux VPS (Ubuntu), follow these exact steps to set up the environment, install the Kraken CLI, and run the agent 24/7.

### Step 1: Install System Dependencies
SSH into your server and update the package list, then install Python and necessary build tools:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv wget tar curl tmux
```

### Step 2: Install Kraken CLI
You need to download the official `kraken-cli` binary, extract it, and move it to a system path so the Python `subprocess` can find it.

```bash
# Download the latest Linux release
wget https://github.com/krakenfx/kraken-cli/releases/download/v0.3.0/kraken-cli-x86_64-unknown-linux-gnu.tar.gz

# Extract the tarball
tar -xzf kraken-cli-x86_64-unknown-linux-gnu.tar.gz

# Make it executable and move to bin
chmod +x kraken
sudo mv kraken /usr/local/bin/

# Verify installation
kraken --version
```

### Step 3: Clone the Repository & Install Python Packages
```bash
git clone https://github.com/YOUR-USERNAME/ArbMind.git
cd ArbMind

# Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Step 4: Configure Environment Variables
Create your `.env` file with your API keys and configuration:
```bash
nano .env
```
Paste your keys:
```env
OPENROUTER_API_KEY="sk-or-v1-..."
PAPER_MODE="true"
BASE_RPC_URL="https://..."
ARB_WALLET_PRIVATE_KEY="..."
KRAKEN_API_KEY="..."
KRAKEN_API_SECRET="..."
```

### Step 5: Authenticate Kraken CLI
Run the initial setup to link your API keys to the CLI binary:
```bash
kraken auth --api-key YOUR_KRAKEN_API_KEY --api-secret YOUR_KRAKEN_API_SECRET
```

### Step 6: Run the Agent 24/7
To keep the bot running after you close your SSH terminal, use `tmux` or `pm2`:

```bash
# Start a new tmux session
tmux new -s arbmind

# Run the bot
python main.py
```
*(To detach from the tmux session and leave it running in the background, press `Ctrl+B`, then `D`. To re-attach later, type `tmux attach -t arbmind`.)*

---

## Docker Deployment (Alternative)

If you prefer deploying via Docker (for Railway/Render), create this `Dockerfile` in your root directory. It automatically installs the Kraken CLI alongside your Python code.

```dockerfile
# Use official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory
WORKDIR /app

# Install system dependencies (wget and tar)
RUN apt-get update && apt-get install -y wget tar && rm -rf /var/lib/apt/lists/*

# Download and install Kraken CLI
RUN wget https://github.com/krakenfx/kraken-cli/releases/download/v0.3.0/kraken-cli-x86_64-unknown-linux-gnu.tar.gz \
    && tar -xzf kraken-cli-x86_64-unknown-linux-gnu.tar.gz \
    && chmod +x kraken \
    && mv kraken /usr/local/bin/ \
    && rm kraken-cli-x86_64-unknown-linux-gnu.tar.gz

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose the dashboard API port
EXPOSE 8000

# Command to run the agent
CMD ["python", "main.py"]
```
