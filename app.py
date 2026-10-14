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
socketio = SocketIO(app, cors_allowed_origins="*")

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
            if current_time - timestamp > 60:  # 60 seconds auto-delete
                if os.path.exists(filepath):
                    os.remove(filepath)
                expired.append(filepath)
                
        for path in expired:
            del SHARED_FILES_DB[path]
            filename = os.path.basename(path)
            socketio.emit('terminal_log', {'msg': f"[SELF-DESTRUCT] 🔥 File vaporized: {filename}"})
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
    # Auto-detect website URL
    host_url = request.host_url.rstrip('/')
    url = f"{host_url}/auth?token={SESSION_TOKEN}&action={action}"
    return jsonify({'url': url, 'qr': generate_qr_base64(url, action)})

@app.route('/get_file/<path:filename>')
def get_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

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
    files = [os.path.basename(f) for f in SHARED_FILES_DB.keys() if os.path.exists(f)]
    return jsonify({'files': files})

@socketio.on('connect')
def handle_connect():
    ua = request.headers.get('User-Agent', 'Unknown Device')
    CONNECTED_DEVICES[request.sid] = "📱 Mobile Node" if "Mobile" in ua else "💻 Command Node"
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
        body { background-color: var(--bg); color: var(--text); padding: 15px; display: flex; justify-content: center; }
        .box { background: linear-gradient(145deg, rgba(20,20,35,0.95), rgba(10,10,20,0.95)); padding: 20px; border-radius: 15px; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
        .box-cyan { border-top: 3px solid var(--cyan); box-shadow: 0 -5px 20px rgba(0,243,255,0.15); }
        .box-pink { border-top: 3px solid var(--pink); box-shadow: 0 -5px 20px rgba(255,0,127,0.15); }
        .box-orange { border-top: 3px solid var(--orange); box-shadow: 0 -5px 20px rgba(255,102,0,0.15); }
        .box-purple { border-top: 3px solid var(--purple); box-shadow: 0 -5px 20px rgba(157,0,255,0.15); }
        .grid-container { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; max-width: 1300px; width: 100%; }
        h2 { margin-bottom: 15px; font-size: 1.2rem; text-transform: uppercase; letter-spacing: 1px; text-shadow: 0 0 10px currentColor;}
        .pin-display { font-size: 2.8rem; letter-spacing: 12px; text-align: center; font-weight: 800; color: var(--cyan); text-shadow: 0 0 15px var(--cyan); margin: 5px 0;}
        .btn { background: linear-gradient(45deg, var(--purple), var(--pink)); color: var(--text); padding: 12px; border: none; border-radius: 8px; cursor: pointer; transition: 0.3s; width: 100%; text-transform: uppercase; font-weight: bold;}
        .btn:hover { filter: brightness(1.2); transform: scale(1.02); }
        .btn-view { background: var(--cyan); color: #000; border: none; padding: 5px 12px; border-radius: 5px; font-weight: bold; cursor: pointer; float: right; box-shadow: 0 0 10px var(--cyan); }
        .btn-view:hover { background: #fff; box-shadow: 0 0 15px #fff; }
        .terminal { background: #000; color: var(--green); height: 200px; overflow-y: auto; padding: 12px; font-family: 'Consolas', monospace; font-size: 0.85rem; border-radius: 8px; border: 1px solid #333;}
        .phone-container { width: 100%; max-width: 400px; margin: 20px auto; }
        .pin-input { font-size: 2rem; width: 180px; text-align: center; background: #111; color: var(--cyan); border: 2px solid var(--cyan); border-radius: 8px; padding: 10px; margin: 20px 0; outline: none; }
        .swipe-container { width: 100%; background: #222; border-radius: 50px; height: 60px; position: relative; margin-top: 20px; border: 2px solid var(--cyan); display: none;}
        .swipe-slider { width: 60px; height: 60px; background: linear-gradient(45deg, var(--cyan), var(--purple)); border-radius: 50px; position: absolute; left: 0; top: 0; display: flex; align-items: center; justify-content: center; cursor: pointer; z-index: 2; font-size: 1.5rem; transition: left 0.1s; box-shadow: 0 0 15px var(--cyan);}
        .swipe-text { position: absolute; width: 100%; line-height: 60px; color: #fff; font-weight: bold; z-index: 1; pointer-events: none; text-align: center; }
        .file-list-item { padding: 12px; background: rgba(0,0,0,0.5); border-radius: 8px; margin-bottom: 10px; border-left: 4px solid var(--cyan); transition: 0.3s;}
        
        /* Responsive CSS for Mobile */
        @media (max-width: 800px) {
            .grid-container { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    {% if mode == 'laptop' %}
    <div class="grid-container">
        <div style="display:flex; flex-direction:column; gap:20px;">
            <div class="box box-cyan">
                <h2 style="color:var(--cyan)">📥 RECEIVE FROM PHONE</h2>
                <div class="pin-display">{{ pin }}</div>
                <button class="btn" onclick="showQR('receive')" style="background:var(--cyan); color:#000;">SHOW RECEIVE QR</button>
                <div id="qr-receive-section" style="display:none; text-align:center; margin-top:15px;">
                    <img id="qr-receive-img" src="" width="140" style="border-radius:10px; box-shadow:0 0 15px var(--cyan);">
                </div>
            </div>
            <div class="box box-purple">
                <h2 style="color:var(--purple)">📡 LIVE RADAR</h2>
                <ul id="device-list" style="list-style:none; color:#ddd; font-size:0.95rem;">
                    <li>Scanning for devices...</li>
                </ul>
            </div>
        </div>

        <div style="display:flex; flex-direction:column; gap:20px;">
            <div class="box box-orange">
                <h2 style="color:var(--orange)">📤 SEND TO PHONE</h2>
                <input type="file" id="share-input" multiple style="display:none;" onchange="uploadToPhone()">
                <button class="btn" onclick="document.getElementById('share-input').click()" style="background: linear-gradient(45deg, var(--orange), var(--pink));">📁 CHOOSE FILE</button>
                <p id="share-status" style="margin-top:10px; color:var(--green); text-align:center; font-weight:bold; display:none;">Uploading...</p>
                <div id="qr-share-section" style="display:none; text-align:center; margin-top:15px;">
                    <img id="qr-share-img" src="" width="140" style="border-radius:10px; box-shadow:0 0 15px var(--orange);">
                </div>
            </div>
            <div class="box" style="flex-grow:1; border-top: 3px solid var(--green);">
                <h2 style="color:var(--green)">🛡️ SYSTEM LOGS</h2>
                <div id="terminal" class="terminal">
                    <p>[SYS] Cloud Routing Engine Online...</p>
                </div>
            </div>
        </div>

        <div class="box box-pink">
            <h2 style="color:var(--pink)">📥 CLOUD FILES</h2>
            <div id="file-list" style="height:450px; overflow-y:auto; padding-right:5px;">
                <p style="color:#555; font-style:italic;">No files received yet...</p>
            </div>
        </div>
    </div>

    <script>
        const socket = io();

        function showQR(action) {
            fetch('/api/qr/' + action).then(r=>r.json()).then(data => {
                if (action === 'receive') {
                    document.getElementById('qr-receive-img').src = data.qr;
                    document.getElementById('qr-receive-section').style.display = 'block';
                } else {
                    document.getElementById('qr-share-img').src = data.qr;
                    document.getElementById('qr-share-section').style.display = 'block';
                }
            });
        }

        function uploadToPhone() {
            let files = document.getElementById('share-input').files;
            if(!files.length) return;
            document.getElementById('share-status').style.display = 'block';
            document.getElementById('share-status').innerText = 'Uploading...';
            document.getElementById('qr-share-section').style.display = 'none'; 

            let fd = new FormData();
            for(let i=0; i<files.length; i++) fd.append('files', files[i]);

            fetch('/api/share_to_phone', { method: 'POST', body: fd })
            .then(() => {
                document.getElementById('share-status').innerText = '✅ Hosted successfully!';
                showQR('share'); 
                setTimeout(()=> document.getElementById('share-status').style.display = 'none', 3000);
            });
        }

        socket.on('update_radar', (data) => {
            let list = document.getElementById('device-list');
            list.innerHTML = data.devices.length ? '' : '<li>No nodes found...</li>';
            data.devices.forEach(d => list.innerHTML += `<li style="margin-bottom:5px;">🟢 ${d}</li>`);
        });

        socket.on('terminal_log', (data) => {
            let term = document.getElementById('terminal');
            term.innerHTML += `<p>[${new Date().toLocaleTimeString()}] ${data.msg}</p>`;
            term.scrollTop = term.scrollHeight;
        });

        socket.on('file_completed', (data) => {
            let list = document.getElementById('file-list');
            if(list.innerText.includes('No files')) list.innerHTML = '';
            
            let safeId = data.filename.replace(/[^a-zA-Z0-9]/g, "");
            let safeFilename = encodeURIComponent(data.filename);
            let fileURL = `/get_file/${safeFilename}`;
            
            list.innerHTML = `
            <div class="file-list-item" id="file-${safeId}">
                <button class="btn-view" onclick="window.location.href='${fileURL}'">GET</button>
                <div style="font-weight:bold; color:var(--cyan); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; width:70%;">${data.filename}</div>
            </div>` + list.innerHTML;
        });

        socket.on('file_destructed', (data) => {
            let safeId = data.filename.replace(/[^a-zA-Z0-9]/g, "");
            let el = document.getElementById(`file-${safeId}`);
            if(el) el.innerHTML = `<span style="color:var(--pink); text-decoration:line-through; font-weight:bold;">${data.filename} (DESTROYED)</span>`;
        });
    </script>

    {% elif mode == 'phone_auth' %}
    <div style="text-align:center;">
        <div id="step-1" class="phone-container box box-cyan">
            <h1 style="color:var(--cyan);">CYBER VAULT</h1>
            <input type="number" id="pin-input" class="pin-input" placeholder="****">
            <button class="btn" onclick="verifyPin()">UNLOCK SYSTEM</button>
        </div>

        <div id="step-2" style="display:none; padding-bottom:50px;">
            {% if phone_action == 'receive' %}
            <div class="phone-container box box-pink">
                <h2 style="color:var(--pink)">📤 SEND TO LAPTOP</h2>
                <input type="file" id="file-input" multiple style="display:none;" onchange="filesSelected()">
                <div style="background:rgba(0,0,0,0.5); padding:30px; border-radius:12px; border:2px dashed var(--pink); cursor:pointer;" onclick="document.getElementById('file-input').click()">
                    <h1 style="font-size:3rem; margin:0;">📁</h1>
                    <p style="color:#aaa; margin-top:10px;">Tap to select files</p>
                </div>
                <h4 id="selection-text" style="color:var(--cyan); margin-top:15px;"></h4>
                <div class="swipe-container" id="swipe-box">
                    <div class="swipe-text" id="swipe-text">SWIPE TO SEND >>></div>
                    <div class="swipe-slider" id="slider">🚀</div>
                </div>
                <p id="prog-text" style="color:var(--green); font-weight:bold; margin-top:15px; display:none;"></p>
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
                body: JSON.stringify({pin: pin})
            }).then(r=>r.json()).then(data => {
                if(data.status === 'success') {
                    document.getElementById('step-1').style.display = 'none';
                    document.getElementById('step-2').style.display = 'block';
                    if(actionType === 'share') fetchSharedFiles(); 
                } else alert("INVALID PIN.");
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
                document.getElementById('swipe-text').innerText = "SENDING...";
                slider.style.left = (swipeBox.clientWidth - slider.clientWidth) + 'px';
                setTimeout(uploadFiles, 400);
            }

            function uploadFiles() {
                swipeBox.style.display = 'none';
                let pt = document.getElementById('prog-text');
                pt.style.display = 'block';
                let formData = new FormData();
                let files = document.getElementById('file-input').files;
                for(let i=0; i<files.length; i++) formData.append('files', files[i]);

                let xhr = new XMLHttpRequest();
                xhr.open('POST', '/api/upload', true);
                xhr.onload = () => {
                    pt.innerText = "✅ TRANSFER SECURED!";
                    setTimeout(() => { pt.style.display = 'none'; document.getElementById('file-input').value = ""; document.getElementById('selection-text').innerText = "";}, 3000);
                };
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
                        <a href="${url}" download class="btn-view" style="background:var(--orange); text-decoration:none;">GET</a>
                        <div style="font-weight:bold; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; width:75%;">${f}</div>
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
