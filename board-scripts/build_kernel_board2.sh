#!/bin/bash
ln -sf /usr/bin/python3 /usr/bin/python
python --version 2>&1
cd /root/kernel_work
export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
nohup make -j6 Image dtbs > /root/kernel_build3.log 2>&1 &
echo "BUILD_RESTARTED pid=$!"
