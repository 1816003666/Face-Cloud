#!/bin/bash
for p in $(pgrep -f "run_stable.py"); do kill -9 $p 2>/dev/null; done
for p in $(pgrep -f "screenrecord"); do kill -9 $p 2>/dev/null; done
sleep 2
cd /home/firefly/cloud-phone/backend
setsid nohup venv/bin/python run_stable.py > backend.log 2>&1 < /dev/null &
echo started
