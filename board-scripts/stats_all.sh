#!/bin/bash
for d in $(adb devices | awk 'NR>1 && $2=="device" {print $1}'); do
  echo "=== $d ==="
  adb -s "$d" shell 'df -h /data 2>/dev/null | tail -1; df 2>/dev/null | grep -E " /data$" | tail -1; echo "part_mmcblk1=$(cat /proc/partitions 2>/dev/null | grep -E \"^ *[0-9]+ +[0-9]+ +[0-9]+ mmcblk1 \" | head -1)"; echo "model=$(getprop ro.product.model) ver=$(getprop ro.build.version.release)"; echo "v4=$(cat /proc/net/tcp 2>/dev/null | grep -ci 22B6) v6=$(cat /proc/net/tcp6 2>/dev/null | grep -ci 22B6)"' 2>&1 | tr '\n' ' '
  echo
done

