#!/bin/bash
cd /root/kernel_work
echo "=== forbidden mechanism ==="
grep -rn "forbidden warning" Makefile arch/arm64/Makefile scripts/ 2>/dev/null | head -5
echo "=== patch ==="
grep -q "Wno-stringop-overflow" Makefile || \
  echo 'KBUILD_CFLAGS += -Wno-array-bounds -Wno-stringop-overflow -Wno-stringop-truncation -Wno-format-truncation -Wno-maybe-uninitialized -Wno-unused-but-set-variable -Wno-address -Wno-pointer-sign' >> Makefile
tail -3 Makefile
export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-
nohup make -j6 Image dtbs > /root/kernel_build5.log 2>&1 &
echo "BUILD5_STARTED pid=$!"
