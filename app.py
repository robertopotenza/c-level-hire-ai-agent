import os
import json
import logging
import requests
import base64
import hashlib
import hmac
from flask import Flask, request, jsonify
from anthropic import Anthropic

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RailwayAgent:
    def __init__(self, config_file="railway_config.json"):
        self.config = self.load_config(config_file)
        self.anthropic = Anthropic(api_key=self.config["anthropic_api_key"])
        self.app = Flask(__name__)
        self.setup_routes()
        
    def load_config(self, config_file):
        """Load configuration with environment variable substitution"""
        with open(config_file, 'r') as f:
            config = json.load(f)
        
        # Replace environment variables
        def replace_env_vars(obj):
            if isinstance(obj, dict):
                return {k: replace_env_vars(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [replace_env_vars(item) for item in obj]
            elif isinstance(obj, str) and obj.startswith('$'):
                return os.environ.get(obj[2:], obj)
            return obj
        
        return replace_env_vars(config)
    
    def setup_routes(self):
        """Set up Flask routes"""
        
        @self.app.route('/')
        def home():
            return jsonify({
                "message": "C-Level Hire AI Agent",
                "status": "running",
                "method": "github_api",
                "available_endpoints": ["/analyze", "/webhook", "/status"]
            })
        
        @self.app.route('/status')
        def status():
            return jsonify({
                "repositories": list(self.config["repositories"].keys()),
                "method": "github_api",
                "status": "ready"
            })
        
        @self.app.route('/analyze', methods=['POST'])
        def analyze():
            try:
                data = request.get_json() or {}
                query = data.get('query', 'Analyze this repository')
                repo_name = data.get('repository', self.config["default_repo"])
                
                # Fetch repo contents via GitHub API
                file_contents = self.fetch_repo_via_github_api(repo_name)
                
                if not file_contents:
                    return jsonify({
                        'success': False,
                        'error': 'No readable files found in repository'
                    }), 400
                
                # Send to Claude
                response = self.send_to_claude(query, file_contents, repo_name)
                
                return jsonify({
                    'success': True,
                    'analysis': response,
                    'repository': repo_name,
                    'files_analyzed': len(file_contents)
                })
                
            except Exception as e:
                logger.error(f"Analysis error: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': str(e)
                }), 500
        
        @self.app.route('/webhook', methods=['POST'])
        def github_webhook():
            try:
                # Verify GitHub webhook signature
                signature = request.headers.get('X-Hub-Signature-256')
                if not self.verify_webhook_signature(request.data, signature):
                    return jsonify({'error': 'Invalid signature'}), 401
                
                payload = request.get_json()
                repo_url = payload.get('repository', {}).get('clone_url')
                
                # Find matching repository
                repo_name = None
                for name, config in self.config["repositories"].items():
                    if config["url"] == repo_url:
                        repo_name = name
                        break
                
                if not repo_name:
                    return jsonify({'message': 'Repository not watched'}), 200
                
                # Auto-analyze on push
                if payload.get('ref') == f'refs/heads/{self.config["repositories"][repo_name]["branch"]}':
                    commits = payload.get('commits', [])
                    if commits:
                        latest_commit = commits[-1]
                        modified_files = latest_commit.get('modified', [])
                        
                        if modified_files:
                            query = f"Review the recent changes: {', '.join(modified_files[:5])}"
                            file_contents = self.fetch_repo_via_github_api(repo_name)
                            analysis = self.send_to_claude(query, file_contents, repo_name)
                            logger.info(f"Auto-analysis completed for {repo_name}")
                
                return jsonify({'message': 'Webhook processed successfully'}), 200
                
            except Exception as e:
                logger.error(f"Webhook error: {str(e)}")
                return jsonify({'error': str(e)}), 500
    
    def verify_webhook_signature(self, payload_body, signature_header):
        """Verify GitHub webhook signature"""
        if not signature_header:
            return False
        
        secret = self.config["github_webhook_secret"].encode()
        expected_signature = hmac.new(secret, payload_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"sha256={expected_signature}", signature_header)
    
    def fetch_repo_via_github_api(self, repo_name):
        """Fetch repository contents via GitHub API"""
        logger.info(f"Fetching repository {repo_name} via GitHub API")
        
        # Extract owner/repo from URL
        repo_config = self.config["repositories"][repo_name]
        repo_url = repo_config["url"]
        
        # Parse GitHub URL to get owner/repo
        if "github.com/" in repo_url:
            parts = repo_url.split("github.com/")[1].replace(".git", "").split("/")
            owner, repo = parts[0], parts[1]
        else:
            raise ValueError(f"Invalid GitHub URL: {repo_url}")
        
        # Fetch repository tree
        api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/main?recursive=1"
        logger.info(f"Fetching from: {api_url}")
        
        response = requests.get(api_url)
        
        if response.status_code != 200:
            raise Exception(f"Failed to fetch repository tree: {response.status_code} - {response.text}")
        
        tree_data = response.json()
        file_contents = {}
        
        watched_extensions = self.config["watched_extensions"]
        ignored_paths = self.config["ignored_paths"]
        max_file_size = self.config["max_file_size"]
        
        files_processed = 0
        
        for item in tree_data.get("tree", []):
            if item["type"] == "blob":  # It's a file
                file_path = item["path"]
                
                # Check if we should process this file
                if any(ignored in file_path for ignored in ignored_paths):
                    continue
                
                file_ext = "." + file_path.split(".")[-1] if "." in file_path else ""
                if file_ext not in watched_extensions:
                    continue
                
                if item["size"] > max_file_size:
                    logger.info(f"Skipping large file: {file_path} ({item['size']} bytes)")
                    continue
                
                # Fetch file content
                file_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
                file_response = requests.get(file_url)
                
                if file_response.status_code == 200:
                    file_data = file_response.json()
                    if file_data.get("encoding") == "base64":
                        try:
                            content = base64.b64decode(file_data["content"]).decode('utf-8')
                            file_contents[file_path] = content
                            files_processed += 1
                            logger.info(f"Processed file: {file_path}")
                        except (UnicodeDecodeError, Exception) as e:
                            logger.warning(f"Skipping binary/unreadable file: {file_path} - {e}")
                            continue
                else:
                    logger.warning(f"Failed to fetch file {file_path}: {file_response.status_code}")
        
        logger.info(f"Successfully processed {files_processed} files from {repo_name}")
        return file_contents
    
    def send_to_claude(self, query, file_contents, repo_name):
        """Send analysis request to Claude"""
        repo_description = self.config["repositories"][repo_name]["description"]
        
        # Prepare context
        context = f"Repository: {repo_name}\nDescription: {repo_description}\n\n"
        context += f"Files analyzed: {len(file_contents)}\n\n"
        context += "File Contents:\n"
        
        for file_path, content in file_contents.items():
            context += f"\n--- {file_path} ---\n"
            context += content[:2000]  # Truncate very long files
            if len(content) > 2000:
                context += "\n... (file truncated)"
        
        # Send to Claude
        message = f"{query}\n\nContext:\n{context}"
        
        try:
            response = self.anthropic.messages.create(
                model="claude-3-5-sonnet-20241022",
                max_tokens=4000,
                temperature=0.1,
                messages=[{
                    "role": "user",
                    "content": message
                }]
            )
            
            return response.content[0].text
            
        except Exception as e:
            logger.error(f"Claude API error: {str(e)}")
            raise

# Create the Flask app instance for gunicorn
agent = RailwayAgent()
app = agent.app

# For local testing
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
