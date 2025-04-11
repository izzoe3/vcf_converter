from flask import Flask, request, jsonify, send_from_directory, send_file, session, redirect, url_for, render_template_string
import csv
from io import StringIO, BytesIO
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import qrcode
from PIL import Image
import zipfile
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import base64  # Add for base64 encoding

load_dotenv(override=True)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.secret_key = os.getenv('FLASK_SECRET_KEY')

APP_PASSWORD = os.getenv('APP_PASSWORD')
if not APP_PASSWORD:
    raise ValueError("APP_PASSWORD must be set in environment variables")
PASSWORD_HASH = generate_password_hash(APP_PASSWORD)

def get_db_connection():
    conn = psycopg2.connect(
        dbname=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT', '5432'),
        cursor_factory=RealDictCursor
    )
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            mobile TEXT,
            designation TEXT,
            faculty TEXT,
            school TEXT
        );
        CREATE TABLE IF NOT EXISTS metadata (
            id SERIAL PRIMARY KEY,
            last_activity TIMESTAMP
        );
    """)
    cur.execute("SELECT COUNT(*) FROM metadata")
    if cur.fetchone()['count'] == 0:
        cur.execute("INSERT INTO metadata (last_activity) VALUES (%s)", (datetime.now(timezone.utc),))
    conn.commit()
    cur.close()
    conn.close()

def check_and_clean_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT last_activity FROM metadata LIMIT 1")
    result = cur.fetchone()
    last_activity = result['last_activity'].replace(tzinfo=timezone.utc) if result else datetime.now(timezone.utc)
    if datetime.now(timezone.utc) - last_activity > timedelta(days=14):
        cur.execute("TRUNCATE TABLE employees, metadata RESTART IDENTITY")
        cur.execute("INSERT INTO metadata (last_activity) VALUES (%s)", (datetime.now(timezone.utc),))
    conn.commit()
    cur.close()
    conn.close()

def update_last_activity():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE metadata SET last_activity = %s", (datetime.now(timezone.utc),))
    conn.commit()
    cur.close()
    conn.close()

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

init_db()

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if check_password_hash(PASSWORD_HASH, password):
            session['authenticated'] = True
            update_last_activity()
            return redirect(url_for('index'))
        else:
            return render_template_string("""
                <form method="post">
                    <label>Password: <input type="password" name="password"></label>
                    <input type="submit" value="Login">
                    <p style="color: red;">Incorrect password</p>
                </form>
            """)
    return render_template_string("""
        <form method="post">
            <label>Password: <input type="password" name="password"></label>
            <input type="submit" value="Login">
        </form>
    """)

@app.route('/')
def index():
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    check_and_clean_db()
    update_last_activity()
    return app.send_static_file('index.html')

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('login'))

@app.route('/upload', methods=['POST'])
def upload_csv():
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    check_and_clean_db()
    update_last_activity()
    
    if 'file' not in request.files:
        return jsonify({'message': 'No file uploaded'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'message': 'No file selected'}), 400
    
    stream = StringIO(file.stream.read().decode("UTF-8"), newline="")
    csv_reader = csv.reader(stream)
    headers = next(csv_reader)
    updated = 0
    inserted = 0
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT email FROM employees")
    email_set = {row['email'] for row in cur.fetchall()}
    
    for row in csv_reader:
        if len(row) < 9:
            continue
        email = row[1]
        is_existing = email in email_set
        cur.execute("""
            INSERT INTO employees (name, email, mobile, designation, faculty, school)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE
            SET name = EXCLUDED.name,
                mobile = EXCLUDED.mobile,
                designation = EXCLUDED.designation,
                faculty = EXCLUDED.faculty,
                school = EXCLUDED.school
        """, (row[3], email, row[8], row[5], row[6], row[7]))
        if is_existing:
            updated += 1
        else:
            inserted += 1
            email_set.add(email)
    
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'CSV processed', 'inserted': inserted, 'updated': updated})

@app.route('/api/employees', methods=['GET'])
def get_employees():
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    check_and_clean_db()
    update_last_activity()
    
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    query = request.args.get('name', '').lower()
    offset = (page - 1) * per_page
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) FROM employees WHERE LOWER(name) LIKE %s", (f'%{query}%',))
    total = cur.fetchone()['count']
    
    cur.execute(
        "SELECT name, email, mobile FROM employees WHERE LOWER(name) LIKE %s ORDER BY name LIMIT %s OFFSET %s",
        (f'%{query}%', per_page, offset)
    )
    employees = cur.fetchall()
    cur.close()
    conn.close()
    
    return jsonify({
        'employees': employees,
        'total': total,
        'page': page,
        'per_page': per_page
    })

@app.route('/add_employee', methods=['POST'])
def add_employee():
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    check_and_clean_db()
    update_last_activity()
    
    data = request.form
    name = data.get('name')
    email = data.get('email')
    mobile = data.get('mobile')
    designation = data.get('designation')
    faculty = data.get('faculty')
    school = data.get('school')
    
    if not name or not email:
        return jsonify({'message': 'Name and email are required'}), 400
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO employees (name, email, mobile, designation, faculty, school)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (email) DO UPDATE
        SET name = EXCLUDED.name,
            mobile = EXCLUDED.mobile,
            designation = EXCLUDED.designation,
            faculty = EXCLUDED.faculty,
            school = EXCLUDED.school
    """, (name, email, mobile, designation, faculty, school))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Employee added/updated'})

@app.route('/generate_qr/<name>')
def generate_qr(name):
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    check_and_clean_db()
    update_last_activity()
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE name = %s", (name,))
    emp = cur.fetchone()
    cur.close()
    conn.close()
    if not emp:
        return jsonify({'message': 'Employee not found'}), 404
    
    name_parts = emp['name'].split(' ')
    last_name = name_parts[-1] if len(name_parts) > 1 else ''
    first_name = ' '.join(name_parts[:-1]) if len(name_parts) > 1 else emp['name']
    title = ' | '.join(filter(None, [emp['designation'], emp['faculty'], emp['school']]))
    vcard = f"""BEGIN:VCARD
VERSION:3.0
N:{last_name};{first_name};;;
FN:{emp['name']}
TITLE:{title}
EMAIL:{emp['email']}
TEL;TYPE=CELL:{emp['mobile']}
END:VCARD"""
    
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(vcard)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    img_byte_arr = BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_base64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
    
    return jsonify({
        'name': emp['name'],
        'email': emp['email'],
        'mobile': emp['mobile'] or 'N/A',
        'designation': emp['designation'] or 'N/A',
        'faculty': emp['faculty'] or 'N/A',
        'school': emp['school'] or 'N/A',
        'qr_code': f'data:image/png;base64,{img_base64}'
    })

@app.route('/vcard/<name>')
def generate_vcard(name):
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    check_and_clean_db()
    update_last_activity()
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE name = %s", (name,))
    emp = cur.fetchone()
    cur.close()
    conn.close()
    if not emp:
        return "Employee not found", 404
    
    name_parts = emp['name'].split(' ')
    last_name = name_parts[-1] if len(name_parts) > 1 else ''
    first_name = ' '.join(name_parts[:-1]) if len(name_parts) > 1 else emp['name']
    title = ' | '.join(filter(None, [emp['designation'], emp['faculty'], emp['school']]))
    vcard = f"""BEGIN:VCARD
VERSION:3.0
N:{last_name};{first_name};;;
FN:{emp['name']}
TITLE:{title}
EMAIL:{emp['email']}
TEL;TYPE=CELL:{emp['mobile']}
END:VCARD"""
    
    vcard_buffer = BytesIO(vcard.encode('utf-8'))
    return send_file(
        vcard_buffer,
        mimetype='text/vcard',
        as_attachment=True,
        download_name=f"{emp['name'].replace(' ', '_')}.vcf"
    )

@app.route('/download_all_qr')
def download_all_qr():
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    check_and_clean_db()
    update_last_activity()
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM employees")
    employees = cur.fetchall()
    cur.close()
    conn.close()
    
    if not employees:
        return "No employees found", 404
    
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for emp in employees:
            name_parts = emp['name'].split(' ')
            last_name = name_parts[-1] if len(name_parts) > 1 else ''
            first_name = ' '.join(name_parts[:-1]) if len(name_parts) > 1 else emp['name']
            title = ' | '.join(filter(None, [emp['designation'], emp['faculty'], emp['school']]))
            vcard = f"""BEGIN:VCARD
VERSION:3.0
N:{last_name};{first_name};;;
FN:{emp['name']}
TITLE:{title}
EMAIL:{emp['email']}
TEL;TYPE=CELL:{emp['mobile']}
END:VCARD"""
            
            qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
            qr.add_data(vcard)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            
            img_buffer = BytesIO()
            img.save(img_buffer, format='PNG')
            safe_name = emp['name'].replace(' ', '_').replace('/', '_')
            zip_file.writestr(f"{safe_name}-bizcard.png", img_buffer.getvalue())
    
    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name='employee_qr_codes.zip'
    )

@app.route('/static/<path:path>')
def send_static(path):
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    return send_from_directory('static', path)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5001)), debug=True)