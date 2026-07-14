#!/bin/bash


source /etc/profile.d/clash-for-linux.sh
source /software/polybot/venv/bin/activate && proxy_on && nohup python bot.py  --mode console &
