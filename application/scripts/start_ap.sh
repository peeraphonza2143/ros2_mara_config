#!/bin/bash
# start_ap.sh
# Brings up a WiFi Access Point on the mini PC using NetworkManager (nmcli),
# so a phone can connect directly to the robot without any external router.

set -e

IFACE="wlp3s0"
SSID="AMR_Robot"
PASSWORD="amr12345"
CON_NAME="AMR_Hotspot"
STATIC_IP="192.168.4.1/24"

# Remove any leftover hotspot connection profile from a previous run
nmcli connection delete "$CON_NAME" 2>/dev/null || true

nmcli device wifi hotspot ifname "$IFACE" con-name "$CON_NAME" ssid "$SSID" password "$PASSWORD"
nmcli connection modify "$CON_NAME" ipv4.addresses "$STATIC_IP"
nmcli connection up "$CON_NAME"

echo "Hotspot started: SSID=$SSID  IP=${STATIC_IP%/*}"
