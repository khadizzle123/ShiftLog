"""
ShiftLog — HVE Traffic Task Force Reporting
Full backend: auth, submissions, leaderboard, supervisor reports, admin.
"""
from flask import Flask, request, send_file, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from datetime import datetime
import io, os, json, functools

from sqlalchemy import (create_engine, Column, Integer, String,
                        DateTime, Text, ForeignKey, func)
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_URL = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(BASE_DIR, 'shiftlog.db'))
if DB_URL.startswith('postgres://'):
    DB_URL = DB_URL.replace('postgres://', 'postgresql://', 1)

engine = create_engine(DB_URL, pool_pre_ping=True,
                       connect_args={} if DB_URL.startswith('postgresql') else {'check_same_thread': False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

CATEGORIES = [
    ('total_contacts',       'Total Contacts'),
    ('seat_belt',            'Seat Belt Citations'),
    ('child_safety_seat',    'Child Safety Seat'),
    ('speeding',             'Speeding Citations'),
    ('texting',              'Texting Citations'),
    ('suspended_licenses',   'Suspended Licenses'),
    ('uninsured_motorists',  'Uninsured Motorists'),
    ('dui_alcohol',          'DUI - Alcohol'),
    ('dui_drugs',            'DUI - Drugs'),
    ('dui_drugs_alcohol',    'DUI - Drugs & Alcohol'),
    ('underage_alcohol',     'Underage Alcohol'),
    ('reckless_driving',     'Reckless Driving'),
    ('inattentive_driving',  'Inattentive Driving'),
    ('felony_arrests',       'Felony Arrests'),
    ('recovered_stolen',     'Recovered Stolen'),
    ('fugitives',            'Fugitives'),
    ('motorcycle_endorsement','Motorcycle'),
    ('bicycle_pedestrian',   'Bicycle/Pedestrian'),
    ('other_activity',       'Other'),
    # Other Contacts
    ('assist_officer',       'Assist Other Officer'),
    ('calls_for_service',    'Calls for Service'),
    ('field_interview',      'Field Interview (FI)'),
]
CAT_KEYS = [k for k, _ in CATEGORIES]

# Keys that count as citations for citations-per-hour
CITATION_KEYS = [
    'seat_belt','child_safety_seat','speeding','texting','suspended_licenses',
    'uninsured_motorists','reckless_driving','inattentive_driving',
    'dui_alcohol','dui_drugs','dui_drugs_alcohol',
]


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    badge = Column(String(40), unique=True, nullable=False)
    department = Column(String(120))
    email = Column(String(160))
    password_hash = Column(String(300), nullable=False)
    role = Column(String(30), default='officer')
    created_at = Column(DateTime, default=datetime.utcnow)


class Submission(Base):
    __tablename__ = 'submissions'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    officer_name = Column(String(120))
    badge = Column(String(40))
    department = Column(String(120))
    shift_date = Column(String(40))
    start_time = Column(String(40))
    end_time = Column(String(40))
    ot_hours = Column(String(20))
    hours_worked = Column(String(20))
    mileage = Column(String(20))
    details = Column(Text)
    data_json = Column(Text)
    source = Column(String(20), default='form')
    created_at = Column(DateTime, default=datetime.utcnow)


class Setting(Base):
    __tablename__ = 'settings'
    key = Column(String(80), primary_key=True)
    value = Column(Text, default='')



class Grant(Base):
    __tablename__ = 'grants'
    id           = Column(Integer, primary_key=True)
    name         = Column(String(200), nullable=False)
    agency       = Column(String(200))
    amount       = Column(String(40))
    start_date   = Column(String(20))
    end_date     = Column(String(20))
    created_at   = Column(DateTime, default=datetime.utcnow)


class GrantOfficer(Base):
    __tablename__ = 'grant_officers'
    id       = Column(Integer, primary_key=True)
    grant_id = Column(Integer, ForeignKey('grants.id'))
    user_id  = Column(Integer, ForeignKey('users.id'))


class OfficerWage(Base):
    __tablename__ = 'officer_wages'
    id      = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True)
    wage    = Column(String(20))


Base.metadata.create_all(engine)


# ── startup migrations ────────────────────────────────────────────────────────
def _run_migrations():
    """Add any missing columns to existing tables."""
    import traceback
    migrations = [
        "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS hours_worked VARCHAR(20)",
        "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS assist_officer INTEGER DEFAULT 0",
        "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS calls_for_service INTEGER DEFAULT 0",
        "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS field_interview INTEGER DEFAULT 0",
    ]
    # SQLite uses different syntax
    if DB_URL.startswith('sqlite'):
        # SQLite doesn't support IF NOT EXISTS on ALTER TABLE
        # check columns first
        with engine.connect() as conn:
            from sqlalchemy import text, inspect
            insp = inspect(engine)
            existing = {c['name'] for c in insp.get_columns('submissions')}
            for col, typ in [('hours_worked','VARCHAR(20)'),('assist_officer','INTEGER'),
                              ('calls_for_service','INTEGER'),('field_interview','INTEGER')]:
                if col not in existing:
                    try:
                        conn.execute(text(f"ALTER TABLE submissions ADD COLUMN {col} {typ}"))
                        conn.commit()
                    except Exception:
                        pass
        return
    with engine.connect() as conn:
        from sqlalchemy import text
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass

try:
    _run_migrations()
except Exception:
    pass


# ── helpers ──────────────────────────────────────────────────────────────────

def get_setting(s, key, default=''):
    row = s.query(Setting).get(key)
    return row.value if row else default


def set_setting(s, key, value):
    row = s.query(Setting).get(key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))
    s.commit()

app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')
app.secret_key = os.environ.get('SECRET_KEY', 'shiftlog-' + os.urandom(8).hex())


def db():
    return SessionLocal()


def current_user(s):
    uid = session.get('uid')
    return s.query(User).get(uid) if uid else None


def login_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        if not session.get('uid'):
            return jsonify({'error': 'auth required'}), 401
        return fn(*a, **k)
    return wrap


def role_required(*roles):
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            s = db()
            try:
                u = current_user(s)
                if not u or u.role not in roles:
                    return jsonify({'error': 'forbidden'}), 403
                return fn(*a, **k)
            finally:
                s.close()
        return wrap
    return deco


def user_dict(u):
    return {'id': u.id, 'name': u.name, 'badge': u.badge,
            'department': u.department, 'email': u.email, 'role': u.role}


@app.route('/api/signup', methods=['POST'])
def signup():
    d = request.json or {}
    s = db()
    try:
        # check if signup is allowed
        if get_setting(s, 'allow_signup', 'true').lower() != 'true':
            return jsonify({'error': 'New account registration is currently disabled. Contact your administrator.'}), 403
        name = (d.get('name') or '').strip()
        badge = (d.get('badge') or '').strip()
        pw = d.get('password') or ''
        if not name or not badge or not pw:
            return jsonify({'error': 'Name, badge, and password are required.'}), 400
        if get_setting(s, 'require_email', 'false').lower() == 'true' and not (d.get('email') or '').strip():
            return jsonify({'error': 'An email address is required to register.'}), 400
        if s.query(User).filter_by(badge=badge).first():
            return jsonify({'error': 'That badge number is already registered.'}), 409
        is_first = s.query(User).count() == 0
        requested = d.get('role') or 'officer'
        role = 'admin' if is_first else ('pending_supervisor' if requested == 'supervisor' else 'officer')
        u = User(name=name, badge=badge, department=(d.get('department') or '').strip(),
                 email=(d.get('email') or '').strip(),
                 password_hash=generate_password_hash(pw), role=role)
        s.add(u); s.commit()
        session['uid'] = u.id
        return jsonify({'user': user_dict(u), 'is_first': is_first})
    finally:
        s.close()


@app.route('/api/login', methods=['POST'])
def login():
    d = request.json or {}
    s = db()
    try:
        u = s.query(User).filter_by(badge=(d.get('badge') or '').strip()).first()
        if not u or not check_password_hash(u.password_hash, d.get('password') or ''):
            return jsonify({'error': 'Invalid badge number or password.'}), 401
        session['uid'] = u.id
        return jsonify({'user': user_dict(u)})
    finally:
        s.close()


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/me')
def me():
    s = db()
    try:
        u = current_user(s)
        return jsonify({'user': user_dict(u) if u else None})
    finally:
        s.close()


@app.route('/api/submit', methods=['POST'])
@login_required
def submit():
    import traceback
    d = request.json or {}
    s = db()
    try:
        u = current_user(s)
        cats = {k: int(d.get(k) or 0) for k in CAT_KEYS}
        sub = Submission(
            user_id=u.id, officer_name=u.name, badge=u.badge, department=u.department,
            shift_date=d.get('date') or '', start_time=d.get('start_time') or '',
            end_time=d.get('end_time') or '', ot_hours=str(d.get('ot_hours') or ''),
            hours_worked=str(d.get('hours_worked') or ''),
            mileage=str(d.get('mileage') or ''), details=d.get('details') or '',
            data_json=json.dumps(cats), source='form')
        s.add(sub); s.commit()
        return jsonify({'ok': True, 'submission_id': sub.id})
    except Exception as e:
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500
    finally:
        s.close()


@app.route('/api/leaderboard')
def leaderboard():
    s = db()
    try:
        today = datetime.utcnow().date()
        subs = s.query(Submission).filter(func.date(Submission.created_at) == today).all()
        tally = {}
        for sub in subs:
            try:
                c = json.loads(sub.data_json).get('total_contacts', 0)
            except Exception:
                c = 0
            key = (sub.officer_name, sub.badge)
            tally[key] = tally.get(key, 0) + c
        ranked = sorted(tally.items(), key=lambda x: x[1], reverse=True)[:3]
        return jsonify({'leaders': [{'name': n, 'badge': b, 'contacts': c} for (n, b), c in ranked]})
    finally:
        s.close()


@app.route('/api/officers')
@role_required('supervisor', 'admin')
def officers():
    s = db()
    try:
        names = [r[0] for r in s.query(Submission.officer_name).distinct().all()]
        return jsonify({'officers': sorted(set(filter(None, names)))})
    finally:
        s.close()


def _apply_date_filter(q):
    """Filter a Submission query by ?start=YYYY-MM-DD&end=YYYY-MM-DD on created_at."""
    start = request.args.get('start')
    end = request.args.get('end')
    if start:
        try:
            q = q.filter(Submission.created_at >= datetime.strptime(start, '%Y-%m-%d'))
        except Exception:
            pass
    if end:
        try:
            ed = datetime.strptime(end, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
            q = q.filter(Submission.created_at <= ed)
        except Exception:
            pass
    return q


@app.route('/api/performance')
@role_required('supervisor', 'admin')
def performance():
    officer = request.args.get('officer', 'all')
    s = db()
    try:
        q = s.query(Submission)
        if officer != 'all':
            q = q.filter(Submission.officer_name == officer)
        q = _apply_date_filter(q)
        subs = q.order_by(Submission.created_at.desc()).all()
        rows, totals = [], {k: 0 for k in CAT_KEYS}
        officers_seen = set()
        for sub in subs:
            try:
                cats = json.loads(sub.data_json)
            except Exception:
                cats = {}
            for k in CAT_KEYS:
                totals[k] += int(cats.get(k, 0) or 0)
            if sub.officer_name:
                officers_seen.add(sub.officer_name)
            rows.append({'date': sub.shift_date or sub.created_at.strftime('%m/%d/%Y'),
                         'officer': sub.officer_name, 'badge': sub.badge, **cats})
        return jsonify({'rows': rows, 'totals': totals, 'count': len(rows),
                        'officer_count': len(officers_seen)})
    finally:
        s.close()


@app.route('/api/admin/users')
@role_required('admin')
def admin_users():
    s = db()
    try:
        return jsonify({'users': [user_dict(u) for u in s.query(User).order_by(User.created_at).all()]})
    finally:
        s.close()


@app.route('/api/admin/set_role', methods=['POST'])
@role_required('admin')
def admin_set_role():
    d = request.json or {}
    s = db()
    try:
        u = s.query(User).get(d.get('user_id'))
        if not u:
            return jsonify({'error': 'not found'}), 404
        u.role = d.get('role', 'officer'); s.commit()
        return jsonify({'ok': True, 'user': user_dict(u)})
    finally:
        s.close()


@app.route('/api/admin/delete_user', methods=['POST'])
@role_required('admin')
def admin_delete_user():
    d = request.json or {}
    s = db()
    try:
        u = s.query(User).get(d.get('user_id'))
        if u and u.role != 'admin':
            s.delete(u); s.commit()
        return jsonify({'ok': True})
    finally:
        s.close()


@app.route('/api/admin/add_user', methods=['POST'])
@role_required('admin')
def admin_add_user():
    d = request.json or {}
    name = (d.get('name') or '').strip()
    badge = (d.get('badge') or '').strip()
    pw = (d.get('password') or '').strip()
    if not name or not badge or not pw:
        return jsonify({'error': 'Name, badge, and password are required.'}), 400
    s = db()
    try:
        if s.query(User).filter_by(badge=badge).first():
            return jsonify({'error': 'Badge number already in use.'}), 409
        u = User(name=name, badge=badge,
                 department=(d.get('department') or '').strip(),
                 email=(d.get('email') or '').strip(),
                 password_hash=generate_password_hash(pw),
                 role=d.get('role') or 'officer')
        s.add(u); s.commit()
        return jsonify({'ok': True, 'user': user_dict(u)})
    finally:
        s.close()


@app.route('/api/admin/edit_user', methods=['POST'])
@role_required('admin')
def admin_edit_user():
    d = request.json or {}
    s = db()
    try:
        u = s.query(User).get(d.get('user_id'))
        if not u:
            return jsonify({'error': 'User not found'}), 404
        if 'name' in d:       u.name = (d['name'] or '').strip() or u.name
        if 'badge' in d:
            new_badge = (d['badge'] or '').strip()
            if new_badge and new_badge != u.badge:
                if s.query(User).filter_by(badge=new_badge).first():
                    return jsonify({'error': 'Badge number already in use.'}), 409
                u.badge = new_badge
        if 'department' in d: u.department = (d['department'] or '').strip()
        if 'email' in d:      u.email = (d['email'] or '').strip()
        if 'role' in d and not (u.role == 'admin' and d['role'] != 'admin'):
            u.role = d['role']
        if d.get('password'):
            u.password_hash = generate_password_hash(d['password'])
        s.commit()
        return jsonify({'ok': True, 'user': user_dict(u)})
    finally:
        s.close()


# ── Officer self-service ──────────────────────────────────────────────────────

@app.route('/api/profile', methods=['POST'])
@login_required
def update_profile():
    d = request.json or {}
    s = db()
    try:
        u = current_user(s)
        if not u:
            return jsonify({'error': 'not found'}), 404
        if d.get('name', '').strip():
            u.name = d['name'].strip()
        if 'department' in d:
            u.department = d['department'].strip()
        if 'email' in d:
            u.email = d['email'].strip()
        if d.get('new_password'):
            if not check_password_hash(u.password_hash, d.get('current_password', '')):
                return jsonify({'error': 'Current password is incorrect.'}), 400
            u.password_hash = generate_password_hash(d['new_password'])
        s.commit()
        return jsonify({'ok': True, 'user': user_dict(u)})
    finally:
        s.close()


@app.route('/api/my_submissions')
@login_required
def my_submissions():
    s = db()
    try:
        u = current_user(s)
        subs = (s.query(Submission)
                .filter(Submission.user_id == u.id)
                .order_by(Submission.created_at.desc())
                .all())
        out = []
        for sub in subs:
            try: cats = json.loads(sub.data_json)
            except Exception: cats = {}
            out.append({
                'id': sub.id,
                'date': sub.shift_date or sub.created_at.strftime('%m/%d/%Y'),
                'start_time': sub.start_time, 'end_time': sub.end_time,
                'hours_worked': sub.hours_worked,
                'ot_hours': sub.ot_hours, 'mileage': sub.mileage,
                'details': sub.details, 'cats': cats,
                'created': sub.created_at.strftime('%m/%d/%Y %I:%M %p'),
            })
        return jsonify({'submissions': out})
    finally:
        s.close()


@app.route('/api/check_duplicate')
@login_required
def check_duplicate():
    date = request.args.get('date', '')
    s = db()
    try:
        u = current_user(s)
        exists = s.query(Submission).filter(
            Submission.user_id == u.id,
            Submission.shift_date == date
        ).first()
        return jsonify({'duplicate': bool(exists)})
    finally:
        s.close()


# ── Announcements ─────────────────────────────────────────────────────────────

@app.route('/api/announcement')
def get_announcement():
    s = db()
    try:
        return jsonify({
            'text': get_setting(s, 'announcement_text', ''),
            'active': get_setting(s, 'announcement_active', 'false'),
        })
    finally:
        s.close()


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route('/api/admin/categories')
@role_required('admin', 'supervisor')
def admin_categories():
    return jsonify({'categories': [{'key': k, 'label': l} for k, l in CATEGORIES]})


@app.route('/api/admin/submissions')
@role_required('admin')
def admin_submissions():
    officer = request.args.get('officer', 'all')
    s = db()
    try:
        q = s.query(Submission)
        if officer != 'all':
            q = q.filter(Submission.officer_name == officer)
        subs = q.order_by(Submission.created_at.desc()).all()
        out = []
        for sub in subs:
            try: cats = json.loads(sub.data_json)
            except Exception: cats = {}
            out.append({'id': sub.id, 'officer': sub.officer_name, 'badge': sub.badge,
                        'department': sub.department,
                        'date': sub.shift_date or sub.created_at.strftime('%m/%d/%Y'),
                        'start_time': sub.start_time, 'end_time': sub.end_time,
                        'ot_hours': sub.ot_hours, 'mileage': sub.mileage,
                        'details': sub.details,
                        'cats': cats,
                        'created': sub.created_at.strftime('%m/%d/%Y %I:%M %p')})
        return jsonify({'submissions': out})
    finally:
        s.close()


@app.route('/api/admin/update_submission', methods=['POST'])
@role_required('admin')
def admin_update_submission():
    d = request.json or {}
    s = db()
    try:
        sub = s.query(Submission).get(d.get('id'))
        if not sub:
            return jsonify({'error': 'not found'}), 404
        if 'cats' in d:
            sub.data_json = json.dumps({k: int(d['cats'].get(k, 0) or 0) for k in CAT_KEYS})
        if 'shift_date' in d:  sub.shift_date  = d['shift_date']
        if 'ot_hours'   in d:  sub.ot_hours    = d['ot_hours']
        if 'hours_worked' in d: sub.hours_worked = d['hours_worked']
        if 'mileage'    in d:  sub.mileage     = d['mileage']
        if 'details'    in d:  sub.details     = d['details']
        if 'start_time' in d:  sub.start_time  = d['start_time']
        if 'end_time'   in d:  sub.end_time    = d['end_time']
        s.commit()
        return jsonify({'ok': True})
    finally:
        s.close()


@app.route('/api/admin/delete_submission', methods=['POST'])
@role_required('admin')
def admin_delete_submission():
    d = request.json or {}
    s = db()
    try:
        sub = s.query(Submission).get(d.get('id'))
        if sub:
            s.delete(sub); s.commit()
        return jsonify({'ok': True})
    finally:
        s.close()


@app.route('/api/admin/add_submission', methods=['POST'])
@role_required('admin')
def admin_add_submission():
    d = request.json or {}
    s = db()
    try:
        # look up user by name or badge to link user_id
        officer_name = (d.get('officer_name') or '').strip()
        badge = (d.get('badge') or '').strip()
        u = None
        if badge:
            u = s.query(User).filter_by(badge=badge).first()
        if not u and officer_name:
            u = s.query(User).filter_by(name=officer_name).first()
        cats = {k: int(d.get(k, 0) or 0) for k in CAT_KEYS}
        sub = Submission(
            user_id=u.id if u else None,
            officer_name=officer_name or (u.name if u else ''),
            badge=badge or (u.badge if u else ''),
            department=d.get('department') or (u.department if u else ''),
            shift_date=d.get('shift_date') or '',
            start_time=d.get('start_time') or '',
            end_time=d.get('end_time') or '',
            ot_hours=str(d.get('ot_hours') or ''),
            hours_worked=str(d.get('hours_worked') or ''),
            mileage=str(d.get('mileage') or ''),
            details=d.get('details') or '',
            data_json=json.dumps(cats),
            source='admin')
        s.add(sub); s.commit()
        return jsonify({'ok': True, 'id': sub.id})
    finally:
        s.close()


# ── App Settings ──────────────────────────────────────────────────────────────

SETTING_KEYS = ['app_name', 'department_name', 'default_department',
                'allow_signup', 'maintenance_mode', 'maintenance_message',
                'leaderboard_visible', 'require_email',
                'announcement_active', 'announcement_text']
SETTING_DEFAULTS = {
    'app_name': 'ShiftLog',
    'department_name': 'Blackfoot PD',
    'default_department': 'Blackfoot PD',
    'allow_signup': 'true',
    'maintenance_mode': 'false',
    'maintenance_message': 'ShiftLog is currently down for maintenance. Please check back soon.',
    'leaderboard_visible': 'true',
    'require_email': 'false',
    'announcement_active': 'false',
    'announcement_text': '',
}


@app.route('/api/settings')
def public_settings():
    """Public settings the frontend needs on load (no auth required)."""
    s = db()
    try:
        keys = ['app_name', 'department_name', 'default_department',
                'allow_signup', 'maintenance_mode', 'maintenance_message',
                'leaderboard_visible', 'announcement_active', 'announcement_text']
        return jsonify({k: get_setting(s, k, SETTING_DEFAULTS.get(k, '')) for k in keys})
    finally:
        s.close()


@app.route('/api/admin/settings', methods=['GET'])
@role_required('admin')
def admin_get_settings():
    s = db()
    try:
        return jsonify({k: get_setting(s, k, SETTING_DEFAULTS.get(k, '')) for k in SETTING_KEYS})
    finally:
        s.close()


@app.route('/api/admin/settings', methods=['POST'])
@role_required('admin')
def admin_save_settings():
    d = request.json or {}
    s = db()
    try:
        for k in SETTING_KEYS:
            if k in d:
                set_setting(s, k, str(d[k]))
        return jsonify({'ok': True})
    finally:
        s.close()


# ── Time parsing helper ───────────────────────────────────────────────────────

def _parse_hours(start_str, end_str):
    """Parse start/end time strings into decimal hours. Returns 0 on failure."""
    import re
    def to_minutes(s):
        s = (s or '').strip().upper()
        # try H:MM AM/PM
        m = re.match(r'^(\d{1,2}):(\d{2})\s*(AM|PM)?$', s)
        if m:
            h, mi, period = int(m.group(1)), int(m.group(2)), m.group(3)
            if period == 'PM' and h != 12: h += 12
            if period == 'AM' and h == 12: h = 0
            return h * 60 + mi
        # try H AM/PM
        m = re.match(r'^(\d{1,2})\s*(AM|PM)$', s)
        if m:
            h, period = int(m.group(1)), m.group(2)
            if period == 'PM' and h != 12: h += 12
            if period == 'AM' and h == 12: h = 0
            return h * 60
        return None
    s_min = to_minutes(start_str)
    e_min = to_minutes(end_str)
    if s_min is None or e_min is None:
        return 0.0
    diff = e_min - s_min
    if diff <= 0:
        diff += 24 * 60   # overnight shift
    return round(diff / 60, 2)


# ── Officer Wages ─────────────────────────────────────────────────────────────

@app.route('/api/wages')
@role_required('supervisor', 'admin')
def get_wages():
    s = db()
    try:
        users = s.query(User).filter(User.role.in_(['officer','supervisor','admin'])).all()
        wages = {w.user_id: w.wage for w in s.query(OfficerWage).all()}
        out = []
        for u in users:
            out.append({'user_id': u.id, 'name': u.name, 'badge': u.badge,
                        'department': u.department, 'role': u.role,
                        'wage': wages.get(u.id, '')})
        return jsonify({'wages': out})
    finally:
        s.close()


@app.route('/api/wages', methods=['POST'])
@role_required('supervisor', 'admin')
def set_wage():
    d = request.json or {}
    s = db()
    try:
        user_id = d.get('user_id')
        wage_val = str(d.get('wage') or '').strip()
        row = s.query(OfficerWage).filter_by(user_id=user_id).first()
        if row:
            row.wage = wage_val
        else:
            s.add(OfficerWage(user_id=user_id, wage=wage_val))
        s.commit()
        return jsonify({'ok': True})
    finally:
        s.close()


# ── Grants ────────────────────────────────────────────────────────────────────

def _grant_dict(g, officers=None):
    return {'id': g.id, 'name': g.name, 'agency': g.agency or '',
            'amount': g.amount or '0', 'start_date': g.start_date or '',
            'end_date': g.end_date or '',
            'created': g.created_at.strftime('%m/%d/%Y') if g.created_at else '',
            'officer_ids': officers or []}


@app.route('/api/grants')
@role_required('supervisor', 'admin')
def list_grants():
    s = db()
    try:
        grants = s.query(Grant).order_by(Grant.created_at.desc()).all()
        out = []
        for g in grants:
            oids = [go.user_id for go in s.query(GrantOfficer).filter_by(grant_id=g.id).all()]
            out.append(_grant_dict(g, oids))
        return jsonify({'grants': out})
    finally:
        s.close()


@app.route('/api/grants', methods=['POST'])
@role_required('supervisor', 'admin')
def create_grant():
    d = request.json or {}
    s = db()
    try:
        g = Grant(name=(d.get('name') or '').strip(),
                  agency=(d.get('agency') or '').strip(),
                  amount=str(d.get('amount') or '0'),
                  start_date=d.get('start_date') or '',
                  end_date=d.get('end_date') or '')
        s.add(g); s.flush()
        for uid in (d.get('officer_ids') or []):
            s.add(GrantOfficer(grant_id=g.id, user_id=uid))
        s.commit()
        oids = [go.user_id for go in s.query(GrantOfficer).filter_by(grant_id=g.id).all()]
        return jsonify({'ok': True, 'grant': _grant_dict(g, oids)})
    finally:
        s.close()


@app.route('/api/grants/<int:gid>', methods=['POST'])
@role_required('supervisor', 'admin')
def update_grant(gid):
    d = request.json or {}
    s = db()
    try:
        g = s.query(Grant).get(gid)
        if not g: return jsonify({'error': 'not found'}), 404
        if 'name'       in d: g.name       = d['name']
        if 'agency'     in d: g.agency     = d['agency']
        if 'amount'     in d: g.amount     = str(d['amount'])
        if 'start_date' in d: g.start_date = d['start_date']
        if 'end_date'   in d: g.end_date   = d['end_date']
        if 'officer_ids' in d:
            s.query(GrantOfficer).filter_by(grant_id=gid).delete()
            for uid in d['officer_ids']:
                s.add(GrantOfficer(grant_id=gid, user_id=uid))
        s.commit()
        oids = [go.user_id for go in s.query(GrantOfficer).filter_by(grant_id=gid).all()]
        return jsonify({'ok': True, 'grant': _grant_dict(g, oids)})
    finally:
        s.close()


@app.route('/api/grants/<int:gid>', methods=['DELETE'])
@role_required('supervisor', 'admin')
def delete_grant(gid):
    s = db()
    try:
        s.query(GrantOfficer).filter_by(grant_id=gid).delete()
        g = s.query(Grant).get(gid)
        if g: s.delete(g)
        s.commit()
        return jsonify({'ok': True})
    finally:
        s.close()


@app.route('/api/grants/<int:gid>/stats')
@role_required('supervisor', 'admin')
def grant_stats(gid):
    import traceback
    s = db()
    try:
        g = s.query(Grant).get(gid)
        if not g: return jsonify({'error': 'not found'}), 404
        officer_ids = [go.user_id for go in s.query(GrantOfficer).filter_by(grant_id=gid).all()]

        wages_map = {}
        users_map = {}
        subs = []
        if officer_ids:
            wages_map = {w.user_id: float(w.wage or 0) for w in
                         s.query(OfficerWage).filter(OfficerWage.user_id.in_(officer_ids)).all()}
            users_map = {u.id: u for u in s.query(User).filter(User.id.in_(officer_ids)).all()}
            subs = s.query(Submission).filter(Submission.user_id.in_(officer_ids)).all()

        def in_range(sub):
            sd = sub.shift_date or ''
            if not sd: return False
            try:    dt = datetime.strptime(sd, '%Y-%m-%d')
            except Exception:
                try: dt = datetime.strptime(sd, '%m/%d/%Y')
                except Exception: return False
            s_dt = datetime.strptime(g.start_date, '%Y-%m-%d') if g.start_date else None
            e_dt = datetime.strptime(g.end_date, '%Y-%m-%d').replace(hour=23, minute=59) if g.end_date else None
            if s_dt and dt < s_dt: return False
            if e_dt and dt > e_dt: return False
            return True

        per_officer = {}
        for uid in officer_ids:
            u = users_map.get(uid)
            if not u: continue
            per_officer[uid] = {
                'name': u.name, 'badge': u.badge, 'wage': wages_map.get(uid, 0),
                'reg_hours': 0.0, 'ot_hours': 0.0, 'reports': 0, 'citations': 0,
                'cats': {k: 0 for k in CAT_KEYS}
            }

        for sub in subs:
            if not in_range(sub): continue
            uid = sub.user_id
            if uid not in per_officer: continue
            po = per_officer[uid]
            po['reports'] += 1
            hw = getattr(sub, 'hours_worked', None)
            reg = float(hw or 0) if (hw and str(hw).strip()) else _parse_hours(sub.start_time, sub.end_time)
            ot  = float(sub.ot_hours or 0)
            po['reg_hours'] += reg
            po['ot_hours']  += ot
            try: cats = json.loads(sub.data_json)
            except Exception: cats = {}
            for k in CAT_KEYS:
                po['cats'][k] += int(cats.get(k, 0) or 0)
            po['citations'] += sum(int(cats.get(k, 0) or 0) for k in CITATION_KEYS)

        total_cost = 0.0
        officers_out = []
        for uid, po in per_officer.items():
            wage = po['wage']
            total_hours   = po['reg_hours'] + po['ot_hours']
            reg_pay       = wage * po['reg_hours']
            ot_pay        = wage * 1.5 * po['ot_hours']
            benefits      = wage * 0.05 * total_hours
            officer_total = reg_pay + ot_pay + benefits
            total_cost   += officer_total
            cph = round(po['citations'] / total_hours, 2) if total_hours > 0 else 0
            officers_out.append({
                'user_id': uid, 'name': po['name'], 'badge': po['badge'],
                'wage': wage, 'reg_hours': round(po['reg_hours'], 2),
                'ot_hours': round(po['ot_hours'], 2),
                'total_hours': round(total_hours, 2),
                'reports': po['reports'], 'citations': po['citations'],
                'citations_per_hour': cph,
                'reg_pay': round(reg_pay, 2), 'ot_pay': round(ot_pay, 2),
                'benefits': round(benefits, 2), 'total': round(officer_total, 2),
                'cats': po['cats'],
            })

        grant_amount   = float(g.amount or 0)
        remaining      = grant_amount - total_cost
        pct_used       = round(total_cost / grant_amount * 100, 1) if grant_amount > 0 else 0
        total_reg_pay  = sum(o['reg_pay']      for o in officers_out)
        total_ot_pay   = sum(o['ot_pay']       for o in officers_out)
        total_benefits = sum(o['benefits']     for o in officers_out)
        total_hours_s  = sum(o['total_hours']  for o in officers_out)
        total_reports  = sum(o['reports']      for o in officers_out)
        total_citations= sum(o['citations']    for o in officers_out)
        total_cph      = round(total_citations / total_hours_s, 2) if total_hours_s > 0 else 0
        total_contacts = sum(o['cats'].get('total_contacts', 0) for o in officers_out)

        return jsonify({
            'grant': _grant_dict(g, list(per_officer.keys())),
            'officers': officers_out,
            'summary': {
                'total_cost': round(total_cost, 2),
                'grant_amount': grant_amount,
                'remaining': round(remaining, 2),
                'pct_used': pct_used,
                'reg_pay': round(total_reg_pay, 2),
                'ot_pay': round(total_ot_pay, 2),
                'benefits': round(total_benefits, 2),
                'total_hours': round(total_hours_s, 2),
                'total_reports': total_reports,
                'total_citations': total_citations,
                'citations_per_hour': total_cph,
                'total_contacts': total_contacts,
            }
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
    finally:
        s.close()


@app.route('/api/grants/<int:gid>/pdf')
@role_required('supervisor', 'admin')
def grant_pdf(gid):
    """Generate the professional grant expenditure PDF using ReportLab."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.units import inch
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                     TableStyle, HRFlowable)
    from reportlab.lib.styles import getSampleStyleSheet

    # ── Fetch stats inline ──
    s_db = db()
    try:
        g_obj = s_db.query(Grant).get(gid)
        if not g_obj:
            return jsonify({'error': 'not found'}), 404
        officer_ids = [go.user_id for go in s_db.query(GrantOfficer).filter_by(grant_id=gid).all()]
        wages_map = {}
        users_map = {}
        subs_all  = []
        if officer_ids:
            wages_map = {w.user_id: float(w.wage or 0) for w in
                         s_db.query(OfficerWage).filter(OfficerWage.user_id.in_(officer_ids)).all()}
            users_map = {u.id: u for u in s_db.query(User).filter(User.id.in_(officer_ids)).all()}
            subs_all  = s_db.query(Submission).filter(Submission.user_id.in_(officer_ids)).all()
        dept_name = get_setting(s_db, 'department_name', 'Blackfoot Police Department')
    finally:
        s_db.close()

    def in_range(sub):
        sd = sub.shift_date or ''
        if not sd: return False
        try:    dt = datetime.strptime(sd, '%Y-%m-%d')
        except Exception:
            try:    dt = datetime.strptime(sd, '%m/%d/%Y')
            except Exception: return False
        s_dt = datetime.strptime(g_obj.start_date, '%Y-%m-%d') if g_obj.start_date else None
        e_dt = datetime.strptime(g_obj.end_date, '%Y-%m-%d').replace(hour=23, minute=59) if g_obj.end_date else None
        if s_dt and dt < s_dt: return False
        if e_dt and dt > e_dt: return False
        return True

    per_officer = {}
    for uid in officer_ids:
        u = users_map.get(uid)
        if not u: continue
        per_officer[uid] = {'name': u.name, 'badge': u.badge, 'wage': wages_map.get(uid, 0),
                             'reg_hours': 0.0, 'ot_hours': 0.0, 'reports': 0,
                             'citations': 0, 'cats': {k: 0 for k in CAT_KEYS}}
    for sub in subs_all:
        if not in_range(sub): continue
        uid = sub.user_id
        if uid not in per_officer: continue
        po = per_officer[uid]
        po['reports'] += 1
        hw = getattr(sub, "hours_worked", None)
        po["reg_hours"] += float(hw or 0) if (hw and str(hw).strip()) else _parse_hours(sub.start_time, sub.end_time)
        po['ot_hours']  += float(sub.ot_hours or 0)
        try: cats = json.loads(sub.data_json)
        except Exception: cats = {}
        for k in CAT_KEYS: po['cats'][k] += int(cats.get(k, 0) or 0)
        po['citations'] += sum(int(cats.get(k, 0) or 0) for k in CITATION_KEYS)

    officers_out = []
    total_cost = 0.0
    for uid, po in per_officer.items():
        wage = po['wage']
        th   = po['reg_hours'] + po['ot_hours']
        rp   = wage * po['reg_hours']
        op   = wage * 1.5 * po['ot_hours']
        bp   = wage * 0.05 * th
        tot  = rp + op + bp
        total_cost += tot
        cph = round(po['citations'] / th, 2) if th > 0 else 0
        officers_out.append({'name': po['name'], 'badge': po['badge'], 'wage': wage,
                              'reg_hours': round(po['reg_hours'], 2), 'ot_hours': round(po['ot_hours'], 2),
                              'total_hours': round(th, 2), 'reports': po['reports'],
                              'citations': po['citations'], 'citations_per_hour': cph,
                              'reg_pay': round(rp, 2), 'ot_pay': round(op, 2),
                              'benefits': round(bp, 2), 'total': round(tot, 2), 'cats': po['cats']})

    grant_amount = float(g_obj.amount or 0)
    remaining    = grant_amount - total_cost
    pct_used     = round(total_cost / grant_amount * 100, 1) if grant_amount > 0 else 0
    total_hours  = sum(o['total_hours']  for o in officers_out)
    total_reports= sum(o['reports']      for o in officers_out)
    total_cit    = sum(o['citations']    for o in officers_out)
    total_cph    = round(total_cit / total_hours, 2) if total_hours > 0 else 0
    total_contacts = sum(o['cats'].get('total_contacts', 0) for o in officers_out)
    total_dui    = sum(sum(o['cats'].get(k, 0) for k in ['dui_alcohol','dui_drugs','dui_drugs_alcohol'])
                       for o in officers_out)

    sort_by = request.args.get('sort', 'name')
    if sort_by == 'cost':   officers_out.sort(key=lambda x: x['total'], reverse=True)
    elif sort_by == 'hours': officers_out.sort(key=lambda x: x['total_hours'], reverse=True)
    else:                    officers_out.sort(key=lambda x: x['name'])

    # ── Build PDF ──
    out_buf = io.BytesIO()
    doc = SimpleDocTemplate(out_buf, pagesize=letter,
                            leftMargin=0.65*inch, rightMargin=0.65*inch,
                            topMargin=0.5*inch, bottomMargin=0.6*inch)
    styles   = getSampleStyleSheet()
    navy     = rl_colors.HexColor('#1e3a5f')
    dgray    = rl_colors.HexColor('#374151')
    lgray    = rl_colors.HexColor('#f3f4f6')
    mgray    = rl_colors.HexColor('#9ca3af')
    gold_c   = rl_colors.HexColor('#b45309')
    white_c  = rl_colors.white
    fmt      = lambda v: f"${float(v):,.2f}"
    fmth     = lambda v: f"{float(v):.1f}h"

    story = []

    # Header
    hdr = [[
        Paragraph(f"<font size=7 color='#9ca3af'>REPORT NO.</font><br/><font size=11><b><font color='#1e3a5f'>GR-{gid:04d}</font></b></font>", styles['Normal']),
        Paragraph(f"<para align=center><font size=7 color='#9ca3af'>— {dept_name.upper()} —</font><br/><font size=17><b>{g_obj.name}</b></font><br/><font size=8 color='#4b5563'>Traffic Safety Grant Expenditure Report</font></para>", styles['Normal']),
        Paragraph(f"<para align=right><font size=7 color='#9ca3af'>GENERATED</font><br/><font size=10><b><font color='#1e3a5f'>{datetime.now().strftime('%B %d, %Y')}</font></b></font></para>", styles['Normal']),
    ]]
    ht = Table(hdr, colWidths=[1.1*inch, 4.8*inch, 1.3*inch])
    ht.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
    story.append(ht)
    story.append(HRFlowable(width='100%', thickness=3, color=navy, spaceAfter=6))

    # Info bar
    status_str = 'Active' if (not g_obj.end_date or g_obj.end_date >= datetime.now().strftime('%Y-%m-%d')) else 'Closed'
    def ic(label, value):
        return Paragraph(f"<font size=7 color='#9ca3af'>{label.upper()}</font><br/><font size=10><b>{value}</b></font>", styles['Normal'])
    info = [[ic('Issuing Agency', g_obj.agency or '—'),
             ic('Grant Period', f"{g_obj.start_date or '—'} – {g_obj.end_date or '—'}"),
             ic('Total Issued', fmt(grant_amount)),
             ic('Officers', str(len(officers_out))),
             ic('Status', status_str)]]
    it = Table(info, colWidths=[1.5*inch, 1.8*inch, 1.2*inch, 0.7*inch, 1.0*inch])
    it.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),lgray),('BOX',(0,0),(-1,-1),.5,mgray),
                             ('INNERGRID',(0,0),(-1,-1),.5,rl_colors.HexColor('#e5e7eb')),
                             ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),8),
                             ('BOTTOMPADDING',(0,0),(-1,-1),8),('LEFTPADDING',(0,0),(-1,-1),10)]))
    story.append(it); story.append(Spacer(1,10))

    # Financial Summary
    story.append(Paragraph('<b><font size=10 color="#1e3a5f">FINANCIAL SUMMARY</font></b>', styles['Normal']))
    story.append(HRFlowable(width='100%', thickness=1.5, color=navy, spaceBefore=3, spaceAfter=6))

    def p8(t): return Paragraph(f'<font size=8>{t}</font>', styles['Normal'])
    def p8g(t): return Paragraph(f'<font size=8 color="#6b7280">{t}</font>', styles['Normal'])
    def p8b(t): return Paragraph(f'<font size=8><b>{t}</b></font>', styles['Normal'])
    def pw(t):  return Paragraph(f'<font size=8 color="white"><b>{t}</b></font>', styles['Normal'])
    def sh(t):  return [Paragraph(f'<font size=8 color="#374151"><b>{t}</b></font>', styles['Normal']),'','','','']

    fin_rows = [[pw('Cost Category'), pw('Description'), pw('Hours'), pw('Rate'), pw('Amount')]]
    fin_rows.append(sh('Regular Compensation'))
    for o in officers_out:
        fin_rows.append([p8(f'Regular Pay — {o["name"]}'), p8g(f'{fmth(o["reg_hours"])} × ${o["wage"]:.2f}/hr'),
                         p8(fmth(o["reg_hours"])), p8(f'${o["wage"]:.2f}'), p8(fmt(o["reg_pay"]))])
    fin_rows.append(['','',p8g(''),p8b('Subtotal'),p8b(fmt(sum(o["reg_pay"] for o in officers_out)))])
    fin_rows.append(sh('Overtime Compensation (1.5×)'))
    for o in officers_out:
        fin_rows.append([p8(f'Overtime Pay — {o["name"]}'), p8g(f'{fmth(o["ot_hours"])} × ${o["wage"]:.2f} × 1.5'),
                         p8(fmth(o["ot_hours"])), p8(f'${o["wage"]*1.5:.2f}'), p8(fmt(o["ot_pay"]))])
    fin_rows.append(['','','',p8b('Subtotal'),p8b(fmt(sum(o["ot_pay"] for o in officers_out)))])
    fin_rows.append(sh('Benefits (5% of Total Hours × Base Wage)'))
    for o in officers_out:
        fin_rows.append([p8(f'Benefits — {o["name"]}'), p8g(f'{fmth(o["total_hours"])} × ${o["wage"]:.2f} × 0.05'),
                         p8(fmth(o["total_hours"])), p8(f'${o["wage"]*0.05:.4f}'), p8(fmt(o["benefits"]))])
    fin_rows.append(['','','',p8b('Subtotal'),p8b(fmt(sum(o["benefits"] for o in officers_out)))])
    fin_rows.append([pw('TOTAL GRANT EXPENDITURE'),'','','',
                     Paragraph(f'<font size=10 color="#fbbf24"><b>{fmt(total_cost)}</b></font>', styles['Normal'])])

    nr = len(fin_rows)
    fs = [('BACKGROUND',(0,0),(-1,0),navy),('GRID',(0,0),(-1,-1),.4,rl_colors.HexColor('#d1d5db')),
          ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
          ('LEFTPADDING',(0,0),(-1,-1),6),('BACKGROUND',(0,nr-1),(-1,nr-1),navy),
          ('SPAN',(0,nr-1),(3,nr-1)),('ALIGN',(2,0),(4,-1),'RIGHT')]
    n_off = len(officers_out)
    for ri in [1, n_off+3, n_off*2+5]:
        if ri < nr:
            fs += [('BACKGROUND',(0,ri),(-1,ri),lgray),('SPAN',(0,ri),(-1,ri))]
    for ri in [n_off+2, n_off*2+4, n_off*3+6]:
        if ri < nr-1:
            fs.append(('BACKGROUND',(0,ri),(-1,ri),rl_colors.HexColor('#e5e7eb')))
    ft = Table(fin_rows, colWidths=[2.0*inch,2.1*inch,0.7*inch,1.0*inch,1.3*inch])
    ft.setStyle(TableStyle(fs)); story.append(ft); story.append(Spacer(1,10))

    # Per-officer breakdown
    story.append(Paragraph('<b><font size=10 color="#1e3a5f">PER-OFFICER EXPENDITURE DETAIL</font></b>', styles['Normal']))
    story.append(HRFlowable(width='100%', thickness=1.5, color=navy, spaceBefore=3, spaceAfter=6))
    for o in officers_out:
        oh = [[Paragraph(f'<font size=9 color="white"><b>{o["name"]}</b>  <font size=8>#{o["badge"]} · ${o["wage"]:.2f}/hr</font></font>', styles['Normal']),
               Paragraph(f'<font size=10 color="#fbbf24"><b>{fmt(o["total"])}</b></font>', styles['Normal'])]]
        oht = Table(oh, colWidths=[5.5*inch,1.7*inch])
        oht.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),dgray),('ALIGN',(1,0),(1,0),'RIGHT'),
                                  ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),5),
                                  ('BOTTOMPADDING',(0,0),(-1,-1),5),('LEFTPADDING',(0,0),(0,0),10),
                                  ('RIGHTPADDING',(1,0),(1,0),10)]))
        story.append(oht)
        sr = [[Paragraph(f'<para align=center><font size=12><b>{fmth(o["reg_hours"])}</b></font><br/><font size=7 color="#9ca3af">REG HRS</font></para>', styles['Normal']),
               Paragraph(f'<para align=center><font size=12><b>{fmth(o["ot_hours"])}</b></font><br/><font size=7 color="#9ca3af">OT HRS</font></para>', styles['Normal']),
               Paragraph(f'<para align=center><font size=12><b>{fmth(o["total_hours"])}</b></font><br/><font size=7 color="#9ca3af">TOTAL HRS</font></para>', styles['Normal']),
               Paragraph(f'<para align=center><font size=12><b>{o["reports"]}</b></font><br/><font size=7 color="#9ca3af">REPORTS</font></para>', styles['Normal']),
               Paragraph(f'<para align=center><font size=12><b>{o["citations"]}</b></font><br/><font size=7 color="#9ca3af">CITATIONS</font></para>', styles['Normal']),
               Paragraph(f'<para align=center><font size=12><b>{o["citations_per_hour"]}</b></font><br/><font size=7 color="#9ca3af">CIT/HR</font></para>', styles['Normal'])]]
        srt = Table(sr, colWidths=[1.2*inch]*6)
        srt.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),lgray),('INNERGRID',(0,0),(-1,-1),.4,rl_colors.HexColor('#e5e7eb')),
                                  ('BOX',(0,0),(-1,-1),.4,rl_colors.HexColor('#d1d5db')),('ALIGN',(0,0),(-1,-1),'CENTER'),
                                  ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
        story.append(srt)
        dd = [[p8g('Regular Pay'),p8g(f'{fmth(o["reg_hours"])} × ${o["wage"]:.2f}'),p8(fmt(o["reg_pay"]))],
              [p8g('Overtime Pay'),p8g(f'{fmth(o["ot_hours"])} × ${o["wage"]:.2f} × 1.5'),p8(fmt(o["ot_pay"]))],
              [p8g('Benefits'),p8g(f'{fmth(o["total_hours"])} × ${o["wage"]:.2f} × 0.05'),p8(fmt(o["benefits"]))],
              [p8b('Officer Total'),'',p8b(fmt(o["total"]))]]
        ddt = Table(dd, colWidths=[1.5*inch,4.2*inch,1.5*inch])
        ddt.setStyle(TableStyle([('BOX',(0,0),(-1,-1),.4,rl_colors.HexColor('#d1d5db')),
                                  ('LINEBELOW',(0,0),(-1,-2),.4,rl_colors.HexColor('#e5e7eb')),
                                  ('BACKGROUND',(0,3),(-1,3),lgray),('ALIGN',(2,0),(2,-1),'RIGHT'),
                                  ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),4),
                                  ('BOTTOMPADDING',(0,0),(-1,-1),4),('LEFTPADDING',(0,0),(-1,-1),8),
                                  ('RIGHTPADDING',(2,0),(2,-1),8)]))
        story.append(ddt); story.append(Spacer(1,7))

    # Activity table
    story.append(Paragraph('<b><font size=10 color="#1e3a5f">TASK FORCE ACTIVITY SUMMARY</font></b>', styles['Normal']))
    story.append(HRFlowable(width='100%', thickness=1.5, color=navy, spaceBefore=3, spaceAfter=6))
    act_hdr = [pw(h) for h in ['Officer','Shifts','Contacts','Citations','DUI','Felony','Hours','Cit/Hr']]
    act_rows = [act_hdr]
    for i, o in enumerate(officers_out):
        dui = sum(o['cats'].get(k,0) for k in ['dui_alcohol','dui_drugs','dui_drugs_alcohol'])
        row = [p8(f'{o["name"]}, #{o["badge"]}'),p8(str(o["reports"])),p8(str(o["cats"].get("total_contacts",0))),
               p8(str(o["citations"])),p8(str(dui)),p8(str(o["cats"].get("felony_arrests",0))),
               p8(fmth(o["total_hours"])),p8(str(o["citations_per_hour"]))]
        act_rows.append(row)
    act_rows.append([pw('TOTALS'),pw(str(total_reports)),pw(str(total_contacts)),pw(str(total_cit)),
                     pw(str(total_dui)),pw(str(sum(o["cats"].get("felony_arrests",0) for o in officers_out))),
                     pw(fmth(total_hours)),pw(str(total_cph))])
    atbl = Table(act_rows, colWidths=[1.6*inch,.5*inch,.7*inch,.7*inch,.5*inch,.55*inch,.65*inch,.5*inch])
    ast = [('BACKGROUND',(0,0),(-1,0),dgray),('BACKGROUND',(0,len(act_rows)-1),(-1,len(act_rows)-1),navy),
           ('GRID',(0,0),(-1,-1),.4,rl_colors.HexColor('#d1d5db')),('ALIGN',(1,0),(-1,-1),'CENTER'),
           ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
           ('LEFTPADDING',(0,0),(-1,-1),5)]
    for i in range(1,len(act_rows)-1):
        if i%2==0: ast.append(('BACKGROUND',(0,i),(-1,i),lgray))
    atbl.setStyle(TableStyle(ast)); story.append(atbl)
    story.append(Paragraph(f'<font size=7 color="#9ca3af">* Citations include traffic violations and DUI. Period: {g_obj.start_date} – {g_obj.end_date}.</font>', styles['Normal']))
    story.append(Spacer(1,10))

    # Signature
    story.append(HRFlowable(width='100%', thickness=1, color=rl_colors.HexColor('#d1d5db'), spaceAfter=5))
    story.append(Paragraph('<b><font size=10 color="#1e3a5f">CERTIFICATION &amp; SIGNATURES</font></b>', styles['Normal']))
    story.append(Spacer(1,4))
    story.append(Paragraph('<font size=8 color="#374151">I certify that the information contained in this report is true and accurate to the best of my knowledge, and that all expenditures were incurred in furtherance of the approved grant activities during the stated grant period.</font>', styles['Normal']))
    story.append(Spacer(1,14))
    sig_data = [[Paragraph('<font size=8 color="#9ca3af">SUPERVISOR SIGNATURE</font><br/><br/><br/>', styles['Normal']),
                 Paragraph('<font size=8 color="#9ca3af">DATE SUBMITTED</font><br/><br/><br/>', styles['Normal']),
                 Paragraph('<font size=8 color="#9ca3af">AGENCY APPROVAL</font><br/><br/><br/>', styles['Normal'])]]
    sigt = Table(sig_data, colWidths=[2.4*inch,2.0*inch,2.8*inch])
    sigt.setStyle(TableStyle([('LINEABOVE',(0,0),(-1,0),1.5,rl_colors.HexColor('#1e293b')),('TOPPADDING',(0,0),(-1,-1),5)]))
    story.append(sigt)

    def on_page(canv, doc_obj):
        canv.saveState()
        W, H = letter
        canv.setFillColor(navy)
        canv.rect(0, 0, W, 0.4*inch, fill=1, stroke=0)
        canv.setFillColor(white_c)
        canv.setFont('Helvetica-Bold', 7)
        canv.drawString(0.65*inch, 0.15*inch,
            f'ShiftLog  ·  {dept_name}  ·  Confidential — For Official Use Only')
        canv.drawRightString(W-0.65*inch, 0.15*inch,
            f'Report GR-{gid:04d}  ·  {datetime.now().strftime("%B %d, %Y")}  ·  Page {doc_obj.page}')
        canv.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    out_buf.seek(0)
    safe_name = (g_obj.name or 'grant').replace(' ', '_')[:40]
    return send_file(out_buf, as_attachment=True,
                     download_name=f"Grant_Report_{safe_name}.pdf",
                     mimetype='application/pdf')


def _find_template():
    for name in ('template.pdf', 'Tracking_Forms.pdf'):
        p = os.path.join(BASE_DIR, name)
        if os.path.exists(p):
            return p
    return os.environ.get('TEMPLATE_PDF', os.path.join(BASE_DIR, 'template.pdf'))
TEMPLATE_PDF = _find_template()

NX = 293
FIELDS = {
    'name':               {'x': 62,  'y': 658, 'align': 'left',   'size': 8},
    'badge':              {'x': 150, 'y': 658, 'align': 'left',   'size': 8},
    'ot_hours':           {'x': 550, 'y': 676, 'align': 'right',  'size': 7},
    'mileage':            {'x': 550, 'y': 664, 'align': 'right',  'size': 7},
    'date':               {'x': 318, 'y': 650, 'align': 'left',   'size': 6.5},
    'start_time':         {'x': 420, 'y': 650, 'align': 'left',   'size': 6.5},
    'end_time':           {'x': 510, 'y': 650, 'align': 'left',   'size': 6.5},
    'total_contacts':     {'x': NX, 'y': 523.0, 'align': 'center', 'size': 8},
    'dui_alcohol':        {'x': NX, 'y': 505.5, 'align': 'center', 'size': 8},
    'dui_drugs':          {'x': NX, 'y': 487.9, 'align': 'center', 'size': 8},
    'dui_drugs_alcohol':  {'x': NX, 'y': 470.1, 'align': 'center', 'size': 8},
    'underage_alcohol':   {'x': NX, 'y': 452.6, 'align': 'center', 'size': 8},
    'seat_belt':          {'x': NX, 'y': 435.1, 'align': 'center', 'size': 8},
    'child_safety_seat':  {'x': NX, 'y': 417.4, 'align': 'center', 'size': 8},
    'felony_arrests':     {'x': NX, 'y': 399.9, 'align': 'center', 'size': 8},
    'recovered_stolen':   {'x': NX, 'y': 382.3, 'align': 'center', 'size': 8},
    'fugitives':          {'x': NX, 'y': 364.5, 'align': 'center', 'size': 8},
    'suspended_licenses': {'x': NX, 'y': 347.0, 'align': 'center', 'size': 8},
    'uninsured_motorists':{'x': NX, 'y': 329.5, 'align': 'center', 'size': 8},
    'speeding':           {'x': NX, 'y': 311.8, 'align': 'center', 'size': 8},
    'reckless_driving':   {'x': NX, 'y': 294.2, 'align': 'center', 'size': 8},
    'inattentive_driving':{'x': NX, 'y': 276.7, 'align': 'center', 'size': 8},
    'texting':            {'x': NX, 'y': 258.9, 'align': 'center', 'size': 8},
    'motorcycle_endorsement':{'x': NX, 'y': 241.4, 'align': 'center', 'size': 8},
    'bicycle_pedestrian': {'x': NX, 'y': 223.9, 'align': 'center', 'size': 8},
    'other_activity':     {'x': NX, 'y': 206.1, 'align': 'center', 'size': 8},
    'details':            {'x': 410, 'y': 462, 'align': 'left', 'multiline': True,
                           'line_height': 10, 'max_chars': 30, 'size': 7},
    'media_letters':      {'x': 240, 'y': 127, 'align': 'left', 'size': 7},
    'media_press':        {'x': 240, 'y': 112, 'align': 'left', 'size': 7},
    'media_social':       {'x': 240, 'y': 97,  'align': 'left', 'size': 7},
    'media_events':       {'x': 240, 'y': 82,  'align': 'left', 'size': 7},
    'media_interviews':   {'x': 240, 'y': 67,  'align': 'left', 'size': 7},
    'media_presentations':{'x': 240, 'y': 52,  'align': 'left', 'size': 7},
    'media_other':        {'x': 240, 'y': 37,  'align': 'left', 'size': 7},
}


def build_overlay(data):
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(612, 792))
    c.setFillColorRGB(0.05, 0.05, 0.05)
    for key, field in FIELDS.items():
        val = str(data.get(key, '') or '').strip()
        if not val or val == '0':
            continue
        x, y = field['x'], field['y']
        c.setFont('Helvetica-Bold', field.get('size', 8))
        if field.get('multiline'):
            words, line, out = val.split(), '', []
            for w in words:
                t = (line + ' ' + w).strip()
                if len(t) <= field['max_chars']:
                    line = t
                else:
                    out.append(line); line = w
            if line:
                out.append(line)
            lh = field.get('line_height', 10)
            for i, ln in enumerate(out[:18]):
                c.drawString(x, y - i * lh, ln)
        elif field['align'] == 'center':
            c.drawCentredString(x, y, val)
        elif field['align'] == 'right':
            c.drawRightString(x, y, val)
        else:
            c.drawString(x, y, val)
    c.save(); packet.seek(0)
    return packet


@app.route('/generate_pdf', methods=['POST'])
def generate_pdf():
    data = request.json or {}
    try:
        # Append Other Contacts to details for PDF
        other_parts = []
        if int(data.get('assist_officer') or 0):
            other_parts.append(f"Assist Officer: {data['assist_officer']}")
        if int(data.get('calls_for_service') or 0):
            other_parts.append(f"Calls for Service: {data['calls_for_service']}")
        if int(data.get('field_interview') or 0):
            other_parts.append(f"FI: {data['field_interview']}")
        if other_parts:
            existing = (data.get('details') or '').strip()
            data = dict(data)
            data['details'] = (existing + (' | ' if existing else '') + ' | '.join(other_parts))
        overlay = PdfReader(build_overlay(data))
        reader = PdfReader(TEMPLATE_PDF)
        writer = PdfWriter()
        page = reader.pages[0]
        page.merge_page(overlay.pages[0])
        writer.add_page(page)
        out = io.BytesIO(); writer.write(out); out.seek(0)
        safe = (data.get('name', 'officer') or 'officer').replace(' ', '_')
        dt = (data.get('date', '') or '').replace('/', '-')
        return send_file(out, as_attachment=True,
                         download_name=f"HVE_Report_{safe}_{dt}.pdf",
                         mimetype='application/pdf')
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/report/<kind>')
@role_required('supervisor', 'admin')
def report(kind):
    officer = request.args.get('officer', 'all')
    s = db()
    try:
        q = s.query(Submission)
        if officer != 'all':
            q = q.filter(Submission.officer_name == officer)
        q = _apply_date_filter(q)
        subs = q.order_by(Submission.created_at.desc()).all()
        data, totals, per_officer = [], {k: 0 for k in CAT_KEYS}, {}
        for sub in subs:
            try:
                cats = json.loads(sub.data_json)
            except Exception:
                cats = {}
            for k in CAT_KEYS:
                totals[k] += int(cats.get(k, 0) or 0)
            data.append((sub.shift_date or sub.created_at.strftime('%m/%d/%Y'),
                         sub.officer_name, cats))
            o = sub.officer_name or '?'
            per_officer.setdefault(o, {'forms': 0, 'contacts': 0})
            per_officer[o]['forms'] += 1
            per_officer[o]['contacts'] += int(cats.get('total_contacts', 0) or 0)
        if kind == 'history':
            out = _history_pdf(data, officer); fn = f"HVE_History_{officer}.pdf"
        else:
            out = _stats_pdf(totals, per_officer, officer, len(data)); fn = f"HVE_Stats_{officer}.pdf"
        return send_file(out, as_attachment=True, download_name=fn, mimetype='application/pdf')
    finally:
        s.close()


def _history_pdf(data, officer):
    out = io.BytesIO(); c = canvas.Canvas(out, pagesize=letter); W, H = letter
    y = H - 50
    c.setFont('Helvetica-Bold', 16); c.drawString(50, y, "HVE History Report")
    c.setFont('Helvetica', 10); c.setFillColorRGB(.4, .4, .4); y -= 18
    c.drawString(50, y, f"Officer: {officer if officer!='all' else 'All Officers'}   "
                        f"Generated: {datetime.now().strftime('%m/%d/%Y %I:%M %p')}")
    c.setFillColorRGB(0, 0, 0); y -= 30
    cols = ['Date', 'Officer', 'Contacts', 'SeatBelt', 'Speed', 'DUI', 'Susp', 'Text', 'Felony']
    keys = [None, None, 'total_contacts', 'seat_belt', 'speeding', 'dui_alcohol',
            'suspended_licenses', 'texting', 'felony_arrests']
    xs = [50, 110, 200, 270, 340, 400, 450, 510, 560]
    c.setFont('Helvetica-Bold', 9)
    for i, col in enumerate(cols):
        c.drawString(xs[i], y, col)
    y -= 4; c.line(50, y, W - 50, y); y -= 14; c.setFont('Helvetica', 9)
    for (dt, name, cats) in data:
        if y < 60:
            c.showPage(); y = H - 50; c.setFont('Helvetica', 9)
        c.drawString(xs[0], y, str(dt)[:9])
        c.drawString(xs[1], y, str(name or '')[:12])
        for i, k in enumerate(keys[2:], start=2):
            c.drawString(xs[i], y, str(cats.get(k, 0)))
        y -= 14
    if not data:
        c.drawString(50, y, "No submissions yet.")
    c.save(); out.seek(0); return out


def _stats_pdf(totals, per_officer, officer, form_count):
    out = io.BytesIO(); c = canvas.Canvas(out, pagesize=letter); W, H = letter
    y = H - 50
    c.setFont('Helvetica-Bold', 18); c.drawString(50, y, "HVE Stats Summary"); y -= 20
    c.setFont('Helvetica', 10); c.setFillColorRGB(.4, .4, .4)
    c.drawString(50, y, f"Officer: {officer if officer!='all' else 'All Officers'}   "
                        f"Forms: {form_count}   Generated: {datetime.now().strftime('%m/%d/%Y')}")
    c.setFillColorRGB(0, 0, 0); y -= 36
    contacts = totals.get('total_contacts', 0)
    citations = sum(totals.get(k, 0) for k in ['seat_belt','speeding','texting','suspended_licenses',
                    'child_safety_seat','reckless_driving','inattentive_driving','uninsured_motorists'])
    arrests = sum(totals.get(k, 0) for k in ['dui_alcohol','dui_drugs','dui_drugs_alcohol','felony_arrests'])
    for i, (label, val) in enumerate([('Total Contacts', contacts), ('Citations', citations),
                                       ('Arrests', arrests), ('Forms', form_count)]):
        tx = 50 + i * 130
        c.setFillColorRGB(.04, .04, .04); c.rect(tx, y - 44, 120, 44, fill=1, stroke=0)
        c.setFillColorRGB(.23, .51, .96); c.setFont('Helvetica-Bold', 22)
        c.drawString(tx + 10, y - 26, str(val))
        c.setFillColorRGB(.5, .5, .5); c.setFont('Helvetica', 8)
        c.drawString(tx + 10, y - 38, label.upper())
    y -= 70
    c.setFillColorRGB(0, 0, 0); c.setFont('Helvetica-Bold', 12)
    c.drawString(50, y, "Category Breakdown"); y -= 20
    bars = [(lbl, totals.get(k, 0)) for k, lbl in CATEGORIES if totals.get(k, 0) > 0]
    bars.sort(key=lambda x: x[1], reverse=True)
    mx = max([v for _, v in bars], default=1)
    c.setFont('Helvetica', 9)
    for lbl, val in bars[:14]:
        if y < 80:
            c.showPage(); y = H - 50; c.setFont('Helvetica', 9)
        c.setFillColorRGB(.4, .4, .4); c.drawRightString(160, y, lbl[:22])
        bw = (val / mx) * 320
        c.setFillColorRGB(.23, .51, .96); c.rect(170, y - 2, bw, 11, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0); c.setFont('Helvetica-Bold', 9)
        c.drawString(175 + bw, y, str(val)); c.setFont('Helvetica', 9); y -= 18
    y -= 10
    if per_officer and y > 120:
        c.setFillColorRGB(0, 0, 0); c.setFont('Helvetica-Bold', 12)
        c.drawString(50, y, "Officer Contributions"); y -= 20; c.setFont('Helvetica', 9)
        for name, dd in sorted(per_officer.items(), key=lambda x: x[1]['contacts'], reverse=True):
            if y < 60:
                c.showPage(); y = H - 50; c.setFont('Helvetica', 9)
            c.setFillColorRGB(.4, .4, .4); c.drawString(50, y, f"{name}")
            c.setFillColorRGB(0, 0, 0)
            c.drawString(220, y, f"{dd['forms']} forms")
            c.drawString(320, y, f"{dd['contacts']} contacts"); y -= 16
    c.save(); out.seek(0); return out


@app.route('/')
def index():
    with open(os.path.join(BASE_DIR, 'templates', 'index.html')) as f:
        return f.read()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7860))
    app.run(host='0.0.0.0', port=port, debug=False)
