#!/bin/bash

# Check VPN status
status=$(systemctl is-active openvpn-client@client)

# Output for Waybar
if [ "$status" = "active" ]; then
    echo '{"text": "", "class": "vpn-on"}'
else
    echo '{"text": "", "class": "vpn-off"}'
fi
