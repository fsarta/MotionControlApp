# Use an official Python image
FROM docker.io/library/python:3.9-slim

# Set the working directory
WORKDIR /app

# # Install python3, pip, and required system tools
# RUN apt-get update && apt-get install -y \
#     python3 \
#     python3-pip \
#     sshpass \
#     openssh-client \
#     && rm -rf /var/lib/apt/lists/*

# Install dbus to communicate with the host's systemd
RUN apt-get update && apt-get install -y \
    dbus \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (--no-cache-dir saves ~50MB)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy vendor-specific package (Phoenix Contact gRPC stubs, not on PyPI)
COPY pylibs/pxc_grpc /usr/local/lib/python3.9/site-packages/pxc_grpc

# Copy the rest of the application code
COPY . .

# Make main.py executable
RUN chmod +x main.py

# Launch the app
CMD ["python3", "-u", "main.py"]

