import os, re, json, sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
DATABASE = os.environ.get('DATABASE_PATH', 'tracker.db')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            name TEXT NOT NULL,
            selector TEXT,
            current_price REAL,
            target_price REAL,
            currency TEXT DEFAULT 'Kč',
            last_checked TEXT,
            created_at TEXT,
            active INTEGER DEFAULT 1
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            price REAL,
            checked_at TEXT
        )''')

# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, skipping notification")
        return False
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    try:
        r = requests.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML'
        }, timeout=10)
        return r.ok
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

# ──────────────────────────────────────────────
# SCRAPING
# ──────────────────────────────────────────────

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'cs-CZ,cs;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}

def parse_price_text(text):
    """Extract float price from text like '1 299 Kč' or '1,299.00'"""
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').strip()
    # Remove currency symbols, keep digits, spaces, commas, dots
    cleaned = re.sub(r'[^\d\s,.]', '', text)
    # Try to find price pattern
    matches = re.findall(r'[\d]+(?:[\s][\d]{3})*(?:[,.][\d]{1,2})?', cleaned)
    for m in matches:
        try:
            val = float(m.replace(' ', '').replace(',', '.'))
            if 1 < val < 10_000_000:
                return val
        except:
            pass
    return None

def scrape_page(url, custom_selector=None):
    """Scrape a URL and return list of found products with prices."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        raise Exception(f"Nepodařilo se načíst stránku: {e}")

    soup = BeautifulSoup(resp.text, 'html.parser')
    results = []
    seen_prices = set()

    def add_result(name, price, selector, raw=''):
        if price and price not in seen_prices and 1 < price < 10_000_000:
            seen_prices.add(price)
            results.append({
                'name': (name or 'Produkt')[:120],
                'price': price,
                'selector': selector,
                'raw': raw
            })

    # 1. JSON-LD schema.org
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or '{}')
            if isinstance(data, list):
                data = data[0] if data else {}
            items_to_check = []
            if data.get('@type') == 'Product':
                items_to_check = [data]
            elif data.get('@type') == 'ItemList':
                items_to_check = [e.get('item', e) for e in data.get('itemListElement', [])]
            for item in items_to_check:
                offers = item.get('offers', {})
                if isinstance(offers, list): offers = offers[0] if offers else {}
                price = offers.get('price') or offers.get('lowPrice')
                name = item.get('name', '')
                if price:
                    add_result(name, float(str(price).replace(',', '.')), 'json-ld', str(price))
        except:
            pass

    # 2. Meta tags
    for prop in ['product:price:amount', 'og:price:amount']:
        tag = soup.find('meta', property=prop)
        if tag:
            price = parse_price_text(tag.get('content', ''))
            name_tag = soup.find('meta', property='og:title') or soup.find('title')
            name = ''
            if name_tag:
                name = name_tag.get('content') or (name_tag.string or '')
            add_result(name, price, 'meta-tag', tag.get('content', ''))

    # 3. itemprop
    for elem in soup.find_all(itemprop='price'):
        raw = elem.get('content') or elem.get_text()
        price = parse_price_text(raw)
        name_elem = soup.find(itemprop='name')
        name = name_elem.get_text(strip=True) if name_elem else ''
        add_result(name, price, 'itemprop', raw)

    # 4. Custom CSS selector (user-specified)
    if custom_selector:
        for elem in soup.select(custom_selector)[:5]:
            raw = elem.get_text(strip=True)
            price = parse_price_text(raw)
            h1 = soup.find('h1')
            name = h1.get_text(strip=True) if h1 else 'Produkt'
            add_result(name, price, custom_selector, raw)

    # 5. Common price CSS selectors (fallback)
    if not results:
        selectors = [
            '.price-box__price', '.product-price__value', '.c-price__main',
            '.price--main', '.price_color', '.now-price', '.price-now',
            '[class*="price"][class*="final"]', '[class*="price"][class*="current"]',
            '[class*="price"][class*="sale"]', '.price-tag', '.price__value',
            '.cena', '.product__cena', '#price', '.Price', '.price',
            '[data-price]', '[data-testid*="price"]',
        ]
        h1 = soup.find('h1')
        name = h1.get_text(strip=True) if h1 else 'Produkt'
        for sel in selectors:
            for elem in soup.select(sel)[:3]:
                raw = elem.get('data-price') or elem.get_text(strip=True)
                price = parse_price_text(raw)
                if price:
                    add_result(name, price, sel, raw)
            if results:
                break

    return results

# ──────────────────────────────────────────────
# SCHEDULED CHECKS
# ──────────────────────────────────────────────

def check_all_items():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Running scheduled price check...")
    with get_db() as conn:
        items = conn.execute('SELECT * FROM items WHERE active=1').fetchall()
        for item in items:
            try:
                results = scrape_page(item['url'], item['selector'])
                if not results:
                    continue
                new_price = results[0]['price']
                old_price = item['current_price']
                now = datetime.now().isoformat()

                conn.execute('UPDATE items SET current_price=?, last_checked=? WHERE id=?',
                             (new_price, now, item['id']))
                conn.execute('INSERT INTO price_history (item_id, price, checked_at) VALUES (?,?,?)',
                             (item['id'], new_price, now))

                cur = item['currency'] or 'Kč'

                if old_price and new_price < old_price:
                    diff = old_price - new_price
                    pct = diff / old_price * 100
                    msg = (
                        f"🐕 <b>Hlídací pes — cena klesla!</b>\n\n"
                        f"📦 <b>{item['name']}</b>\n"
                        f"Stará cena: {old_price:,.0f} {cur}\n"
                        f"Nová cena: <b>{new_price:,.0f} {cur}</b>\n"
                        f"Úspora: {diff:,.0f} {cur} ({pct:.1f} %)\n\n"
                        f"🔗 <a href='{item['url']}'>Přejít na produkt</a>"
                    )
                    send_telegram(msg)
                    print(f"  Price drop: {item['name']} {old_price}→{new_price}")

                target = item['target_price']
                if target and new_price <= target:
                    msg = (
                        f"🎯 <b>Cílová cena dosažena!</b>\n\n"
                        f"📦 <b>{item['name']}</b>\n"
                        f"Aktuální cena: <b>{new_price:,.0f} {cur}</b>\n"
                        f"Tvoje cílová cena: {target:,.0f} {cur}\n\n"
                        f"🔗 <a href='{item['url']}'>Přejít na produkt</a>"
                    )
                    send_telegram(msg)

            except Exception as e:
                print(f"  Error checking {item['url']}: {e}")

# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    return 'OK', 200

@app.route('/api/scrape', methods=['POST'])
def api_scrape():
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL je povinná'}), 400
    if not url.startswith('http'):
        url = 'https://' + url
    try:
        products = scrape_page(url)
        return jsonify({'products': products, 'url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/items', methods=['GET'])
def api_get_items():
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM items WHERE active=1 ORDER BY created_at DESC'
        ).fetchall()
        return jsonify([dict(r) for r in rows])

@app.route('/api/items', methods=['POST'])
def api_add_item():
    data = request.get_json() or {}
    required = ['url', 'name', 'price']
    for f in required:
        if not data.get(f):
            return jsonify({'error': f'Chybí pole: {f}'}), 400
    with get_db() as conn:
        conn.execute('''
            INSERT INTO items (url, name, selector, current_price, target_price, currency, last_checked, created_at)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (
            data['url'], data['name'], data.get('selector'),
            float(data['price']),
            float(data['target_price']) if data.get('target_price') else None,
            data.get('currency', 'Kč'),
            datetime.now().isoformat(), datetime.now().isoformat()
        ))
    return jsonify({'ok': True})

@app.route('/api/items/<int:item_id>', methods=['DELETE'])
def api_delete_item(item_id):
    with get_db() as conn:
        conn.execute('UPDATE items SET active=0 WHERE id=?', (item_id,))
    return jsonify({'ok': True})

@app.route('/api/items/<int:item_id>/history', methods=['GET'])
def api_item_history(item_id):
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM price_history WHERE item_id=? ORDER BY checked_at DESC LIMIT 30',
            (item_id,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])

@app.route('/api/check-now', methods=['POST'])
def api_check_now():
    check_all_items()
    return jsonify({'ok': True})

@app.route('/api/test-telegram', methods=['POST'])
def api_test_telegram():
    ok = send_telegram("🐕 Hlídací pes je aktivní a funguje! Test notifikace.")
    if ok:
        return jsonify({'ok': True, 'message': 'Zpráva odeslána na Telegram!'})
    return jsonify({'ok': False, 'message': 'Chyba — zkontroluj TELEGRAM_TOKEN a TELEGRAM_CHAT_ID'}), 500

# ──────────────────────────────────────────────
# START
# ──────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    scheduler = BackgroundScheduler(timezone='Europe/Prague')
    scheduler.add_job(check_all_items, 'interval', hours=1, id='price_check')
    scheduler.start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
