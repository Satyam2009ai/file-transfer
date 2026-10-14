import os
import io
import base64
import time
import uuid
import zipfile
import threading
import random
from flask import Flask, request, jsonify, send_from_directory, render_template_string
from flask_socketio import SocketIO, emit
import qrcode
from PIL import Image

app = Flask(__name__)
app.config['SECRET_KEY'] = 'neon-ultimate-colorful'
# Cloud server (Render/Heroku) ke liye async_mode setup
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

app.config['MAX_CONTENT_LENGTH'] = 1000 * 1024 * 1024  # 1GB Max

# --- Server Folders Setup ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'vault_uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

SESSION_TOKEN = str(uuid.uuid4().hex)[:10] 
SECURITY_PIN = str(random.randint(1000, 9999)) 

CONNECTED_DEVICES = {} 
SHARED_FILES_DB = {}   

# ================= BACKGROUND THREAD (SELF-DESTRUCT) =================
def auto_destruct_worker():
    while True:
        time.sleep(5)
        current_time = time.time()
        expired = []
        for filepath, timestamp in SHARED_FILES_DB.items():
            # 60 Seconds timer (Cloud se delete karne ke liye)
            if current_time - timestamp > 60: 
                if os.path.exists(filepath):
                    os.remove(filepath)
                expired.append(filepath)
                
        for path in expired:
            del SHARED_FILES_DB[path]
            filename = os.path.basename(path)
            socketio.emit('terminal_log', {'msg': f"[SELF-DESTRUCT] 🔥 File vaporized from cloud: {filename}"})
            socketio.emit('file_destructed', {'filename': filename})
            socketio.emit('refresh_downloads', broadcast=True)

threading.Thread(target=auto_destruct_worker, daemon=True).start()

def make_safe_filename(filename):
    return filename.replace(" ", "_").replace("(", "").replace(")", "").replace("#", "").replace("%", "")

@app.after_request
def add_header(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

def generate_qr_base64(url, action):
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    fill_color = "#00f3ff" if action == 'receive' else "#ff6600"
    img = qr.make_image(fill_color=fill_color, back_color="#0a0a12")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

# ================= ROUTES =================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, mode='laptop', pin=SECURITY_PIN, token=SESSION_TOKEN)

@app.route('/auth')
def phone_view():
    token = request.args.get('token')
    action = request.args.get('action') 
    
    if token == SESSION_TOKEN:
        return render_template_string(HTML_TEMPLATE, mode='phone_auth', phone_action=action)
    return "<h1>403 FORBIDDEN - INVALID SECURITY TOKEN</h1>", 403

@app.route('/api/verify_pin', methods=['POST'])
def verify_pin():
    if request.json.get('pin') == SECURITY_PIN:
        return jsonify({'status': 'success'})
    return jsonify({'status': 'error'})

@app.route('/api/qr/<action>')
def get_qr(action):
    # Dynamic URL generation based on the website's host
    host_url = request.host_url.rstrip('/')
    url = f"{host_url}/auth?token={SESSION_TOKEN}&action={action}"
    return jsonify({'url': url, 'qr': generate_qr_base64(url, action)})

@app.route('/get_file/<path:filename>')
def get_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# Upload to Cloud
@app.route('/api/upload', methods=['POST'])
def handle_upload():
    files = request.files.getlist('files')
    if not files: return jsonify({'error': 'No files'}), 400

    if len(files) > 1:
        zip_filename = f"Vault_Archive_{int(time.time())}.zip"
        zip_path = os.path.join(UPLOAD_FOLDER, zip_filename)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file in files:
                safe_name = make_safe_filename(file.filename)
                zipf.writestr(safe_name, file.read())
                
        SHARED_FILES_DB[zip_path] = time.time()
        socketio.emit('terminal_log', {'msg': f"[AUTO-ZIP] Compressed {len(files)} files -> {zip_filename}"})
        socketio.emit('file_completed', {'filename': zip_filename, 'info': 'Zipped Batch'})
    
    else:
        file = files[0]
        safe_filename = make_safe_filename(file.filename)
        final_path = os.path.join(UPLOAD_FOLDER, safe_filename)
        file.save(final_path)
        SHARED_FILES_DB[final_path] = time.time()
        socketio.emit('terminal_log', {'msg': f"[CLOUD] File received: {safe_filename}"})
        socketio.emit('file_completed', {'filename': safe_filename, 'info': 'Cloud Hosted'})

    return jsonify({'status': 'success'})

@app.route('/api/share_to_phone', methods=['POST'])
def share_to_phone():
    files = request.files.getlist('files')
    for file in files:
        if file.filename:
            safe_filename = make_safe_filename(file.filename)
            save_path = os.path.join(UPLOAD_FOLDER, safe_filename)
            file.save(save_path)
            SHARED_FILES_DB[save_path] = time.time()
            socketio.emit('terminal_log', {'msg': f"[SHARE] Hosted new file for mobile: {safe_filename}"})
    
    socketio.emit('refresh_downloads', broadcast=True)
    return jsonify({'status': 'success'})

@app.route('/api/list_shared')
def list_shared():
    # Return files that are currently active in the database
    files = []
    for filepath in SHARED_FILES_DB.keys():
        if os.path.exists(filepath):
            files.append(os.path.basename(filepath))
    return jsonify({'files': files})

# Socket Events
@socketio.on('connect')
def handle_connect():
    ua = request.headers.get('User-Agent', 'Unknown Device')
    CONNECTED_DEVICES[request.sid] = "📱 Mobile Node" if "Mobile" in ua else "💻 Command Node"
    emit('update_radar', {'devices': list(CONNECTED_DEVICES.values())}, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in CONNECTED_DEVICES: del CONNECTED_DEVICES[request.sid]
    emit('update_radar', {'devices': list(CONNECTED_DEVICES.values())}, broadcast=True)

# HTML TEMPLATE KO SAME RAKHEIN, BAS `/get_file/<folder_key>/...` ko `/get_file/...` kar dein HTML mein
HTML_TEMPLATE = """
<!-- YAHA APNA PURANA WALA HTML TEMPLATE DAAL DEIN -->
<!-- Bus JavaScript mein `let fileURL = \`/get_file/${data.folder_key}/${safeFilename}\`;` 
     ko badal kar `let fileURL = \`/get_file/${safeFilename}\`;` kar dijiyega. -->
<!-- Aur share function mein bhi `/get_file/shared_out/` ko `/get_file/` kar dijiyega. -->
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"🔥 NEON CLOUD V7 ACTIVE ON PORT {port} 🔥")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)