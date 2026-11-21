#!/bin/bash
# Replays every bot we have a real practice score for under several house-bot configs.
# Real: example 1.15, v1 3.38, v3 4.17, v5 4.25, v6 4.33 (4.27/4.40), v7 4.13, roof 4.04, aggro 3.88
B="sim/bots/example_bot.py sim/bots/v1_bot.py sim/bots/v3_bot.py sim/bots/v5_bot.py sim/bots/v6_bot.py sim/bots/v7_bot.py sim/bots/v7_bot.py:anchor=1,anchor_laser=0 sim/bots/v7_bot.py:pick_fights=1,engage_r=200"
N=${N:-96}
run () { echo "== $1"; shift; python -m sim.run --mode practice --n $N --workers 23 --seed 40000 --bots $B --set "$@" 2>&1 | grep -E "^practice"; }
run K0 easy.lag=0
run K1 easy.lead=0 medium.lead=0 hard.lead=0 expert.lead=0 easy.lag=0.25 medium.lag=0.25 hard.lag=0.25 expert.lag=0.25
run K2 easy.lead=0 medium.lead=0 hard.lead=0 expert.lead=0 easy.lag=0.25 medium.lag=0.25 hard.lag=0.25 expert.lag=0.25 hard.laser=1 expert.laser=1
run K3 easy.lead=0 medium.lead=0 hard.lead=0 expert.lead=0 easy.lag=0.4 medium.lag=0.4 hard.lag=0.4 expert.lag=0.4 expert.laser=1 easy.sigma=0.032 medium.sigma=0.02 hard.sigma=0.012 expert.sigma=0.006
