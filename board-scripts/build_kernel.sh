#!/bin/bash
set -x
cd /root/linux-4.4.194
cp /boot/config-4.4.194 .config
./scripts/config --enable ANDROID_BINDER_IPC
./scripts/config --enable ASHMEM
./scripts/config --set-str EXTRA_FIRMWARE ""
./scripts/config --set-str EXTRA_FIRMWARE_DIR ""
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig > /root/k_olddef.log 2>&1 || exit 1
grep -E "CONFIG_ANDROID_BINDER_IPC=|CONFIG_ASHMEM=" .config
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j6 Image dtbs > /root/k_build.log 2>&1 || exit 1
ls -la arch/arm64/boot/Image 2>&1
ls -la arch/arm64/boot/dts/rockchip/rk3399-firefly-aio-lvds.dtb 2>&1
echo KERNEL_DONE
