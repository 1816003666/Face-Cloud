#!/bin/bash
set -e
cd /root
rm -rf kernel_work
tar xzf kernel_src.tar.gz
cd /root/kernel_work
rm -rf .git
df -h /root | tail -1
echo "===boot-config==="
file /boot/config-4.4.194
cp /boot/config-4.4.194 .config
sed -i 's/^CONFIG_EXTRA_FIRMWARE=.*/CONFIG_EXTRA_FIRMWARE=""/' .config
sed -i 's/^# CONFIG_ANDROID_BINDER_IPC is not set/CONFIG_ANDROID_BINDER_IPC=y/' .config
sed -i 's/^CONFIG_ANDROID_BINDER_IPC is not set/CONFIG_ANDROID_BINDER_IPC=y/' .config
sed -i 's/^# CONFIG_ASHMEM is not set/CONFIG_ASHMEM=y/' .config
sed -i 's/^CONFIG_ASHMEM is not set/CONFIG_ASHMEM=y/' .config
echo "===patched==="
grep -E 'CONFIG_ANDROID_BINDER_IPC=|CONFIG_ASHMEM=|CONFIG_EXTRA_FIRMWARE=' .config
echo "===gcc==="
which aarch64-linux-gnu-gcc
echo PREP_OK
