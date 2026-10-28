#!/bin/bash
cd /root/kernel_work
rm -rf .git
file /boot/config-4.4.194
if file /boot/config-4.4.194 | grep -q gzip; then
  zcat /boot/config-4.4.194 > .config
else
  cp /boot/config-4.4.194 .config
fi
sed -i 's/^CONFIG_EXTRA_FIRMWARE=.*/CONFIG_EXTRA_FIRMWARE=""/' .config
sed -i 's/^# CONFIG_ANDROID_BINDER_IPC is not set/CONFIG_ANDROID_BINDER_IPC=y/' .config
sed -i 's/^CONFIG_ANDROID_BINDER_IPC is not set/CONFIG_ANDROID_BINDER_IPC=y/' .config
sed -i 's/^# CONFIG_ASHMEM is not set/CONFIG_ASHMEM=y/' .config
sed -i 's/^CONFIG_ASHMEM is not set/CONFIG_ASHMEM=y/' .config
echo "===patched==="
grep -E 'CONFIG_ANDROID_BINDER_IPC=|CONFIG_ASHMEM=|CONFIG_EXTRA_FIRMWARE=' .config
echo "===count==="
grep -c 'CONFIG_' .config
echo CFG_OK
