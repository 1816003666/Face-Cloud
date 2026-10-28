#!/bin/bash
set -e
cp /boot/extlinux/extlinux.conf /boot/extlinux/extlinux.conf.bak_redroid
sed -i '/initrd/d' /boot/extlinux/extlinux.conf
echo "=== new extlinux ==="
cat /boot/extlinux/extlinux.conf
sync
echo REBOOTING
reboot
