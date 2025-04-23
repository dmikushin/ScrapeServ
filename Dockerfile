# Use the official Ubuntu base image
FROM ubuntu:22.04

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive

# Install necessary packages
RUN apt-get update && \
    apt-get install --no-install-recommends -y \
        python3 \
        python3-pip \
        redis-server \
        wget \
        curl \
        gnupg2 \
        supervisor && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Playwright and Flask
COPY scraper/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# Install Playwright dependencies
RUN playwright install-deps

# Install firefox
RUN playwright install firefox

# Create a working directory
WORKDIR /app

# Copy your Flask application code into the container
COPY scraper /app

# Expose the port the app runs on
EXPOSE 5006

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
