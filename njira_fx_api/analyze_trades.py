"""Dump per-trade outcome by analyzer to see which signal types win vs lose."""
import os, django, sys, io
sys.path.insert(0, '.')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'forex.settings')
django.setup()
from forex.backtest.parser import parse_tf_csv
from forex.backtest.engine import BacktestEngine
from forex.analysis.signal_engine import reset_funnel

csv_path = sys.argv[1]
with open(csv_path, encoding='utf-8') as fh:
    text = fh.read()
candles = parse_tf_csv(io.StringIO(text), symbol='EURUSD', timeframe=60)
reset_funnel()
res = BacktestEngine().run(candles, 'EURUSD', strategies=['malysian'],
                           initial_balance=10000.0, risk_per_trade=0.01,
                           base_timeframe=60)
print(f"trades={len(res.trades)} pnl={res.total_pnl:.2f} win={res.overall_win_rate:.1f}%")
from collections import defaultdict
by = defaultdict(lambda: [0, 0, 0.0])  # analyzer -> [n, wins, pnl]
for t in res.trades:
    ev = t.get('evidence') or {}
    a = ev.get('analyzer', '?')
    by[a][0] += 1
    if t.get('outcome') == 'win':
        by[a][1] += 1
    by[a][2] += t.get('pnl', 0.0)
print(f"{'analyzer':<12} {'n':>3} {'win':>4} {'wr%':>6} {'pnl':>10}")
for a, (n, w, pnl) in sorted(by.items()):
    wr = (w / n * 100) if n else 0
    print(f"{a:<12} {n:>3} {w:>4} {wr:>5.1f}% {pnl:>10.2f}")
# R:R actually achieved
rrs = [t.get('risk_reward_ratio') for t in res.trades if t.get('risk_reward_ratio')]
if rrs:
    print("median planned R:R =", round(sorted(rrs)[len(rrs)//2], 2))
