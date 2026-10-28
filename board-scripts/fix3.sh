#!/bin/bash
JAR=/home/firefly/ws-scrcpy/dist/vendor/Genymobile/scrcpy/scrcpy-server.jar
VER=1.19-ws8
for d in 192.168.99.101:5555 192.168.99.103:5555 192.168.99.105:5555; do
  echo "== $d =="
  adb -s "$d" shell 'killall -9 app_process 2>/dev/null; pkill -9 -f scrcpy 2>/dev/null; echo cleaned' 2>&1 | head -1
  sleep 1
  adb -s "$d" push "$JAR" /data/local/tmp/scrcpy-server.jar 2>&1 | tail -1
  adb -s "$d" shell "CLASSPATH=/data/local/tmp/scrcpy-server.jar setsid nohup app_process / com.genymobile.scrcpy.Server $VER web ERROR 8886 true >/dev/null 2>&1 &" 2>&1 | head -1
  sleep 2
  echo -n "8886: "
  adb -s "$d" shell 'echo v4=$(cat /proc/net/tcp 2>/dev/null | grep -ci 22B6) v6=$(cat /proc/net/tcp6 2>/dev/null | grep -ci 22B6) proc=$(ps -A 2>/dev/null | grep -ci "app_process")' 2>&1 | head -1
done

