#!/bin/bash
set -x
cd /root/linux-4.4.194
cp /boot/config-4.4.194 .config
./scripts/config --module ANDROID_BINDER_IPC
./scripts/config --module ASHMEM
grep -E "CONFIG_ANDROID_BINDER_IPC=|CONFIG_ASHMEM=" .config
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- modules_prepare > /root/b3_prepare.log 2>&1 || exit 1
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- M=drivers/android > /root/b3_binder.log 2>&1 || exit 1
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- M=drivers/staging/android > /root/b3_ashmem.log 2>&1 || exit 1
ls -la drivers/android/binder.ko drivers/staging/android/ashmem.ko 2>&1
echo BUILD_DONE
