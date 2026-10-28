#!/bin/bash
JAR=/home/firefly/ws-scrcpy/dist/vendor/Genymobile/scrcpy/scrcpy-server.jar
VER=1.19-ws8
for d in $(adb devices | awk 'NR>1 && $2=="device" {print $1}'); do
  echo "== deploy $d =="
  adb -s "$d" push "$JAR" /data/local/tmp/scrcpy-server.jar 2>&1 | tail -1
  adb -s "$d" shell "CLASSPATH=/data/local/tmp/scrcpy-server.jar setsid nohup app_process / com.genymobile.scrcpy.Server $VER web ERROR 8886 true >/dev/null 2>&1 &" 2>&1 | head -1
  sleep 1
done
echo "== verify 8886 =="
for d in $(adb devices | awk 'NR>1 && $2=="device" {print $1}'); do
  echo -n "$d: "
  adb -s "$d" shell "cat /proc/net/tcp 2>/dev/null | grep -qi '22B6' && echo OK || echo NO" 2>&1 | head -1
done

