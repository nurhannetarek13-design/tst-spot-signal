import json
from pathlib import Path

USER = Path('/octobot/user')
CONFIG = USER / 'config.json'
PROFILE = USER / 'profiles' / 'smart_dca' / 'profile.json'
DCA = USER / 'profiles' / 'smart_dca' / 'specific_config' / 'DCATradingMode.json'


def load(path: Path):
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)


def save(path: Path, data):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)
        f.write('\n')
    tmp.replace(path)


# Fail closed: these files are installed by OctoBot before normal startup.
for required in (CONFIG, PROFILE, DCA):
    if not required.exists():
        raise SystemExit(f'Missing required OctoBot file: {required}')

# Activate the built-in Smart DCA profile while keeping exchange credentials untouched.
config = load(CONFIG)
config['profile'] = 'smart_dca'
# Ensure this bootstrap can never switch the exchange away from Spot.
binance = config.setdefault('exchanges', {}).setdefault('binance', {})
binance['enabled'] = True
binance['exchange-type'] = 'spot'
save(CONFIG, config)

# Paper account mirrors the intended small-capital test: 50 USDT, no starting crypto.
profile = load(PROFILE)
pconfig = profile.setdefault('config', {})
pconfig.setdefault('trader', {})['enabled'] = False
sim = pconfig.setdefault('trader-simulator', {})
sim['enabled'] = True
sim['starting-portfolio'] = {'BTC': 0, 'ETH': 0, 'USDT': 50}
pconfig['exchanges'] = {'binance': {'enabled': True, 'exchange-type': 'spot'}}
pconfig['crypto-currencies'] = {
    'Bitcoin': {'enabled': True, 'pairs': ['BTC/USDT']}
}
pconfig.setdefault('trading', {})['reference-market'] = 'USDT'
save(PROFILE, profile)

# Make order notional practical for a 50 USDT paper portfolio while retaining the
# built-in strategy logic and paired take-profit exits.
dca = load(DCA)
dca['buy_order_amount'] = '25%t'
dca['secondary_entry_orders_amount'] = '25%t'
dca['secondary_entry_orders_count'] = 2
dca['minutes_before_next_buy'] = 240
dca['use_secondary_entry_orders'] = True
dca['use_take_profit_exit_orders'] = True
dca['use_market_entry_orders'] = False
# Never add leverage, futures, live-trader flags, or credentials here.
save(DCA, dca)

print('OCTOBOT_PAPER_BOOTSTRAP_OK profile=smart_dca balance=50_USDT pair=BTC/USDT sizing=25pct cooldown=240m')
