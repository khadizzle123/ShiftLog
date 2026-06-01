from flask import Flask, request, send_file, jsonify
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
import io, os, textwrap

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')

def _find_template():
    for name in ('template.pdf', 'Tracking_Forms.pdf'):
        p = os.path.join(BASE_DIR, name)
        if os.path.exists(p):
            return p
    return os.environ.get('TEMPLATE_PDF', os.path.join(BASE_DIR, 'template.pdf'))
TEMPLATE_PDF = _find_template()

# Calibrated for the electronic HVE form (US Letter 612x792 pts)
NX = 293   # center of Number column
FIELDS = {
    'name':               {'x': 62,  'y': 652, 'align': 'left',   'size': 8},
    'badge':              {'x': 150, 'y': 652, 'align': 'left',   'size': 8},
    'ot_hours':           {'x': 510, 'y': 676, 'align': 'right',  'size': 7},
    'mileage':            {'x': 510, 'y': 664, 'align': 'right',  'size': 7},
    'date':               {'x': 318, 'y': 650, 'align': 'left',   'size': 6.5},
    'start_time':         {'x': 420, 'y': 650, 'align': 'left',   'size': 6.5},
    'end_time':           {'x': 510, 'y': 650, 'align': 'left',   'size': 6.5},

    # Activity table rows (NX=293, centered in Number column)
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

    # Details box (below "Details:" label, wrapped)
    'details':            {'x': 410, 'y': 462, 'align': 'left', 'multiline': True,
                           'line_height': 10, 'max_chars': 30, 'size': 7},

    # Media outreach rows (x=240)
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
        x, y   = field['x'], field['y']
        size   = field.get('size', 8)
        align  = field.get('align', 'left')
        c.setFont('Helvetica-Bold', size)

        if field.get('multiline'):
            words = val.split()
            line, lines_out = '', []
            mc = field['max_chars']
            for w in words:
                test = (line + ' ' + w).strip()
                if len(test) <= mc:
                    line = test
                else:
                    lines_out.append(line)
                    line = w
            if line:
                lines_out.append(line)
            lh = field.get('line_height', 10)
            for i, ln in enumerate(lines_out[:18]):
                c.drawString(x, y - i * lh, ln)
        elif align == 'center':
            c.drawCentredString(x, y, val)
        elif align == 'right':
            c.drawRightString(x, y, val)
        else:
            c.drawString(x, y, val)

    c.save()
    packet.seek(0)
    return packet

@app.route('/')
def index():
    with open(os.path.join(BASE_DIR, 'templates', 'index.html')) as f:
        return f.read()

@app.route('/generate_pdf', methods=['POST'])
def generate_pdf():
    data = request.json or {}
    print('RECEIVED:', data, flush=True)
    try:
        overlay_packet = build_overlay(data)

        reader  = PdfReader(TEMPLATE_PDF)
        overlay = PdfReader(overlay_packet)
        writer  = PdfWriter()
        page    = reader.pages[0]
        page.merge_page(overlay.pages[0])
        writer.add_page(page)

        out = io.BytesIO()
        writer.write(out)
        out.seek(0)

        safe = (data.get('name', 'officer') or 'officer').replace(' ', '_')
        date = (data.get('date', '') or '').replace('/', '-')
        fname = f"HVE_Report_{safe}_{date}.pdf"

        return send_file(out, as_attachment=True,
                         download_name=fname,
                         mimetype='application/pdf')
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7860))
    app.run(host='0.0.0.0', port=port, debug=False)
