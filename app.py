import glob
import json
import os
import tempfile
import paramiko
import re
import subprocess
import platform
import urllib.request
import urllib.parse
import urllib.error
import base64
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

app = Flask(__name__)
MODEL_DIR = "phone_models"
TEMP_DIR = tempfile.gettempdir()
ENDPOINT_FILE = "endpoints.json"

def cleanup_json_at_startup(wipe_inventory=False):
    # 1. Delete all phone model JSON files
    if os.path.exists(MODEL_DIR):
        for file in glob.glob(f"{MODEL_DIR}/*.json"):
            os.remove(file)
            print(f"Deleted startup file: {file}")

    # 2. Delete the saved endpoints inventory (if wipe_inventory is True)
    if wipe_inventory and os.path.exists(ENDPOINT_FILE):
        os.remove(ENDPOINT_FILE)
        print(f"Deleted startup file: {ENDPOINT_FILE}")

# Call the cleanup before creating the defaults
cleanup_json_at_startup(wipe_inventory=False) # Change to True if you want to wipe saved phones too

# --- Initialization Logic (Same as original) ---
def initialize_models():
    if not os.path.exists(MODEL_DIR): os.makedirs(MODEL_DIR)
    # Generic Model
    model_generic = {
        "model": "generic", "description": "Global SIP & Network Settings",
        "settings": [
            {"key": "dhcp", "label": "DHCP (1=On, 0=Off)", "type": "choice", "options": ["1", "0"]},
            {"key": "tftp server", "label": "TFTP Server IP Address", "type": "string"},
            {"key": "sip line1 screen name", "label": "Screen Label (Line 1)", "type": "string"},
            {"key": "sip line1 auth name", "label": "SIP Auth Name / Extension", "type": "string"},
            {"key": "sip line1 password", "label": "SIP Auth Password", "type": "string"},
            {"key": "sip line1 proxy ip", "label": "SIP Proxy / PBX IP", "type": "string"},
            {"key": "ring tone", "label": "Ringtone ID (1-5)", "type": "choice", "options": ["1", "2", "3", "4", "5"]},
            # Truncated for brevity - add your full generic settings list here
        ]
    }
    # 6867i Model
    settings_6867i = [
        {"key": "background image", "label": "Wallpaper Filename", "type": "string"},
        {"key": "background image display mode", "label": "Wallpaper Scaling", "type": "choice", "options": ["0", "1"]}
    ]
    for i in range(1, 11):
        settings_6867i.extend([{"key": f"topsoftkey{i} type", "label": f"Top Key {i} Type", "type": "choice", "options": ["none", "speeddial", "blf"]}])
    model_6867i = {"model": "6867i", "description": "Mitel 6867i", "settings": settings_6867i}

    for name, data in [("generic", model_generic), ("6867i", model_6867i)]:
        filepath = f"{MODEL_DIR}/{name}.json"
        if not os.path.exists(filepath):
            with open(filepath, "w") as f: json.dump(data, f, indent=4)

initialize_models()

# --- Helper Functions ---
def get_sftp(host, user, passwd):
    transport = paramiko.Transport((host, 22))
    transport.connect(username=user, password=passwd)
    return paramiko.SFTPClient.from_transport(transport), transport

def load_endpoints():
    if os.path.exists(ENDPOINT_FILE):
        with open(ENDPOINT_FILE, "r") as f: return json.load(f)
    return []

def save_endpoints(data):
    with open(ENDPOINT_FILE, "w") as f: json.dump(data, f, indent=4)

# --- API Routes ---
@app.route('/')
def index():
    models = [f.replace(".json", "") for f in os.listdir(MODEL_DIR) if f.endswith(".json")]
    return render_template('index.html', models=models, endpoints=load_endpoints())

@app.route('/api/schema/<model_name>')
def get_schema(model_name):
    with open(f"{MODEL_DIR}/{model_name}.json", "r") as f:
        return jsonify(json.load(f))

@app.route('/api/sftp/upload', methods=['POST'])
def sftp_upload():
    data = request.json
    filename = data.get('filename')
    content = data.get('content')
    sftp_config = data.get('sftp')
    
    local_path = os.path.join(TEMP_DIR, filename)
    with open(local_path, 'w') as f: f.write(content)
        
    try:
        sftp, transport = get_sftp(sftp_config['host'], sftp_config['user'], sftp_config['pass'])
        sftp.put(local_path, f"{sftp_config['path']}/{filename}")
        sftp.close(); transport.close()
        return jsonify({"status": "success", "msg": f"Uploaded {filename}"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route('/api/arp', methods=['POST'])
def fetch_mac():
    ip = request.json.get('ip')
    try:
        ping_cmd = ['ping', '-c', '1', '-W', '1', ip]
        subprocess.run(ping_cmd, stdout=subprocess.DEVNULL)
        arp_cmd = ['arp', '-n', ip]
        output = subprocess.check_output(arp_cmd, universal_newlines=True)
        mac_match = re.search(r'([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})', output)
        if mac_match:
            return jsonify({"status": "success", "mac": mac_match.group(0).replace('-', ':').upper()})
        return jsonify({"status": "error", "msg": "MAC not found"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route('/api/reboot', methods=['POST'])
def reboot_phone():
    data = request.json
    ip, pwd = data.get('ip'), data.get('password')
    xml_string = '<AastraIPPhoneExecute><ExecuteItem URI="Command: Reset"/></AastraIPPhoneExecute>'
    payload = urllib.parse.urlencode({'xml': xml_string}).encode('utf-8')
    url = f"http://{ip}/"
    
    try:
        req = urllib.request.Request(url, data=payload)
        base64string = base64.b64encode(f"admin:{pwd}".encode('utf-8')).decode('utf-8')
        req.add_header("Authorization", f"Basic {base64string}")
        urllib.request.urlopen(req, timeout=3)
        return jsonify({"status": "success", "msg": "Reboot command sent."})
    except Exception as e:
        return jsonify({"status": "success", "msg": "Phone dropped connection (restarting)."}) # Expected drop

@app.route('/api/wallpaper', methods=['POST'])
def process_wallpaper():
    file = request.files['image']
    sftp_host = request.form['host']
    sftp_user = request.form['user']
    sftp_pass = request.form['pass']
    sftp_path = request.form['path']
    
    if file:
        filename = secure_filename(file.filename)
        local_path = os.path.join(TEMP_DIR, filename)
        file.save(local_path)
        
        target_name = "fond_leger.png"
        processed_path = os.path.join(TEMP_DIR, target_name)
        
        try:
            with Image.open(local_path) as img:
                img_fitted = ImageOps.fit(img, (320, 240), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
                img_quantized = img_fitted.quantize(colors=256)
                img_quantized.save(processed_path, format="PNG")
                
            sftp, transport = get_sftp(sftp_host, sftp_user, sftp_pass)
            sftp.put(processed_path, f"{sftp_path}/{target_name}")
            sftp.close(); transport.close()
            return jsonify({"status": "success", "msg": "Wallpaper optimized and uploaded!"})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)}), 500

@app.route('/api/endpoints/import', methods=['POST'])
def import_endpoints():
    if 'file' not in request.files:
        return jsonify({"status": "error", "msg": "No file uploaded."}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "msg": "No file selected."}), 400
        
    try:
        # Parse the uploaded JSON
        new_endpoints = json.load(file)
        if not isinstance(new_endpoints, list):
            return jsonify({"status": "error", "msg": "Invalid format: Expected a JSON list."}), 400
        
        # Load existing endpoints and track MACs to prevent duplicates
        current_endpoints = load_endpoints()
        existing_macs = {ep.get('mac', '').upper() for ep in current_endpoints}
        
        added_count = 0
        for ep in new_endpoints:
            mac = ep.get('mac', '').upper()
            if mac and mac not in existing_macs:
                current_endpoints.append(ep)
                existing_macs.add(mac)
                added_count += 1
                
        # Save the merged list
        save_endpoints(current_endpoints)
        return jsonify({"status": "success", "msg": f"Imported {added_count} new endpoints!"})
        
    except json.JSONDecodeError:
        return jsonify({"status": "error", "msg": "Invalid JSON file."}), 400
    except Exception as e:
        return jsonify({"status": "error", "msg": f"Import failed: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)