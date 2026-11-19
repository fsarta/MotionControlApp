# 🚀 AXIS Control Panel - Deployment Guide

This guide describes the correct procedure to perform a cross-platform build (from a Windows PC to an ARMv7 target), compress the image, and deploy it onto a PLCnext-based industrial controller (Podman).

### Prerequisites
* Docker or Podman Desktop installed on your Windows PC.
* SSH or terminal access to the target PLC.

---

### 📋 Step-by-Step Procedure

#### 1. Check the target architecture (on the PLC)
Access the PLC terminal and run this command to confirm that the architecture is indeed 32-bit ARM (`armv7l` or similar), which is required to set the proper compilation flags.

```bash
uname -m

```

#### 2. Build on Windows PC
Build the Docker image specifying the ARMv7 platform (`linux/arm/v7`). Using `--no-cache` ensures a clean build from scratch, and `--load` loads the image directly into the local Docker daemon memory.

```bash
docker buildx build --platform linux/arm/v7 -t axis-control-panel:latest --no-cache --load .

```

#### 3. Export the Docker image
Save the newly created Docker image into an uncompressed tar archive inside the local `images` directory.

```bash
docker save axis-control-panel:latest -o images\axis-control-panel.tar

```

#### 4. Compress the archive (Gzip)
Use a temporary Alpine container to compress the `.tar` file into `.tar.gz` format via a shared volume, significantly reducing the file size for transfer to the PLC.

```bash
docker run --rm -v "%cd%/images:/data" docker.io/library/alpine:latest gzip /data/axis-control-panel.tar

```

#### 5. Verify file size
Check that the compressed file has been successfully created and verify its final size.

```bash
dir images\axis-control-panel.tar.gz

```

#### 6. Retrieve the Image Hash (Unique ID)
Extract the full SHA-256 hash of the newly created image. This value is typically required by container configuration files.

```bash
docker images --no-trunc --format "{{.ID}}" axis-control-panel:latest

```

#### 7. Update metadata hashes
Take the hash obtained in the previous step and update it manually inside configuration files:
* `axis-control.container`
* `app_info.json`

#### 8. Transfer files to the PLC
Copy the compressed archive `axis-control-panel.tar.gz` from your Windows PC to the PLC controller (via SCP, SFTP, or USB).

#### 9. Load the image into the Podman runtime (on the PLC)
Access the PLC via terminal and import the tar.gz archive into the Podman container engine.

```bash
podman load -i axis-control-panel.tar.gz

```

#### 10. Run the Container on the PLC
Start the container by mapping the required system volumes for PLCnext interaction, exposing the desired port (e.g., `8080`), and using the host network.

```bash
podman run --rm -it --network host \
  -v /run/plcnext:/run/plcnext:ro \
  -v /opt/plcnext/projects:/opt/plcnext/projects \
  -v /opt/plcnext/logs:/opt/plcnext/logs \
  -v /opt/plcnext/user:/opt/plcnext/user \
  -e WEBSERVER_PORT=8080 \
  axis-control-panel:latest
```