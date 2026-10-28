#!/bin/bash
set -e
echo "=== mmc config ==="
grep -E "CONFIG_MMC_DW_ROCKCHIP|CONFIG_MMC_DW=|CONFIG_EXT4_FS=" /boot/config-4.4.194
echo "=== backup ==="
cp /boot/Image-4.4.194 /boot/Image-4.4.194.bak_redroid
cp /boot/rk3399-firefly-aio-lvds.dtb /boot/rk3399-firefly-aio-lvds.dtb.bak_redroid
echo "=== replace ==="
cp /root/kernel_work/arch/arm64/boot/Image /boot/Image-4.4.194
cp /root/kernel_work/arch/arm64/boot/dts/rockchip/rk3399-firefly-aio-lvds.dtb /boot/rk3399-firefly-aio-lvds.dtb
sync
echo "=== verify ==="
ls -la /boot/Image-4.4.194 /boot/rk3399-firefly-aio-lvds.dtb
md5sum /root/kernel_work/arch/arm64/boot/Image /boot/Image-4.4.194
echo REPLACE_OK
