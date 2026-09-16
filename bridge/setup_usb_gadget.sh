#!/bin/bash
set -e

# 1. Ensure mtools is installed
if ! command -v mdir &>/dev/null; then
    echo "[setup_usb_gadget] mtools not found. Installing..."
    apt-get update -qq && apt-get install -y -qq --no-install-recommends mtools
fi

# 2. Ensure /etc/mtools.conf is configured
MTOOLS_CONF="/etc/mtools.conf"
if [ ! -f "$MTOOLS_CONF" ] || ! grep -q 'drive a: file="/var/arlo_storage.img"' "$MTOOLS_CONF" 2>/dev/null; then
    echo "[setup_usb_gadget] Configuring $MTOOLS_CONF..."
    cat << 'EOF' >> "$MTOOLS_CONF"

# Arlo USB Storage Bridge mapping
MTOOLS_SKIP_CHECK=1
drive a: file="/var/arlo_storage.img" partition=1
EOF
fi

# 3. Load composite driver and mount configfs
modprobe libcomposite 2>/dev/null || true
mount -t configfs none /sys/kernel/config 2>/dev/null || true

GADGET_DIR="/sys/kernel/config/usb_gadget/arlo_bridge"

# 4. Teardown any existing or stale gadget configuration
if [ -d "$GADGET_DIR" ]; then
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    rm -f "$GADGET_DIR/configs/c.1/mass_storage.usb0" 2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1" 2>/dev/null || true
    rmdir "$GADGET_DIR/functions/mass_storage.usb0" 2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR" 2>/dev/null || true
fi

# 5. Create gadget definition
mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR"

echo 0x1d6b > idVendor
echo 0x0104 > idProduct

mkdir -p strings/0x409
echo "0123456789" > strings/0x409/serialnumber
echo "OrangePi" > strings/0x409/manufacturer
echo "Arlo Bridge Storage" > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "Config 1: Mass Storage" > configs/c.1/strings/0x409/configuration

mkdir -p functions/mass_storage.usb0
echo "/var/arlo_storage.img" > functions/mass_storage.usb0/lun.0/file
echo 1 > functions/mass_storage.usb0/lun.0/removable

ln -s functions/mass_storage.usb0 configs/c.1/

# 6. Locate UDC and bind
UDC_NAME=$(ls /sys/class/udc 2>/dev/null | head -n 1)
if [ -n "$UDC_NAME" ]; then
    echo "$UDC_NAME" > UDC
else
    echo "musb-hdrc.4.auto" > UDC
fi

echo "[setup_usb_gadget] Gadget initialization complete."

# 7. Create Synology watching directory
# Step 1: Mount the Synology Share on the Orange Pi
# Ensure your Synology shared folder is mounted on the Orange Pi via SMB.

# Create a dedicated credentials file:

# Bash
# sudo mkdir -p /etc/synology
# sudo nano /etc/synology/cifs.creds
# Add your Synology user credentials:

# Ini, TOML
# username=YOUR_DSM_USERNAME
# password=YOUR_DSM_PASSWORD
# Secure permissions:

# Bash
# sudo chmod 600 /etc/synology/cifs.creds
# Create the mount directory:

# Bash
# sudo mkdir -p /mnt/synology_watch
# Add the persistent mount to /etc/fstab:

# Bash
# echo '//YOUR_SYNOLOGY_IP/docker/sightline-core/storage/watch /mnt/synology_watch cifs credentials=/etc/synology/cifs.creds,iocharset=utf8,_netdev,nofail 0 0' | sudo tee -a /etc/fstab
# sudo mount -a
