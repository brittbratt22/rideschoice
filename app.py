import os, sqlite3, secrets, functools
from datetime import timedelta
import click
from flask import Flask, request, session, jsonify, render_template, g
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None

app = Flask(__name__)
secret = os.environ.get('SECRET_KEY')
if not secret or len(secret) < 32:
    raise RuntimeError('Set SECRET_KEY to a random value of at least 32 characters. See README.')

app.config.update(
    SECRET_KEY=secret,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '1') == '1',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=32768
)

DB_URL = os.environ.get('DATABASE_URL', '').strip()
if DB_URL.startswith('postgres://'):
    DB_URL = 'postgresql://' + DB_URL[len('postgres://'):]
IS_PG = DB_URL.startswith('postgresql://')
DB_PATH = os.environ.get('DATABASE_PATH', 'ridechoice.sqlite3')
LIVE = os.environ.get('ENABLE_BOOKINGS', '0') == '1'
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '').strip().lower()


class DBWrapper:
    def __init__(self, conn, is_pg):
        self.conn = conn
        self.is_pg = is_pg

    def _sql(self, query):
        if not self.is_pg:
            return query
        return query.replace('?', '%s').replace('BEGIN IMMEDIATE', 'BEGIN')

    def execute(self, query, params=()):
        cur = self.conn.cursor()
        cur.execute(self._sql(query), params)
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def db():
    if 'db' not in g:
        if IS_PG:
            if psycopg is None:
                raise RuntimeError('psycopg is required when DATABASE_URL is set.')
            conn = psycopg.connect(DB_URL, row_factory=dict_row)
            g.db = DBWrapper(conn, True)
        else:
            conn = sqlite3.connect(DB_PATH, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            g.db = DBWrapper(conn, False)
    return g.db


@app.teardown_appcontext
def close(error):
    if 'db' in g:
        g.db.close()


@app.cli.command('init-db')
def init_db():
    conn = db()
    if IS_PG:
        statements = [
            '''CREATE TABLE IF NOT EXISTS users (
              id SERIAL PRIMARY KEY,
              name TEXT NOT NULL,
              email TEXT UNIQUE NOT NULL,
              password TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('passenger','driver','admin')),
              vehicle TEXT NOT NULL DEFAULT '',
              approved INTEGER NOT NULL DEFAULT 0,
              available INTEGER NOT NULL DEFAULT 0,
              created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''',
            '''CREATE TABLE IF NOT EXISTS rides (
              id SERIAL PRIMARY KEY,
              passenger_id INTEGER NOT NULL REFERENCES users(id),
              driver_id INTEGER NOT NULL REFERENCES users(id),
              pickup TEXT NOT NULL,
              destination TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'requested',
              created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''',
            '''CREATE UNIQUE INDEX IF NOT EXISTS one_active_passenger ON rides(passenger_id)
              WHERE status IN ('requested','accepted','in_progress')''',
            '''CREATE UNIQUE INDEX IF NOT EXISTS one_active_driver ON rides(driver_id)
              WHERE status IN ('requested','accepted','in_progress')'''
        ]
        for stmt in statements:
            conn.execute(stmt)
    else:
        conn.conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          email TEXT UNIQUE NOT NULL,
          password TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('passenger','driver','admin')),
          vehicle TEXT NOT NULL DEFAULT '',
          approved INTEGER NOT NULL DEFAULT 0,
          available INTEGER NOT NULL DEFAULT 0,
          created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS rides (
          id INTEGER PRIMARY KEY,
          passenger_id INTEGER NOT NULL REFERENCES users(id),
          driver_id INTEGER NOT NULL REFERENCES users(id),
          pickup TEXT NOT NULL,
          destination TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'requested',
          created TEXT DEFAULT CURRENT_TIMESTAMP,
          updated TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_passenger ON rides(passenger_id)
          WHERE status IN ('requested','accepted','in_progress');
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_driver ON rides(driver_id)
          WHERE status IN ('requested','accepted','in_progress');
        ''')
    conn.commit()
    click.echo('Database initialized.')


@app.cli.command('create-admin')
@click.option('--email', prompt=True)
@click.option('--name', prompt=True)
@click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True)
def create_admin(email, name, password):
    if len(password) < 12:
        raise click.ClickException('Use at least 12 characters.')
    integrity_errors = (sqlite3.IntegrityError,) + ((psycopg.IntegrityError,) if psycopg else ())
    try:
        db().execute(
            'INSERT INTO users(name,email,password,role,approved) VALUES(?,?,?,?,1)',
            (name.strip(), email.strip().lower(), generate_password_hash(password), 'admin')
        )
        db().commit()
    except integrity_errors:
        raise click.ClickException('Email already registered.')
    click.echo('Administrator created.')


@app.before_request
def security():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    if request.path.startswith('/api/') and request.method != 'GET':
        if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), session['csrf']):
            return jsonify(error='Session expired. Refresh the page.'), 403


@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; manifest-src 'self'; worker-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def user():
    return db().execute('SELECT * FROM users WHERE id=?', (session.get('uid'),)).fetchone()


def auth(*roles):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            u = user()
            if not u:
                return jsonify(error='Sign in first.'), 401
            if roles and u['role'] not in roles:
                return jsonify(error='Not permitted.'), 403
            return fn(u, *args, **kwargs)
        return wrapped
    return decorator


def public(u):
    return {k: u[k] for k in ('id', 'name', 'role', 'vehicle', 'approved', 'available')}


def data():
    return request.get_json(silent=True) or {}


def text(d, k, limit=200):
    return str(d.get(k, '')).strip()[:limit]


@app.get('/')
def index():
    return render_template('index.html')


@app.get('/healthz')
def healthz():
    return jsonify(ok=True)


@app.get('/sw.js')
def service_worker():
    resp = app.send_static_file('sw.js')
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.get('/manifest.json')
def manifest():
    resp = app.send_static_file('manifest.json')
    resp.headers['Content-Type'] = 'application/manifest+json'
    return resp


@app.get('/api/session')
def state():
    u = user()
    return jsonify(user=public(u) if u else None, csrf=session['csrf'], bookings_enabled=LIVE)


@app.post('/api/register')
def register():
    d = data()
    name = text(d, 'name', 80)
    email = text(d, 'email', 254).lower()
    password = str(d.get('password', ''))
    role = d.get('role')
    vehicle = text(d, 'vehicle', 120)
    if (
        not name or '@' not in email or len(password) < 12 or len(password) > 256
        or role not in ('driver', 'passenger') or (role == 'driver' and not vehicle)
    ):
        return jsonify(error='Supply name, email, a 12-256 character password, and vehicle details for drivers.'), 400
    approved = 0
    if ADMIN_EMAIL and email == ADMIN_EMAIL:
        role = 'admin'
        approved = 1
    integrity_errors = (sqlite3.IntegrityError,) + ((psycopg.IntegrityError,) if psycopg else ())
    try:
        if IS_PG:
            row = db().execute(
                'INSERT INTO users(name,email,password,role,vehicle,approved) VALUES(?,?,?,?,?,?) RETURNING id',
                (name, email, generate_password_hash(password), role, vehicle, approved)
            ).fetchone()
            uid = row['id']
        else:
            c = db().execute(
                'INSERT INTO users(name,email,password,role,vehicle,approved) VALUES(?,?,?,?,?,?)',
                (name, email, generate_password_hash(password), role, vehicle, approved)
            )
            uid = c.lastrowid
        db().commit()
    except integrity_errors:
        db().rollback()
        return jsonify(error='Registration could not be completed. Try signing in.'), 400
    session.clear()
    session['uid'] = uid
    session['csrf'] = secrets.token_urlsafe(32)
    session.permanent = True
    return jsonify(ok=True)


@app.post('/api/login')
def login():
    d = data()
