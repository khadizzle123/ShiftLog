from flask import Flask, request, send_file, jsonify
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
import io, os, textwrap

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')

# Look for the blank HVE form next to app.py. Rename your scanned form to
# template.pdf and drop it in the same folder as this file.
def _find_template():
    for name in ('template.pdf', 'CamScanner_5-31-26_17_34.pdf'):
        p = os.path.join(BASE_DIR, name)
        if os.path.exists(p):
            return p
    return os.environ.get('TEMPLATE_PDF', os.path.join(BASE_DIR, 'template.pdf'))
TEMPLATE_PDF = _find_template()

# Calibrated PDF coordinates (ReportLab origin = bottom-left, units = pts)
# x = left edge or center for centered text
# y = baseline
NX = 269   # center of Number column
FIELDS = {
    'name':               {'x': 80,  'y': 676, 'align': 'left',   'size': 8},
    'badge':              {'x': 150, 'y': 676, 'align': 'left',   'size': 8},
    'ot_hours':           {'x': 525, 'y': 692, 'align': 'right',  'size': 7},
    'mileage':            {'x': 525, 'y': 680, 'align': 'right',  'size': 7},
    'date':               {'x': 305, 'y': 666, 'align': 'left',   'size': 6.5},
    'start_time':         {'x': 410, 'y': 666, 'align': 'left',   'size': 6.5},
    'end_time':           {'x': 487, 'y': 666, 'align': 'left',   'size': 6.5},
    'total_contacts':     {'x': NX,  'y': 550, 'align': 'center'},
    'dui_alcohol':        {'x': NX,  'y': 533, 'align': 'center'},
    'dui_drugs':          {'x': NX,  'y': 516, 'align': 'center'},
    'dui_drugs_alcohol':  {'x': NX,  'y': 498, 'align': 'center'},
    'underage_alcohol':   {'x': NX,  'y': 480, 'align': 'center'},
    'seat_belt':          {'x': NX,  'y': 462, 'align': 'center'},
    'child_safety_seat':  {'x': NX,  'y': 445, 'align': 'center'},
    'felony_arrests':     {'x': NX,  'y': 427, 'align': 'center'},
    'recovered_stolen':   {'x': NX,  'y': 410, 'align': 'center'},
    'fugitives':          {'x': NX,  'y': 392, 'align': 'center'},
    'suspended_licenses': {'x': NX,  'y': 375, 'align': 'center'},
    'uninsured_motorists':{'x': NX,  'y': 357, 'align': 'center'},
    'speeding':           {'x': NX,  'y': 339, 'align': 'center'},
    'reckless_driving':   {'x': NX,  'y': 322, 'align': 'center'},
    'inattentive_driving':{'x': NX,  'y': 304, 'align': 'center'},
    'texting':            {'x': NX,  'y': 287, 'align': 'center'},
    'motorcycle_endorsement': {'x': NX, 'y': 269, 'align': 'center'},
    'bicycle_pedestrian': {'x': NX,  'y': 251, 'align': 'center'},
    'other_activity':     {'x': NX,  'y': 234, 'align': 'center'},
    'details':            {'x': 445, 'y': 493, 'align': 'left',   'multiline': True,
                           'line_height': 9, 'max_chars': 28, 'size': 7},
    'media_letters':      {'x': 200, 'y': 158, 'align': 'left'},
    'media_press':        {'x': 200, 'y': 143, 'align': 'left'},
    'media_social':       {'x': 200, 'y': 128, 'align': 'left'},
    'media_events':       {'x': 200, 'y': 113, 'align': 'left'},
    'media_interviews':   {'x': 200, 'y': 98,  'align': 'left'},
    'media_presentations':{'x': 200, 'y': 83,  'align': 'left'},
    'media_other':        {'x': 200, 'y': 68,  'align': 'left'},
}

def build_overlay(data):
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(595, 842))
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
            # wrap text to max_chars width
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
    return app.send_static_file('shiftlog.html')

@app.route('/generate_pdf', methods=['POST'])
def generate_pdf():
    data = request.json or {}
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
