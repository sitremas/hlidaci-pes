import os, re, json, sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
import requests

app = Flask(__name__)
DATABASE = os.environ.get('DATABASE_PATH', 'tracker.db')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'cs-CZ,cs;q=0.9,en;q=0.8',
}

GQL_HEADERS = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Origin': 'https://www.ticketswap.com',
    'Referer': 'https://www.ticketswap.com/',
}

# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            name TEXT NOT NULL,
            max_price REAL,
            currency TEXT DEFAULT 'EUR',
            last_checked TEXT,
            created_at TEXT,
            active INTEGER DEFAULT 1
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS seen_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            listing_id TEXT NOT NULL,
            price REAL,
            num_tickets INTEGER,
            listing_url TEXT,
            found_at TEXT,
            UNIQUE(event_id, listing_id)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            listing_id TEXT,
            price REAL,
            num_tickets INTEGER,
            listing_url TEXT,
            sent_at TEXT
        )''')

# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return False
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'},
            timeout=10
        )
        return r.ok
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

# ──────────────────────────────────────────────
# TICKETSWAP SCRAPING
# ──────────────────────────────────────────────

def extract_ids_from_url(url):
    """Parse UUID, numeric ID, and slug from a TicketSwap URL."""
    clean = url.split('?')[0].rstrip('/')
    uuid_match = re.search(
        r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', clean
    )
    uuid = uuid_match.group(1) if uuid_match else None
    num_match = re.search(r'/(\d{4,})(?:/|$)', clean)
    numeric_id = num_match.group(1) if num_match else None
    slug_match = re.search(r'/event/([^?#]+)', clean)
    full_slug = slug_match.group(1) if slug_match else None
    event_slug = full_slug.split('/')[0] if full_slug else None
    return uuid, numeric_id, full_slug, event_slug

def parse_listings_from_edges(edges, fallback_url):
    listings = []
    for edge in edges:
        node = edge.get('node', {})
        listing_id = str(node.get('id', ''))
        num_tickets = node.get('numberOfTicketsInListing', 1)
        price_obj = node.get('price', {}).get('totalPriceWithTransactionFee', {})
        amount = price_obj.get('amount')
        currency = price_obj.get('currency', 'EUR')
        if amount is None:
            continue
        try:
            price = float(amount) / 100
        except:
            price = float(amount)
        listing_url = node.get('uri', '') or fallback_url
        if listing_url and not listing_url.startswith('http'):
            listing_url = 'https://www.ticketswap.com' + listing_url
        listings.append({
            'id': listing_id, 'price': price, 'currency': currency,
            'num_tickets': num_tickets, 'url': listing_url
        })
    return listings

def gql_post(query, variables):
    try:
        resp = requests.post(
            'https://api.ticketswap.com/graphql/public',
            headers=GQL_HEADERS,
            json={'query': query, 'variables': variables},
            timeout=15
        )
        print(f"  GQL status: {resp.status_code}")
        if not resp.ok:
            return None
        data = resp.json()
        if data.get('errors'):
            print(f"  GQL errors: {data['errors']}")
        return data.get('data')
    except Exception as e:
        print(f"  GQL exception: {e}")
        return None

LISTING_FIELDS = """
fragment LF on PublicListing {
  id
  numberOfTicketsInListing
  uri
  price { totalPriceWithTransactionFee { amount currency } }
}
"""

def fetch_via_graphql(url):
    """Try multiple GraphQL strategies based on URL structure."""
    uuid, numeric_id, full_slug, event_slug = extract_ids_from_url(url)
    print(f"  Parsed → uuid={uuid} numeric_id={numeric_id} event_slug={event_slug}")

    import base64

    # Strategy 1: EventType node by Relay ID (UUID)
    if uuid:
        relay_id = base64.b64encode(f"EventType:{uuid}".encode()).decode()
        data = gql_post(LISTING_FIELDS + """
        query S1($id: ID!) {
          node(id: $id) {
            ... on EventType {
              id title
              listings(first: 20, status: AVAILABLE) {
                edges { node { ...LF } }
              }
            }
          }
        }""", {'id': relay_id})
        if data:
            node = (data.get('node') or {})
            edges = node.get('listings', {}).get('edges', [])
            if edges is not None:
                listings = parse_listings_from_edges(edges, url)
                print(f"  Strategy 1 OK: {len(listings)} listings")
                return node.get('title', 'TicketSwap Event'), listings

    # Strategy 2: eventType by numeric ID
    if numeric_id:
        data = gql_post(LISTING_FIELDS + """
        query S2($id: Int!) {
          eventType(id: $id) {
            id title
            listings(first: 20, status: AVAILABLE) {
              edges { node { ...LF } }
            }
          }
        }""", {'id': int(numeric_id)})
        if data:
            et = data.get('eventType') or {}
            edges = et.get('listings', {}).get('edges', [])
            if edges is not None:
                listings = parse_listings_from_edges(edges, url)
                print(f"  Strategy 2 OK: {len(listings)} listings")
                return et.get('title', 'TicketSwap Event'), listings

    # Strategy 3: event by short slug → all eventTypes
    if event_slug:
        data = gql_post(LISTING_FIELDS + """
        query S3($slug: String!) {
          event(slug: $slug) {
            id title
            eventTypes {
              id title
              listings(first: 20, status: AVAILABLE) {
                edges { node { ...LF } }
              }
            }
          }
        }""", {'slug': event_slug})
        if data:
            ev = data.get('event') or {}
            name = ev.get('title', 'TicketSwap Event')
            all_listings = []
            for et in (ev.get('eventTypes') or []):
                edges = et.get('listings', {}).get('edges', [])
                all_listings.extend(parse_listings_from_edges(edges, url))
            if ev.get('eventTypes') is not None:
                print(f"  Strategy 3 OK: {len(all_listings)} listings")
                return name, all_listings

    return None, None

def fetch_via_next_data(url):
    """Try to extract listings from Next.js __NEXT_DATA__ embedded JSON."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
        if not match:
            return None, None

        data = json.loads(match.group(1))
        props = data.get('props', {}).get('pageProps', {})

        event_data = (
            props.get('event') or
            props.get('eventData') or
            props.get('initialData', {}).get('event') or
            {}
        )

        event_name = event_data.get('title') or event_data.get('name') or 'TicketSwap Event'

        raw_listings = (
            event_data.get('listings', {}).get('edges', []) or
            event_data.get('availableListings', {}).get('edges', []) or
            props.get('listings', {}).get('edges', []) or
            []
        )

        listings = parse_listings_from_edges(raw_listings, url)
        return event_name, listings

    except Exception as e:
        print(f"Next.js extraction error: {e}")
        return None, None

def fetch_event_info(url):
    """Fetch event name and listings, trying multiple methods."""
    name, listings = fetch_via_graphql(url)
    if listings is not None:
        print(f"GraphQL OK: {len(listings)} listings")
        return name, listings

    name, listings = fetch_via_next_data(url)
    if listings is not None:
        print(f"Next.js OK: {len(listings)} listings")
        return name, listings

    raise Exception("Nepodařilo se načíst data z TicketSwap. Zkontroluj URL.")

# ──────────────────────────────────────────────
# SCHEDULER
# ──────────────────────────────────────────────

def check_all_events():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking TicketSwap...")
    with get_db() as conn:
        events = conn.execute('SELECT * FROM events WHERE active=1').fetchall()
        for event in events:
            try:
                _, listings = fetch_event_info(event['url'])
                now = datetime.now().isoformat()
                conn.execute('UPDATE events SET last_checked=? WHERE id=?', (now, event['id']))

                max_price = event['max_price']

                for lst in listings:
                    existing = conn.execute(
                        'SELECT id FROM seen_listings WHERE event_id=? AND listing_id=?',
                        (event['id'], lst['id'])
                    ).fetchone()

                    if existing:
                        continue

                    conn.execute('''
                        INSERT OR IGNORE INTO seen_listings
                        (event_id, listing_id, price, num_tickets, listing_url, found_at)
                        VALUES (?,?,?,?,?,?)
                    ''', (event['id'], lst['id'], lst['price'], lst['num_tickets'], lst['url'], now))

                    if max_price is None or lst['price'] <= max_price:
                        cur = lst.get('currency', 'EUR')
                        msg = (
                            f"🎟 <b>Nový lístek na TicketSwap!</b>\n\n"
                            f"🎪 <b>{event['name']}</b>\n"
                            f"💰 Cena: <b>{lst['price']:.2f} {cur}</b>\n"
                            f"🎫 Počet lístků: {lst['num_tickets']}\n"
                            + (f"🎯 Tvůj limit: {max_price:.2f} {cur}\n" if max_price else "")
                            + f"\n🔗 <a href='{lst['url']}'>Koupit lístek</a>"
                        )
                        send_telegram(msg)
                        conn.execute('''
                            INSERT INTO notifications (event_id, listing_id, price, num_tickets, listing_url, sent_at)
                            VALUES (?,?,?,?,?,?)
                        ''', (event['id'], lst['id'], lst['price'], lst['num_tickets'], lst['url'], now))
                        print(f"  Notified: {event['name']} — {lst['price']} {cur}")

            except Exception as e:
                print(f"  Error checking {event['url']}: {e}")

# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    return 'OK', 200

@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({'error': str(e)}), 500

@app.route('/api/preview', methods=['POST'])
def api_preview():
    data = request.get_json() or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'URL je povinná'}), 400
    try:
        name, listings = fetch_event_info(url)
        cheapest = min(listings, key=lambda x: x['price']) if listings else None
        return jsonify({
            'name': name,
            'total_listings': len(listings),
            'cheapest': cheapest,
            'listings': listings[:5]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/events', methods=['GET'])
def api_get_events():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM events WHERE active=1 ORDER BY created_at DESC').fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['notification_count'] = conn.execute(
                'SELECT COUNT(*) FROM notifications WHERE event_id=?', (r['id'],)
            ).fetchone()[0]
            d['seen_count'] = conn.execute(
                'SELECT COUNT(*) FROM seen_listings WHERE event_id=?', (r['id'],)
            ).fetchone()[0]
            result.append(d)
        return jsonify(result)

@app.route('/api/events', methods=['POST'])
def api_add_event():
    data = request.get_json() or {}
    if not data.get('url') or not data.get('name'):
        return jsonify({'error': 'Chybí url nebo name'}), 400
    with get_db() as conn:
        conn.execute('''
            INSERT INTO events (url, name, max_price, currency, last_checked, created_at)
            VALUES (?,?,?,?,?,?)
        ''', (
            data['url'], data['name'],
            float(data['max_price']) if data.get('max_price') else None,
            data.get('currency', 'EUR'),
            datetime.now().isoformat(),
            datetime.now().isoformat()
        ))
    return jsonify({'ok': True})

@app.route('/api/events/<int:event_id>', methods=['DELETE'])
def api_delete_event(event_id):
    with get_db() as conn:
        conn.execute('UPDATE events SET active=0 WHERE id=?', (event_id,))
    return jsonify({'ok': True})

@app.route('/api/events/<int:event_id>/notifications', methods=['GET'])
def api_event_notifications(event_id):
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM notifications WHERE event_id=? ORDER BY sent_at DESC LIMIT 20',
            (event_id,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])

@app.route('/api/check-now', methods=['POST'])
def api_check_now():
    check_all_events()
    return jsonify({'ok': True})

@app.route('/api/test-telegram', methods=['POST'])
def api_test_telegram():
    ok = send_telegram("🎟 TicketSwap hlídač je aktivní! Test notifikace funguje.")
    if ok:
        return jsonify({'ok': True, 'message': 'Zpráva odeslána na Telegram!'})
    return jsonify({'ok': False, 'message': 'Chyba — zkontroluj TELEGRAM_TOKEN a TELEGRAM_CHAT_ID'}), 500

# ──────────────────────────────────────────────
# START
# ──────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    scheduler = BackgroundScheduler(timezone='Europe/Prague')
    scheduler.add_job(check_all_events, 'interval', minutes=5, id='ticketswap_check')
    scheduler.start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
