import asyncio
import logging
import time
import collections
import pandas as pd
import math  # ВАЖНО для округления
from hyperliquid.utils import constants
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
import eth_account

# =============================================================================
# ⚙️ НАСТРОЙКИ V14: 2-STAGE DCA (SOFT & HARD)
# =============================================================================

ACCOUNTS_CONFIG = {
    '1': {
        'long': {'key': '24c59bf2a3482b30f697e', 'addr': '0x00a988f0E7c1'},
        'short': {'key': '0x4e7c1dad0f0c43f', 'addr': '0x7C817800'}
    },
    '2': {
        'long': {'key': 'e707e8f734', 'addr': '0xFb0Ae859B'},
        'short': {'key': '0x3438d2193ce31d', 'addr': '0xd91F19e1422b1b'}
    },
    '3': {
        'long': {'key': 'f73e38d3fd1846e', 'addr': '0x8B34761'},
        'short': {'key': '0x20fc2f0a7878c550d', 'addr': '0x7BECdF6d3'}
    },
}

# Оставляем только настройки для ETH. Остальные можно удалить.
ZONES_RANGE = {
    'ETH': {'1': [1000, 2000], '2': [2000, 3000], '3': [3000, 4500]},
}

# Жестко указываем, что список монет состоит только из ETH
TARGET_COINS = ['ETH'] 
BTC_SYMBOL = 'BTC'

# ПАРАМЕТРЫ ВХОДА
ENTRY_SIZE_USD = 11

# --- ЛОГИКА ДВУХУРОВНЕВОГО УСРЕДНЕНИЯ ---
DCA_THRESHOLD_SIZE = 60.0  # Порог переключения стратегии ($)

# Этап 1: SOFT (пока позиция < $60)
SOFT_STEP = 3.0            # Усредняем каждые 3%
SOFT_MULT = 1.3            # Множитель 1.3

# Этап 2: HARD (когда позиция >= $60)
MARTINGALE_STEP = 10.0     # Усредняем каждые 10%
MARTINGALE_MULT = 1.4      # Множитель 1.4
# ----------------------------------------

# ТЕЙК-ПРОФИТЫ
TP_LEVELS = [0.5, 1.0, 1.5] 
TP_SPLIT  = [0.33, 0.33, 0.34] 

# СИГНАЛЫ И ТРЕНДЫ
BTC_IMPULSE_THRESHOLD = 0.15
COIN_SOLO_THRESHOLD = 0.1
LAG_RATIO = 0.8             
COOLDOWN = 300
WINDOW_SECONDS = 60
VOLATILITY_THRESHOLD = 2.0
SMA_PERIOD = 24
TREND_PERIOD = 48
RANGE_FILTER_POS = 0.25

# =============================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger("HyperDelta")

TICKS = collections.defaultdict(collections.deque)
CTX = {}
LAST_TRADE = {}
HISTORY_WINDOW = 300

class HyperBot:
    def __init__(self):
        self.info = Info(constants.MAINNET_API_URL)
        self.meta = self.info.meta()
        self.coin_meta = {asset['name']: asset for asset in self.meta['universe']}
        
        self.exchanges = {}
        for z_id, types in ACCOUNTS_CONFIG.items():
            for side_type in ['long', 'short']:
                cfg = types[side_type]
                try:
                    acc = eth_account.Account.from_key(cfg['key'])
                    self.exchanges[f"{z_id}_{side_type}"] = Exchange(acc, constants.MAINNET_API_URL)
                except Exception as e:
                    logger.error(f"❌ Ошибка ключа {z_id} {side_type}: {e}")

        self.pos = {z: {'long': {}, 'short': {}} for z in ACCOUNTS_CONFIG.keys()}

    # --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ОКРУГЛЕНИЯ ---
    def clean_price(self, price):
        """Округляет цену до 5 значимых цифр (стандарт Hyperliquid)"""
        if price == 0: return 0
        return float(f"{price:.5g}")

    def clean_sz(self, sz, coin):
        """Округляет объем согласно правилам монеты"""
        decimals = self.coin_meta.get(coin, {}).get('szDecimals', 2)
        return round(sz, decimals)
    # -------------------------------------------

    async def update_all_positions(self):
        for z_id, types in ACCOUNTS_CONFIG.items():
            for side_type in ['long', 'short']:
                addr = types[side_type]['addr']
                try:
                    # Запрос к API
                    state = self.info.user_state(addr)
                    
                    self.pos[z_id][side_type] = {}
                    if 'assetPositions' in state:
                        for p in state['assetPositions']:
                            pos = p['position']
                            sz = float(pos['szi'])
                            if sz != 0:
                                self.pos[z_id][side_type][pos['coin']] = {
                                    'size': sz,
                                    'entry': float(pos['entryPx']),
                                    'pnl': float(pos['unrealizedPnl'])
                                }
                    
                    # ВАЖНО: Делаем микро-паузу между запросами к разным аккаунтам,
                    # чтобы не слать 6 запросов за 0.01 сек
                    await asyncio.sleep(0.5) 

                except Exception as e:
                    # Теперь мы увидим ошибку, если она есть
                    logger.error(f"⚠️ Не могу обновить позиции {z_id}_{side_type}: {e}")

    async def update_context(self, coin):
        try:
            end_at = int(time.time() * 1000)
            start_at = end_at - (1000 * 60 * 60 * 24 * 5)
            candles = self.info.candles(coin, "1h", start_at, end_at)
            df = pd.DataFrame(candles, columns=['t','o','h','l','c','v','n'])
            for col in ['o','h','l','c','v']: df[col] = df[col].astype(float)
            curr = df['c'].iloc[-1]
            sma = df['c'].rolling(window=SMA_PERIOD).mean().iloc[-1]
            vol = ((df['h'].tail(TREND_PERIOD).max() - df['l'].tail(TREND_PERIOD).min()) / df['l'].tail(TREND_PERIOD).min()) * 100
            mode = "RANGE"
            if vol >= VOLATILITY_THRESHOLD:
                mode = "UP_TREND" if curr > sma else "DOWN_TREND"
            CTX[coin] = {'mode': mode, 'vol': vol}
        except: CTX[coin] = {'mode': "RANGE", 'vol': 0}

    async def manage_tp(self, zone_id, side_type, coin):
        addr = ACCOUNTS_CONFIG[zone_id][side_type]['addr']
        exch = self.exchanges[f"{zone_id}_{side_type}"]
        
        try:
            state = self.info.user_state(addr)
            p_data = next((p['position'] for p in state['assetPositions'] if p['position']['coin'] == coin), None)
            
            if not p_data or float(p_data['szi']) == 0: return 

            size, entry = float(p_data['szi']), float(p_data['entryPx'])
            is_long = size > 0
            abs_size = abs(size)
            
            # Считаем стоимость позиции для лимита $10
            current_price = float(self.info.all_mids().get(coin, entry))
            total_value_usd = abs_size * current_price

            # 1. Отмена старых ТП
            open_orders = self.info.open_orders(addr)
            tp_oids = [o['oid'] for o in open_orders if o['coin'] == coin]
            for oid in tp_oids: exch.cancel(coin, oid)
            if tp_oids: await asyncio.sleep(0.5)

            # 2. Выбор стратегии ТП (1 ордер или сетка)
            min_part_value = (total_value_usd * min(TP_SPLIT))
            placed_sz = 0
            
            if min_part_value >= 10.5: # Если часть > $10.5 -> ставим сетку
                levels = TP_LEVELS
                splits = TP_SPLIT
                logger.info(f"✅ {coin}: Объем ${total_value_usd:.1f} > $31. Ставим сетку 3 ТП.")
            else: # Иначе -> один ТП на 0.5% (ближний)
                logger.info(f"⚠️ {coin}: Объем ${total_value_usd:.1f} мал. Ставим 1 ТП на 0.5%")
                levels = [0.5] 
                splits = [1.0]

            # 3. Расстановка
            for i, pct in enumerate(levels):
                raw_tp_px = entry * (1 + pct/100 if is_long else 1 - pct/100)
                tp_price = self.clean_price(raw_tp_px)
                
                if i == len(levels) - 1:
                    part_sz = self.clean_sz(abs_size - placed_sz, coin)
                else:
                    part_sz = self.clean_sz(abs_size * splits[i], coin)
                
                if part_sz <= 0: continue
                
                res = exch.order(coin, not is_long, part_sz, tp_price, {"limit": {"tif": "Gtc"}, "reduceOnly": True})
                if res['status'] == 'ok': placed_sz += part_sz
                else: logger.error(f"❌ ТП Error {coin}: {res}")

        except Exception as e: logger.error(f"TP Manager Error: {e}")

    async def execute_trade(self, zone_id, side_type, coin, side, size_usd, price, is_avg=False):
        exch = self.exchanges.get(f"{zone_id}_{side_type}")
        is_buy = (side == 'buy')
        
        sz = self.clean_sz(size_usd / price, coin)
        limit_px = self.clean_price(price * 1.01 if is_buy else price * 0.99)

        prefix = "🆘 AVG" if is_avg else "🚀 NEW"
        logger.info(f"{prefix} [{zone_id} {side_type.upper()}] {coin} {side.upper()} ${size_usd:.1f} (Sz:{sz} Px:{limit_px})")
        
        try:
            res = exch.order(coin, is_buy, sz, limit_px, {"limit": {"tif": "Gtc"}})
            if res['status'] == 'ok':
                status = res['response']['data']['statuses'][0]
                if 'error' in status:
                    logger.error(f"❌ HL Reject: {status['error']}")
                else:
                    logger.info(f"✅ Executed: {coin}")
                    LAST_TRADE[coin] = time.time()
                    await asyncio.sleep(5) 
                    await self.manage_tp(zone_id, side_type, coin)
            else: logger.error(f"❌ API Fail: {res}")
        except Exception as e: logger.error(f"Trade Except: {e}")

    async def check_logic(self, coin, btc_chg, coin_chg, price, pos_pct):
        z_id = next((k for k,v in ZONES_RANGE[coin].items() if v[0] <= price < v[1]), None)
        if not z_id: return

        signal, is_btc_driven = None, False
        if btc_chg >= BTC_IMPULSE_THRESHOLD and coin_chg < (btc_chg * LAG_RATIO):
            signal, is_btc_driven = 'buy', True
        elif btc_chg <= -BTC_IMPULSE_THRESHOLD and coin_chg > (btc_chg * LAG_RATIO):
            signal, is_btc_driven = 'sell', True
        
        if not signal and abs(btc_chg) < 0.1:
            if coin_chg >= COIN_SOLO_THRESHOLD: signal = 'buy'
            elif coin_chg <= -COIN_SOLO_THRESHOLD: signal = 'sell'
        
        if not signal: return
        side_type = 'long' if signal == 'buy' else 'short'
        pos_data = self.pos[z_id][side_type].get(coin)

        # 1. УСРЕДНЕНИЕ (2 ЭТАПА)
        if pos_data:
            entry_px = pos_data['entry']
            # Считаем просадку по типу аккаунта
            if side_type == 'long': drop = ((price - entry_px) / entry_px) * 100
            else: drop = ((entry_px - price) / entry_px) * 100
            
            # Только если сигнал совпадает с позой
            correct_signal = 'buy' if side_type == 'long' else 'sell'
            if signal != correct_signal: return

            # ОПРЕДЕЛЯЕМ ЭТАП DCA
            current_sz_usd = abs(pos_data['size']) * price
            
            if current_sz_usd < DCA_THRESHOLD_SIZE:
                # Этап 1: SOFT
                req_step = SOFT_STEP  # 3.0%
                req_mult = SOFT_MULT  # 1.3
                stage_name = "SOFT"
            else:
                # Этап 2: HARD
                req_step = MARTINGALE_STEP # 10.0%
                req_mult = MARTINGALE_MULT # 1.4
                stage_name = "HARD"

            if drop <= -req_step:
                new_sz_usd = current_sz_usd * req_mult
                logger.info(f"📉 {coin} Drop {drop:.1f}% [{stage_name} DCA]. Adding ${new_sz_usd:.1f}...")
                await self.execute_trade(z_id, side_type, coin, signal, new_sz_usd, price, is_avg=True)
            return

        # 2. НОВЫЙ ВХОД
        if time.time() - LAST_TRADE.get(coin, 0) < COOLDOWN: return
        mode = CTX[coin]['mode']
        allowed = False
        if mode == "UP_TREND" and signal == 'buy': allowed = True
        elif mode == "DOWN_TREND" and signal == 'sell': allowed = True
        elif mode == "RANGE":
            if is_btc_driven: allowed = True 
            else:
                if signal == 'buy' and pos_pct <= RANGE_FILTER_POS: allowed = True
                elif signal == 'sell' and pos_pct >= (1.0 - RANGE_FILTER_POS): allowed = True
        
        if allowed: await self.execute_trade(z_id, side_type, coin, signal, ENTRY_SIZE_USD, price)

async def main():
    bot = HyperBot()
    logger.info("============== 🔭 HYPER DELTA DCA v14.1 (Anti-Ban) =============")
    
    # Первичная загрузка
    for t in TARGET_COINS + [BTC_SYMBOL]: 
        await bot.update_context(t)
    
    # Сразу обновляем позиции один раз перед стартом
    await bot.update_all_positions()

    last_debug = 0
    last_pos_update = 0      # Таймер для обновления позиций
    POS_UPDATE_INTERVAL = 15 # Обновляем позиции раз в 15 секунд

    while True:
        try:
            now = time.time()
            
            # --- ОПТИМИЗАЦИЯ ЗАПРОСОВ ---
            # 1. Обновляем позиции ТОЛЬКО раз в 15 секунд (или при старте)
            if now - last_pos_update > POS_UPDATE_INTERVAL:
                await bot.update_all_positions()
                last_pos_update = now
            
            # 2. Цены обновляем каждую итерацию (быстро)
            all_mids = bot.info.all_mids()
            # -----------------------------

            for sym in TARGET_COINS + [BTC_SYMBOL]:
                if sym in all_mids:
                    TICKS[sym].append((now, float(all_mids[sym])))
                    while TICKS[sym] and TICKS[sym][0][0] < now - HISTORY_WINDOW: TICKS[sym].popleft()
            
            if len(TICKS[BTC_SYMBOL]) < 2: 
                await asyncio.sleep(1); continue
            
            btc_p = float(all_mids[BTC_SYMBOL])
            btc_old = next((i for i in TICKS[BTC_SYMBOL] if i[0] >= now - WINDOW_SECONDS), TICKS[BTC_SYMBOL][0])
            btc_chg = ((btc_p - btc_old[1]) / btc_old[1]) * 100
            
            if now - last_debug > 10:
                summary = ""
                for z in ['1','2','3']:
                    for s in ['long','short']:
                        p_str = " ".join([f"{c}:{((float(all_mids[c])-d['entry'])/d['entry']*100*(1 if s=='long' else -1)):+.1f}%" 
                                         for c,d in bot.pos[z][s].items()])
                        if p_str: summary += f" [Z{z}_{s[0].upper()}: {p_str}]"
                logger.info(f"📊 BTC: {btc_p:.0f} ({btc_chg:+.3f}%){summary or ' [No Pos]'}")
                last_debug = now
            
            for coin in TARGET_COINS:
                if coin not in all_mids or coin not in CTX: continue
                p, hist = float(all_mids[coin]), TICKS[coin]
                if len(hist) < 2: continue
                old = next((i for i in hist if i[0] >= now - WINDOW_SECONDS), hist[0])
                c_chg = ((p - old[1]) / old[1]) * 100
                v = [x[1] for x in hist]
                pos_pct = (p - min(v)) / (max(v) - min(v)) if max(v)!=min(v) else 0.5
                
                await bot.check_logic(coin, btc_chg, c_chg, p, pos_pct)
            
            await asyncio.sleep(1)
        
        except Exception as e:
            # Если словили 429 - ждем дольше
            if "429" in str(e):
                logger.warning(f"⚠️ Rate Limit (429). Пауза 30 сек...")
                await asyncio.sleep(30)
            else:
                logger.error(f"Main Error: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
