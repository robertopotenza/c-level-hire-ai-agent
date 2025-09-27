import os
import json
from flask import Flask, jsonify
from anthropic import Anthropic

app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({"message": "C-Level Hire AI Agent", "status": "running"})

@app.route("/status")  
def status():
    return jsonify({"status": "ready", "config_loaded": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
