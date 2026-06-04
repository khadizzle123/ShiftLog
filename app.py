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
    ('total_contacts', 'Total Contacts'),
    ('seat_belt', 'Seat Belt Citations'),
    ('child_safety_seat', 'Child Safety Seat'),
    ('speeding', 'Speeding Citations'),
    ('texting', 'Texting Citations'),
    ('suspended_licenses', 'Suspended Licenses'),
    ('uninsured_motorists', 'Uninsured Motorists'),
    ('dui_alcohol', 'DUI - Alcohol'),
    ('dui_drugs', 'DUI - Drugs'),
    ('dui_drugs_alcohol', 'DUI - Drugs & Alcohol'),
    ('underage_alcohol', 'Underage Alcohol'),
    ('reckless_driving', 'Reckless Driving'),
    ('inattentive_driving', 'Inattentive Driving'),
    ('felony_arrests', 'Felony Arrests'),
    ('recovered_stolen', 'Recovered Stolen'),
    ('fugitives', 'Fugitives'),
    ('motorcycle_endorsement', 'Motorcycle'),
    ('bicycle_pedestrian', 'Bicycle/Pedestrian'),
    ('other_activity', 'Other'),
]
CAT_KEYS = [k for k, _ in CATEGORIES]


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
    mileage = Column(String(20))
    details = Column(Text)
    data_json = Column(Text)
    source = Column(String(20), default='form')
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)

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
    name = (d.get('name') or '').strip()
    badge = (d.get('badge') or '').strip()
    pw = d.get('password') or ''
    if not name or not badge or not pw:
        return jsonify({'error': 'Name, badge, and password are required.'}), 400
    s = db()
    try:
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
    d = request.json or {}
    s = db()
    try:
        u = current_user(s)
        cats = {k: int(d.get(k) or 0) for k in CAT_KEYS}
        sub = Submission(
            user_id=u.id, officer_name=u.name, badge=u.badge, department=u.department,
            shift_date=d.get('date') or '', start_time=d.get('start_time') or '',
            end_time=d.get('end_time') or '', ot_hours=str(d.get('ot_hours') or ''),
            mileage=str(d.get('mileage') or ''), details=d.get('details') or '',
            data_json=json.dumps(cats), source='form')
        s.add(sub); s.commit()
        return jsonify({'ok': True, 'submission_id': sub.id})
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


@app.route('/api/performance')
@role_required('supervisor', 'admin')
def performance():
    officer = request.args.get('officer', 'all')
    s = db()
    try:
        q = s.query(Submission)
        if officer != 'all':
            q = q.filter(Submission.officer_name == officer)
        subs = q.order_by(Submission.created_at.desc()).all()
        rows, totals = [], {k: 0 for k in CAT_KEYS}
        for sub in subs:
            try:
                cats = json.loads(sub.data_json)
            except Exception:
                cats = {}
            for k in CAT_KEYS:
                totals[k] += int(cats.get(k, 0) or 0)
            rows.append({'date': sub.shift_date or sub.created_at.strftime('%m/%d/%Y'),
                         'officer': sub.officer_name, 'badge': sub.badge, **cats})
        return jsonify({'rows': rows, 'totals': totals, 'count': len(rows)})
    finally:
        s.close()


@app.route('/api/import_pdf', methods=['POST'])
@role_required('supervisor', 'admin')
def import_pdf():
    officer = (request.form.get('officer_name') or '').strip()
    badge = (request.form.get('badge') or '').strip()
    files = request.files.getlist('files')
    if not officer or not files:
        return jsonify({'error': 'Officer name and at least one PDF are required.'}), 400
    s = db()
    try:
        count = 0
        for f in files:
            f.read()
            sub = Submission(officer_name=officer, badge=badge, source='import',
                             shift_date=datetime.utcnow().strftime('%m/%d/%Y'),
                             data_json=json.dumps({k: 0 for k in CAT_KEYS}))
            s.add(sub); count += 1
        s.commit()
        return jsonify({'ok': True, 'imported': count})
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
