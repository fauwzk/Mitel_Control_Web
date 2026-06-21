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
    if os.path.exists(MODEL_DIR):
        for file in glob.glob(f"{MODEL_DIR}/*.json"): os.remove(file)
    if wipe_inventory and os.path.exists(ENDPOINT_FILE): os.remove(ENDPOINT_FILE)

cleanup_json_at_startup(wipe_inventory=False)

def initialize_models():
    if not os.path.exists(MODEL_DIR): os.makedirs(MODEL_DIR)
    model_generic = {
        "model": "generic", "description": "Global SIP & Network Settings",
        "settings": [
            {"key": "dhcp", "label": "DHCP (1=On, 0=Off)", "type": "choice", "options": ["1", "0"]},
            {"key": "tftp server", "label": "TFTP Server IP Address", "type": "string"},
            {"key": "sip line1 screen name", "label": "Screen Label (Line 1)", "type": "string"},
            {"key": "sip line1 auth name", "label": "SIP Auth Name / Extension", "type": "string"},
            {"key": "sip line1 password", "label": "SIP Auth Password", "type": "string"},
            {"key": "sip line1 proxy ip", "label": "SIP Proxy / PBX IP", "type": "string"},
            {"key": "ring tone", "label": "Ringtone ID (1-5)", "type": "choice", "options": ["1", "2", "3", "4", "5"]}
        ]
    }
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

@app.route('/')
def index():
    models = [f.replace(".json", "") for f in os.listdir(MODEL_DIR) if f.endswith(".json")]
    return render_template('index.html', models=models, endpoints=load_endpoints())

@app.route('/api/schema/<model_name>')
def get_schema(model_name):
    with open(f"{MODEL_DIR}/{model_name}.json", "r") as f: return jsonify(json.load(f))

@app.route('/api/models/<model_name>', methods=['DELETE'])
def delete_model(model_name):
    if model_name.lower() == 'generic': return jsonify({"status": "error", "msg": "Cannot delete base model."}), 403
    filepath = os.path.join(MODEL_DIR, secure_filename(f"{model_name}.json"))
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            return jsonify({"status": "success", "msg": "Model deleted."})
        return jsonify({"status": "error", "msg": "File not found."}), 404
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# --- NEW: SFTP List & Download Routes ---
@app.route('/api/sftp/list', methods=['POST'])
def sftp_list():
    data = request.json
    try:
        sftp, transport = get_sftp(data['host'], data['user'], data['pass'])
        files = sftp.listdir(data['path'])
        cfg_files = sorted([f for f in files if f.endswith('.cfg') or f.endswith('.txt')])
        sftp.close(); transport.close()
        return jsonify({"status": "success", "files": cfg_files})
    except Exception as e:
        return jsonify({"status": "error", "msg": f"SFTP Error: {str(e)}"}), 500

@app.route('/api/sftp/download', methods=['POST'])
def sftp_download():
    data = request.json
    filename = data['filename']
    local_path = os.path.join(TEMP_DIR, filename)
    try:
        sftp, transport = get_sftp(data['host'], data['user'], data['pass'])
        sftp.get(f"{data['path']}/{filename}", local_path)
        sftp.close(); transport.close()
        
        # Parse the downloaded .cfg file into key-value pairs
        parsed_data = {}
        with open(local_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                if ':' in line:
                    k, v = line.split(':', 1)
                    parsed_data[k.strip()] = v.strip()
                    
        return jsonify({"status": "success", "data": parsed_data, "msg": f"Loaded {filename}"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

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

@app.route('/api/endpoints/import', methods=['POST'])
def import_endpoints():
    file = request.files.get('file')
    if not file or file.filename == '': return jsonify({"status": "error", "msg": "No file."}), 400
    try:
        new_endpoints = json.load(file)
        current = load_endpoints()
        existing_macs = {ep.get('mac', '').upper() for ep in current}
        added = 0
        for ep in new_endpoints:
            mac = ep.get('mac', '').upper()
            if mac and mac not in existing_macs:
                current.append(ep)
                existing_macs.add(mac)
                added += 1
        save_endpoints(current)
        return jsonify({"status": "success", "msg": f"Imported {added} endpoints."})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route('/api/arp', methods=['POST'])
def fetch_mac():
    ip = request.json.get('ip')
    try:
        ping_cmd = ['ping', '-c', '1', '-W', '1', ip]
        subprocess.run(ping_cmd, stdout=subprocess.DEVNULL)
        output = subprocess.check_output(['arp', '-n', ip], universal_newlines=True)
        mac_match = re.search(r'([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})', output)
        if mac_match: return jsonify({"status": "success", "mac": mac_match.group(0).replace('-', ':').upper()})
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
        return jsonify({"status": "success", "msg": "Phone restarting."}) 

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)