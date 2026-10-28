#!/bin/bash
cd /root/kernel_work
export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
make olddefconfig > /root/kb_olddef.log 2>&1
nohup make -j6 Image dtbs > /root/kernel_build2.log 2>&1 &
echo "BUILD_STARTED pid=$!"
