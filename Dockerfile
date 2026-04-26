FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (if any)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY bot/ ./bot/
COPY ui/ ./ui/
COPY .env.example ./.env.example

# Expose port for optional dashboard (if needed)
EXPOSE 8000

# Command to run the bot
CMD ["python", "-m", "bot.main"]
