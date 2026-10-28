#!/bin/bash
cd /root/kernel_work
grep -q "Wno-array-bounds" Makefile || echo 'KBUILD_CFLAGS += -Wno-array-bounds' >> Makefile
echo "=== patched makefile ==="
tail -2 Makefile
export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
nohup make -j6 Image dtbs > /root/kernel_build4.log 2>&1 &
echo "BUILD4_STARTED pid=$!"
