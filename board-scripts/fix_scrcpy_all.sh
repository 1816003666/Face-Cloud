#!/bin/bash
JAR=/home/firefly/ws-scrcpy/dist/vendor/Genymobile/scrcpy/scrcpy-server.jar
VER=1.19-ws8
DEVS=$(adb devices | awk 'NR>1 && $2=="device" && $1 ~ /:/ {print $1}')
echo "network boards: $(echo "$DEVS" | wc -l)"
for d in $DEVS; do
  echo "== $d =="
  adb -s "$d" shell "pkill -f com.genymobile.scrcpy.Server 2>/dev/null; sleep 1; rm -f /data/local/tmp/scrcpy-server.jar" 2>&1 | head -1
  adb -s "$d" push "$JAR" /data/local/tmp/scrcpy-server.jar 2>&1 | tail -1
  adb -s "$d" shell "CLASSPATH=/data/local/tmp/scrcpy-server.jar setsid nohup app_process / com.genymobile.scrcpy.Server $VER web ERROR 8886 true >/dev/null 2>&1 &" 2>&1 | head -1
  sleep 1
done
echo "== verify =="
for d in $DEVS; do
  J=$(adb -s "$d" shell "ls -la /data/local/tmp/scrcpy-server.jar 2>/dev/null | wc -l" 2>/dev/null | tr -d '\r')
  P=$(adb -s "$d" shell "cat /proc/net/tcp6 2>/dev/null | grep -ci 22B6" 2>/dev/null | tr -d '\r')
  echo "$d jar=$J port8886=$P"
done
