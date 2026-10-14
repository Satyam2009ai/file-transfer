import os
import io
import base64
import time
import uuid
import zipfile
import threading
import random
import traceback
from flask import Flask, request, jsonify, send_from_directory, render_template_string
from flask_socketio import SocketIO, emit
import qrcode
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = 'neon-ultimate-vault-v2'
# Allow cross origin and use standard polling/websocket
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

app.config['MAX_CONTENT_LENGTH'] = 1000 * 1024 * 1024  # 1GB Max

# --- Server Folders Setup ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'vault_uploads')
TEMP_FOLDER = os.path.join(BASE_DIR, 'vault_temp')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# --- SESSION & PIN VARIABLES ---
SESSION_TOKEN = str(uuid.uuid4().hex)[:10]

# RECEIVE VARIABLES (Phone -> Laptop)
RECEIVE_PIN = ""
FILES_FOR_LAPTOP = {}

# SEND VARIABLES (Laptop -> Phone)
SEND_PIN = ""
FILES_FOR_PHONE = {}

# ================= BACKGROUND THREAD (SELF-DESTRUCT) =================
def auto_destruct_worker():
    while True:
        time.sleep(5)
        current_time = time.time()
        
        # 1. Clean LAPTOP RECEIVE Files
        for filepath, timestamp in list(FILES_FOR_LAPTOP.items()):
            if current_time - timestamp > 60:  # 60 seconds auto-delete
                if os.path.exists(filepath): 
                    try: os.remove(filepath)
                    except: pass
                if filepath in FILES_FOR_LAPTOP:
                    del FILES_FOR_LAPTOP[filepath]
                # Update frontend
                socketio.emit('file_destructed', {'filename': os.path.basename(filepath)})
                
        # 2. Clean PHONE DOWNLOAD Files
        for filepath, timestamp in list(FILES_FOR_PHONE.items()):
            if current_time - timestamp > 60:  # 60 seconds auto-delete
                if os.path.exists(filepath): 
                    try: os.remove(filepath)
                    except: pass
                if filepath in FILES_FOR_PHONE:
                    del FILES_FOR_PHONE[filepath]
                # Update frontend
                socketio.emit('refresh_downloads')

threading.Thread(target=auto_destruct_worker, daemon=True).start()

def get_safe_filename(filename):
    safe = secure_filename(filename)
    if not safe:
        safe = f"file_{int(time.time())}.dat"
    return safe

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
    return render_template_string(HTML_TEMPLATE, mode='laptop')

@app.route('/auth')
def phone_view():
    token = request.args.get('token')
    action = request.args.get('action') 
    if token == SESSION_TOKEN:
        return render_template_string(HTML_TEMPLATE, mode='phone_auth', phone_action=action)
    return "<h1>403 FORBIDDEN - INVALID SECURITY TOKEN (PLEASE SCAN LATEST QR)</h1>", 403

@app.route('/api/verify_pin', methods=['POST'])
def verify_pin():
    data = request.json
    action = data.get('action')
    pin = data.get('pin')
    
    if action == 'receive' and pin == RECEIVE_PIN:
        return jsonify({'status': 'success'})
    elif action == 'share' and pin == SEND_PIN:
        return jsonify({'status': 'success'})
        
    return jsonify({'status': 'error'})

@app.route('/api/qr/<action>')
def get_qr(action):
    global RECEIVE_PIN, SEND_PIN
    reset = request.args.get('reset')
    
    if reset == '1':
        if action == 'receive':
            RECEIVE_PIN = str(random.randint(1000, 9999))
            FILES_FOR_LAPTOP.clear()
            socketio.emit('clear_files')
        elif action == 'share':
            SEND_PIN = str(random.randint(1000, 9999))
            FILES_FOR_PHONE.clear()
            
    current_pin = RECEIVE_PIN if action == 'receive' else SEND_PIN
    host_url = request.host_url.rstrip('/')
    url = f"{host_url}/auth?token={SESSION_TOKEN}&action={action}"
    
    return jsonify({'url': url, 'qr': generate_qr_base64(url, action), 'pin': current_pin})

@app.route('/get_file/<path:filename>')
def get_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/api/upload', methods=['POST'])
def handle_upload():
    # PHONE -> LAPTOP (Receive)
    try:
        files = request.files.getlist('files')
        if not files or files[0].filename == '': 
            return jsonify({'error': 'No files', 'msg': 'No files provided'}), 400

        if len(files) > 1:
            zip_filename = f"Vault_Archive_{int(time.time())}.zip"
            zip_path = os.path.join(UPLOAD_FOLDER, zip_filename)
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file in files:
                    if file.filename:
                        safe_name = get_safe_filename(file.filename)
                        temp_path = os.path.join(TEMP_FOLDER, safe_name)
                        file.save(temp_path)
                        zipf.write(temp_path, arcname=safe_name)
                        os.remove(temp_path)
            FILES_FOR_LAPTOP[zip_path] = time.time()
            socketio.emit('file_completed', {'filename': zip_filename})
        else:
            file = files[0]
            if file.filename:
                safe_filename = get_safe_filename(file.filename)
                final_path = os.path.join(UPLOAD_FOLDER, safe_filename)
                file.save(final_path)
                FILES_FOR_LAPTOP[final_path] = time.time()
                socketio.emit('file_completed', {'filename': safe_filename})

        return jsonify({'status': 'success'})
    except Exception as e:
        print("Upload Error:", traceback.format_exc())
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/share_to_phone', methods=['POST'])
def share_to_phone():
    # LAPTOP -> PHONE (Send)
    try:
        global SEND_PIN
        
        for filepath in list(FILES_FOR_PHONE.keys()):
            if os.path.exists(filepath):
                try: os.remove(filepath)
                except: pass
        FILES_FOR_PHONE.clear()
        
        files = request.files.getlist('files')
        if not files or files[0].filename == '':
            return jsonify({'status': 'error', 'msg': 'No files found'}), 400

        if len(files) > 1:
            zip_filename = f"Vault_Archive_{int(time.time())}.zip"
            zip_path = os.path.join(UPLOAD_FOLDER, zip_filename)
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file in files:
                    if file.filename:
                        safe_name = get_safe_filename(file.filename)
                        temp_path = os.path.join(TEMP_FOLDER, safe_name)
                        file.save(temp_path)
                        zipf.write(temp_path, arcname=safe_name)
                        os.remove(temp_path)
            FILES_FOR_PHONE[zip_path] = time.time()
        else:
            file = files[0]
            if file.filename:
                safe_filename = get_safe_filename(file.filename)
                save_path = os.path.join(UPLOAD_FOLDER, safe_filename)
                file.save(save_path)
                FILES_FOR_PHONE[save_path] = time.time()
                
        # Generate new PIN and QR
        SEND_PIN = str(random.randint(1000, 9999))
        host_url = request.host_url.rstrip('/')
        url = f"{host_url}/auth?token={SESSION_TOKEN}&action=share"
        qr_b64 = generate_qr_base64(url, 'share')
        
        socketio.emit('refresh_downloads')
        return jsonify({'status': 'success', 'pin': SEND_PIN, 'qr': qr_b64})
    
    except Exception as e:
        print("Share Error:", traceback.format_exc())
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/list_shared')
def list_shared():
    files = [os.path.basename(f) for f in FILES_FOR_PHONE.keys() if os.path.exists(f)]
    return jsonify({'files': files})

@socketio.on('connect')
def handle_connect():
    ua = request.headers.get('User-Agent', 'Unknown Device')
    CONNECTED_DEVICES[request.sid] = "📱 Mobile" if "Mobile" in ua else "💻 Laptop"
    emit('update_radar', {'devices': list(CONNECTED_DEVICES.values())}, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in CONNECTED_DEVICES: del CONNECTED_DEVICES[request.sid]
    emit('update_radar', {'devices': list(CONNECTED_DEVICES.values())}, broadcast=True)

# ================= HTML / JS TEMPLATE =================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🌈 NEON CLOUD VAULT 🌈</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.5.4/socket.io.js"></script>
    <style>
        :root { --bg: #090914; --cyan: #00f3ff; --pink: #ff007f; --green: #00ff66; --orange: #ff6600; --purple: #9d00ff; --text: #ffffff; }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', sans-serif; }
        body { background-color: var(--bg); color: var(--text); padding: 15px; display: flex; justify-content: center; align-items: center; min-height: 100vh;}
        
        .box { background: linear-gradient(145deg, rgba(20,20,35,0.95), rgba(10,10,20,0.95)); padding: 20px; border-radius: 15px; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 8px 32px rgba(0,0,0,0.5); position: relative;}
        .box-cyan { border-top: 3px solid var(--cyan); box-shadow: 0 -5px 20px rgba(0,243,255,0.15); }
        .box-pink { border-top: 3px solid var(--pink); box-shadow: 0 -5px 20px rgba(255,0,127,0.15); }
        .box-orange { border-top: 3px solid var(--orange); box-shadow: 0 -5px 20px rgba(255,102,0,0.15); }
        .box-purple { border-top: 3px solid var(--purple); box-shadow: 0 -5px 20px rgba(157,0,255,0.15); }
        
        .grid-container { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; max-width: 1000px; width: 100%; }
        h2 { margin-bottom: 15px; font-size: 1.2rem; text-transform: uppercase; letter-spacing: 1px; text-shadow: 0 0 10px currentColor;}
        .pin-display { font-size: 3.5rem; letter-spacing: 15px; text-align: center; font-weight: 900; color: var(--text); text-shadow: 0 0 20px currentColor; margin: 15px 0;}
        
        .btn { background: linear-gradient(45deg, var(--purple), var(--pink)); color: var(--text); padding: 15px; border: none; border-radius: 8px; cursor: pointer; transition: 0.3s; width: 100%; text-transform: uppercase; font-weight: bold; font-size: 1.1rem;}
        .btn:hover { filter: brightness(1.2); transform: scale(1.02); }
        
        .btn-action { color: #000; border: none; padding: 8px 15px; border-radius: 5px; font-weight: bold; cursor: pointer; text-decoration: none; font-size: 0.9rem; text-align: center; display: inline-block; transition: 0.2s;}
        .btn-view { background: var(--cyan); box-shadow: 0 0 10px var(--cyan); }
        .btn-down { background: var(--green); box-shadow: 0 0 10px var(--green); }
        .btn-action:hover { background: #fff; box-shadow: 0 0 15px #fff; transform: scale(1.05); }

        .home-container { display: flex; gap: 50px; justify-content: center; align-items: center; flex-wrap: wrap; }
        .big-circle { width: 250px; height: 250px; border-radius: 50%; display: flex; flex-direction: column; justify-content: center; align-items: center; cursor: pointer; transition: 0.4s; background: #111; border: 4px solid rgba(255,255,255,0.1); box-shadow: 0 0 30px rgba(0,0,0,0.5); }
        .big-circle h1 { font-size: 5rem; margin-bottom: 10px; text-shadow: 0 0 20px currentColor;}
        .big-circle h2 { font-size: 1.8rem; font-weight: 900; text-shadow: 0 0 10px currentColor; color: #fff;}
        .circle-receive:hover { border-color: var(--cyan); box-shadow: 0 0 50px var(--cyan); transform: scale(1.05); }
        .circle-receive:hover h2 { color: var(--cyan); }
        .circle-send:hover { border-color: var(--orange); box-shadow: 0 0 50px var(--orange); transform: scale(1.05); }
        .circle-send:hover h2 { color: var(--orange); }

        .progress-circle { width: 120px; height: 120px; border-radius: 50%; background: conic-gradient(var(--orange) 0%, #222 0%); display: flex; align-items: center; justify-content: center; margin: 20px auto; box-shadow: 0 0 15px rgba(255, 102, 0, 0.4); }
        .progress-circle-inner { width: 100px; height: 100px; border-radius: 50%; background: var(--bg); display: flex; align-items: center; justify-content: center; font-size: 1.8rem; font-weight: bold; color: var(--orange); text-shadow: 0 0 10px var(--orange); }
        
        .file-list-item { padding: 15px; background: rgba(0,0,0,0.5); border-radius: 8px; margin-bottom: 10px; border-left: 4px solid var(--cyan); transition: 0.3s; display: flex; justify-content: space-between; align-items: center;}
        
        @keyframes blink { 0% { opacity: 1; } 50% { opacity: 0.3; } 100% { opacity: 1; } }
        .blinking-text { animation: blink 1.5s infinite; color: var(--orange); text-align: center; margin-top: 15px; font-weight: bold; font-size: 1.2rem;}
        
        .modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 1000; display: none; align-items: center; justify-content: center; backdrop-filter: blur(5px); }
        .modal-content { background: #111; padding: 40px; border-radius: 20px; border: 2px solid var(--cyan); text-align: center; box-shadow: 0 0 30px var(--cyan); animation: popIn 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275); }
        @keyframes popIn { 0% { transform: scale(0.5); opacity: 0; } 100% { transform: scale(1); opacity: 1; } }

        @media (max-width: 800px) { .grid-container { grid-template-columns: 1fr; } }
    </style>
</head>
<body>

    <div class="modal-overlay" id="success-modal">
        <div class="modal-content">
            <h1 style="font-size: 4rem; margin-bottom: 10px;">🚀</h1>
            <h2 style="color: var(--cyan); margin-bottom: 10px;">Transfer Complete!</h2>
            <p style="color: #aaa;">Your files are securely hosted.</p>
            <button class="btn" style="margin-top:20px; background:var(--cyan); color:#000;" onclick="document.getElementById('success-modal').style.display='none'">Awesome</button>
        </div>
    </div>

    {% if mode == 'laptop' %}
    
    <div id="home-view" class="home-container">
        <div class="big-circle circle-receive" onclick="openReceive()">
            <h1 style="color:var(--cyan);">📥</h1>
            <h2>RECEIVE</h2>
        </div>
        <div class="big-circle circle-send" onclick="openSend()">
            <h1 style="color:var(--orange);">📤</h1>
            <h2>SEND</h2>
        </div>
    </div>

    <div id="receive-view" style="display:none; width:100%; max-width:1200px;">
        <button class="btn-action" style="background:#444; color:#fff; margin-bottom: 20px;" onclick="goHome()">🔙 MAIN MENU</button>
        <div class="grid-container">
            <div class="box box-cyan">
                <h2 style="color:var(--cyan); text-align:center;">SCAN TO SEND FILES TO LAPTOP</h2>
                <div class="pin-display" id="receive-pin" style="color:var(--cyan);">****</div>
                
                <div id="qr-receive-section" style="text-align:center; margin-top:15px;">
                    <img id="qr-receive-img" src="" width="180" style="border-radius:10px; box-shadow:0 0 20px var(--cyan);">
                    
                    <div style="display:flex; gap:10px; justify-content:center; margin-top:15px;">
                        <button class="btn-action btn-down" onclick="downloadQR('qr-receive-img', 'Receive')">⬇️ SAVE</button>
                        <button class="btn-action btn-view" onclick="shareQR('qr-receive-img')">🔗 SHARE</button>
                    </div>
                </div>
            </div>
            
            <div style="display:flex; flex-direction:column; gap:20px;">
                <div class="box box-purple">
                    <h2 style="color:var(--purple)">📡 CONNECTED RADAR</h2>
                    <ul id="device-list" style="list-style:none; color:#ddd; font-size:0.95rem;"><li>Scanning...</li></ul>
                </div>
                <div class="box box-pink" style="flex-grow:1;">
                    <h2 style="color:var(--pink)">📥 CLOUD FILES RECEIVED</h2>
                    <div id="file-list" style="height:250px; overflow-y:auto; padding-right:5px;">
                        <p style="color:#555; font-style:italic;">No files yet...</p>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div id="send-view" style="display:none; width:100%; max-width:600px; margin:auto;">
        <button class="btn-action" style="background:#444; color:#fff; margin-bottom: 20px;" onclick="goHome()">🔙 MAIN MENU</button>
        <div class="box box-orange">
            <h2 style="color:var(--orange); text-align:center;">UPLOAD FILE TO GENERATE QR</h2>
            
            <input type="file" id="share-input" multiple style="display:none;" onchange="uploadToPhone()">
            <button class="btn" id="choose-file-btn" onclick="document.getElementById('share-input').click()" style="background: linear-gradient(45deg, var(--orange), var(--pink)); font-size: 1.5rem; padding: 20px;">📁 SELECT FILE TO SHARE</button>
            
            <div id="processing-state" style="display:none; text-align:center; margin-top: 20px;">
                <div class="blinking-text">⏳ WAIT ON THE PROCESS...</div>
                <div id="laptop-progress" class="progress-circle">
                    <div class="progress-circle-inner" id="laptop-progress-text">0%</div>
                </div>
            </div>

            <div id="qr-share-section" style="display:none; text-align:center; margin-top:30px;">
                <h3 style="color:#fff; margin-bottom:5px;">File Ready! Scan & Enter This PIN:</h3>
                
                <div class="pin-display" id="send-pin" style="color:var(--orange);">****</div>
                
                <img id="qr-share-img" src="" width="200" style="border-radius:10px; box-shadow:0 0 20px var(--orange); margin-top: 10px;">
                
                <div style="display:flex; gap:10px; justify-content:center; margin-top:20px;">
                    <button class="btn-action btn-down" style="padding:10px 20px;" onclick="downloadQR('qr-share-img', 'Share')">⬇️ SAVE QR</button>
                    <button class="btn-action btn-view" style="padding:10px 20px;" onclick="shareQR('qr-share-img')">🔗 SHARE QR</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        const socket = io();

        socket.on('clear_files', () => {
            document.getElementById('file-list').innerHTML = '<p style="color:#555; font-style:italic;">No files yet...</p>';
        });

        function goHome() {
            document.getElementById('home-view').style.display = 'flex';
            document.getElementById('receive-view').style.display = 'none';
            document.getElementById('send-view').style.display = 'none';
        }

        function openReceive() {
            document.getElementById('home-view').style.display = 'none';
            document.getElementById('receive-view').style.display = 'block';
            
            fetch('/api/qr/receive?reset=1').then(r=>r.json()).then(data => {
                document.getElementById('receive-pin').innerText = data.pin;
                document.getElementById('qr-receive-img').src = data.qr;
            });
        }

        function openSend() {
            document.getElementById('home-view').style.display = 'none';
            document.getElementById('send-view').style.display = 'block';
            resetSendProcess();
            fetch('/api/qr/share?reset=1'); 
        }

        function resetSendProcess() {
            document.getElementById('choose-file-btn').style.display = 'block';
            document.getElementById('processing-state').style.display = 'none';
            document.getElementById('qr-share-section').style.display = 'none';
            document.getElementById('share-input').value = ""; 
        }

        function downloadQR(imgId, typeName) {
            let imgUrl = document.getElementById(imgId).src;
            let a = document.createElement('a');
            a.href = imgUrl;
            a.download = `Vault_${typeName}_QR.png`;
            a.click();
        }

        async function shareQR(imgId) {
            let imgUrl = document.getElementById(imgId).src;
            try {
                let res = await fetch(imgUrl);
                let blob = await res.blob();
                let file = new File([blob], "Vault_QR.png", {type: "image/png"});
                
                if (navigator.share) {
                    await navigator.share({ title: 'Vault QR Code', files: [file] });
                } else {
                    alert("Sharing is not supported on this browser.");
                }
            } catch (err) { alert("Error sharing the QR code."); }
        }

        function uploadToPhone() {
            let files = document.getElementById('share-input').files;
            if(!files.length) return;

            let btn = document.getElementById('choose-file-btn');
            let processState = document.getElementById('processing-state');
            let progressContainer = document.getElementById('laptop-progress');
            let progressText = document.getElementById('laptop-progress-text');
            
            btn.style.display = 'none';
            document.getElementById('qr-share-section').style.display = 'none'; 
            processState.style.display = 'block'; 

            let fd = new FormData();
            for(let i=0; i<files.length; i++) fd.append('files', files[i]);

            let xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/share_to_phone', true);
            
            xhr.upload.onprogress = function(e) {
                if (e.lengthComputable) {
                    let percent = Math.round((e.loaded / e.total) * 100);
                    progressText.innerText = percent + "%";
                    progressContainer.style.background = `conic-gradient(var(--orange) ${percent}%, #222 ${percent}%)`;
                }
            };

            xhr.onload = function() {
                try {
                    if (xhr.status !== 200) {
                        let errorMsg = "Unknown Server Error";
                        try {
                            let res = JSON.parse(xhr.responseText);
                            errorMsg = res.msg || errorMsg;
                        } catch(e) {}
                        alert("Upload failed! Status: " + xhr.status + "\\nReason: " + errorMsg);
                        resetSendProcess();
                        return;
                    }
                    
                    let res = JSON.parse(xhr.responseText);
                    if (res.status === 'success') {
                        processState.style.display = 'none';
                        progressText.innerText = "0%";
                        progressContainer.style.background = `conic-gradient(var(--orange) 0%, #222 0%)`;
                        btn.style.display = 'block';
                        
                        document.getElementById('send-pin').innerText = res.pin;
                        document.getElementById('qr-share-img').src = res.qr;
                        document.getElementById('qr-share-section').style.display = 'block';
                        
                        document.getElementById('success-modal').style.display = 'flex';
                        document.getElementById('share-input').value = ""; 
                    }
                } catch(err) {
                    alert("Processing Error! Try again.");
                    resetSendProcess();
                }
            };
            
            xhr.onerror = function() {
                alert("Network Error occurred!");
                resetSendProcess();
            };
            
            xhr.send(fd);
        }

        socket.on('update_radar', (data) => {
            let list = document.getElementById('device-list');
            list.innerHTML = data.devices.length ? '' : '<li>No nodes found...</li>';
            data.devices.forEach(d => list.innerHTML += `<li style="margin-bottom:5px;">🟢 ${d}</li>`);
        });

        socket.on('file_completed', (data) => {
            let list = document.getElementById('file-list');
            if(list.innerText.includes('No files')) list.innerHTML = '';
            
            let safeId = data.filename.replace(/[^a-zA-Z0-9]/g, "");
            let safeFilename = encodeURIComponent(data.filename);
            let fileURL = `/get_file/${safeFilename}`;
            
            list.innerHTML = `
            <div class="file-list-item" id="file-${safeId}">
                <div style="font-weight:bold; color:var(--cyan); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; width:50%;" title="${data.filename}">${data.filename}</div>
                <div style="display:flex; gap:8px;">
                    <button class="btn-action btn-view" onclick="window.open('${fileURL}', '_blank')">VIEW</button>
                    <a href="${fileURL}" download class="btn-action btn-down">SAVE</a>
                </div>
            </div>` + list.innerHTML;
        });

        socket.on('file_destructed', (data) => {
            let safeId = data.filename.replace(/[^a-zA-Z0-9]/g, "");
            let el = document.getElementById(`file-${safeId}`);
            if(el) el.innerHTML = `<span style="color:var(--pink); text-decoration:line-through; font-weight:bold; width:100%; text-align:center;">${data.filename} (DESTROYED)</span>`;
        });
    </script>

    {% elif mode == 'phone_auth' %}
    <style>
        .phone-container { width: 100%; max-width: 400px; margin: 20px auto; }
        .pin-input { font-size: 2rem; width: 180px; text-align: center; background: #111; color: var(--cyan); border: 2px solid var(--cyan); border-radius: 8px; padding: 10px; margin: 20px 0; outline: none; }
        .swipe-container { width: 100%; background: #222; border-radius: 50px; height: 60px; position: relative; margin-top: 20px; border: 2px solid var(--cyan); display: none;}
        .swipe-slider { width: 60px; height: 60px; background: linear-gradient(45deg, var(--cyan), var(--purple)); border-radius: 50px; position: absolute; left: 0; top: 0; display: flex; align-items: center; justify-content: center; cursor: pointer; z-index: 2; font-size: 1.5rem; transition: left 0.1s; box-shadow: 0 0 15px var(--cyan);}
        .swipe-text { position: absolute; width: 100%; line-height: 60px; color: #fff; font-weight: bold; z-index: 1; pointer-events: none; text-align: center; }
    </style>
    <div style="text-align:center; width:100%;">
        <div id="step-1" class="phone-container box box-cyan">
            <h1 style="color:var(--cyan);">CYBER VAULT</h1>
            <p style="color:#aaa; font-size: 0.9rem; margin-top:10px;">Enter the PIN shown on the Laptop</p>
            <input type="number" id="pin-input" class="pin-input" placeholder="****">
            <button class="btn" onclick="verifyPin()">UNLOCK SYSTEM</button>
        </div>

        <div id="step-2" style="display:none; padding-bottom:50px; width:100%;">
            {% if phone_action == 'receive' %}
            <div class="phone-container box box-pink">
                <h2 style="color:var(--pink)">📤 SEND TO LAPTOP</h2>
                <input type="file" id="file-input" multiple style="display:none;" onchange="filesSelected()">
                
                <div id="upload-ui">
                    <div style="background:rgba(0,0,0,0.5); padding:30px; border-radius:12px; border:2px dashed var(--pink); cursor:pointer;" onclick="document.getElementById('file-input').click()">
                        <h1 style="font-size:3rem; margin:0;">📁</h1>
                        <p style="color:#aaa; margin-top:10px;">Tap to select files</p>
                    </div>
                    <h4 id="selection-text" style="color:var(--cyan); margin-top:15px;"></h4>
                    <div class="swipe-container" id="swipe-box">
                        <div class="swipe-text" id="swipe-text">SWIPE TO SEND >>></div>
                        <div class="swipe-slider" id="slider">🚀</div>
                    </div>
                </div>

                <div id="progress-container" class="progress-circle" style="display:none;">
                    <div class="progress-circle-inner" id="progress-text">0%</div>
                </div>
            </div>
            {% endif %}

            {% if phone_action == 'share' %}
            <div class="phone-container box box-orange">
                <h2 style="color:var(--orange)">📥 DOWNLOAD FILES</h2>
                <button class="btn" style="background:var(--cyan); color:#000; margin-bottom:15px; width:auto; padding:5px 15px;" onclick="fetchSharedFiles()">🔄 REFRESH</button>
                <div id="download-list" style="text-align:left;">
                    <p style="color:#555; text-align:center;">Empty...</p>
                </div>
            </div>
            {% endif %}
        </div>
    </div>

    <script>
        const socket = io();
        const actionType = "{{ phone_action }}"; 

        function verifyPin() {
            let pin = document.getElementById('pin-input').value;
            fetch('/api/verify_pin', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({pin: pin, action: actionType}) 
            }).then(r=>r.json()).then(data => {
                if(data.status === 'success') {
                    document.getElementById('step-1').style.display = 'none';
                    document.getElementById('step-2').style.display = 'block';
                    if(actionType === 'share') fetchSharedFiles(); 
                } else alert("INCORRECT PIN OR EXPIRED. PLEASE CHECK THE LAPTOP SCREEN.");
            });
        }

        if(actionType === 'receive') {
            let slider = document.getElementById('slider');
            let swipeBox = document.getElementById('swipe-box');
            let isDragging = false, startX = 0;

            window.filesSelected = function() {
                let files = document.getElementById('file-input').files;
                if(files.length > 0) {
                    document.getElementById('selection-text').innerText = `${files.length} File(s) Selected`;
                    swipeBox.style.display = 'block';
                    slider.style.left = '0px';
                    document.getElementById('swipe-text').innerText = "SWIPE TO SEND >>>";
                }
            }

            slider.addEventListener('touchstart', (e) => { isDragging = true; startX = e.touches[0].clientX; });
            slider.addEventListener('touchmove', (e) => {
                if(!isDragging) return;
                let move = e.touches[0].clientX - startX;
                let maxMove = swipeBox.clientWidth - slider.clientWidth;
                if(move > 0 && move <= maxMove) slider.style.left = move + 'px';
                if(move >= maxMove - 10) { isDragging = false; triggerUpload(); }
            });
            slider.addEventListener('touchend', () => { if(isDragging) { slider.style.left = '0px'; isDragging = false; }});

            function triggerUpload() {
                document.getElementById('upload-ui').style.display = 'none';
                let progressContainer = document.getElementById('progress-container');
                progressContainer.style.display = 'flex';
                uploadFiles();
            }

            function uploadFiles() {
                let formData = new FormData();
                let files = document.getElementById('file-input').files;
                for(let i=0; i<files.length; i++) formData.append('files', files[i]);

                let xhr = new XMLHttpRequest();
                xhr.open('POST', '/api/upload', true);
                
                xhr.upload.onprogress = function(e) {
                    if (e.lengthComputable) {
                        let percent = Math.round((e.loaded / e.total) * 100);
                        document.getElementById('progress-text').innerText = percent + "%";
                        document.getElementById('progress-container').style.background = `conic-gradient(var(--cyan) ${percent}%, #222 ${percent}%)`;
                    }
                };

                xhr.onload = () => {
                    try {
                        if(xhr.status !== 200) {
                            let errorMsg = "Unknown Error";
                            try {
                                let res = JSON.parse(xhr.responseText);
                                errorMsg = res.msg || errorMsg;
                            } catch(e) {}
                            throw new Error(errorMsg);
                        }
                        alert("Files Sent to Laptop Successfully! 🚀");
                        document.getElementById('progress-container').style.display = 'none';
                        document.getElementById('progress-text').innerText = "0%";
                        document.getElementById('progress-container').style.background = `conic-gradient(var(--cyan) 0%, #222 0%)`;
                        document.getElementById('upload-ui').style.display = 'block';
                        document.getElementById('file-input').value = ""; 
                        document.getElementById('selection-text').innerText = "";
                        swipeBox.style.display = 'none';
                    } catch(e) {
                        alert("Error uploading files! \\nReason: " + e.message);
                        document.getElementById('progress-container').style.display = 'none';
                        document.getElementById('upload-ui').style.display = 'block';
                    }
                };
                
                xhr.onerror = () => {
                    alert("Network Error!");
                    document.getElementById('progress-container').style.display = 'none';
                    document.getElementById('upload-ui').style.display = 'block';
                }
                xhr.send(formData);
            }
        }

        window.fetchSharedFiles = function() {
            fetch('/api/list_shared')
            .then(r=>r.json()).then(data => {
                let list = document.getElementById('download-list');
                if(data.files.length === 0) { list.innerHTML = '<p style="color:#555; text-align:center;">Empty...</p>'; return; }
                list.innerHTML = '';
                data.files.forEach(f => {
                    let safeF = encodeURIComponent(f);
                    let url = `/get_file/${safeF}`;
                    list.innerHTML += `
                    <div class="file-list-item" style="border-left-color:var(--orange);">
                        <div style="font-weight:bold; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; width:50%;" title="${f}">${f}</div>
                        <div style="display:flex; gap:5px;">
                            <button class="btn-action btn-view" onclick="window.open('${url}', '_blank')">VIEW</button>
                            <a href="${url}" download class="btn-action btn-down" style="background:var(--orange); box-shadow: 0 0 10px var(--orange);">SAVE</a>
                        </div>
                    </div>`;
                });
            });
        }
        
        socket.on('refresh_downloads', () => { if(actionType === 'share') fetchSharedFiles(); });
    </script>
    {% endif %}
</body>
</html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
