import os
import json
import logging
from flask import Flask, request, jsonify
from anthropic import Anthropic
import git
import hashlib
import hmac
from pathlib import Path

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RailwayAgent:
    def __init__(self, config_file="railway_config.json"):
        self.config = self.load_config(config_file)
        self.anthropic = Anthropic(api_key=self.config["anthropic_api_key"])
        self.app = Flask(__name__)
        self.setup_routes()
        self.repos_dir = Path("./repos")
        self.repos_dir.mkdir(exist_ok=True)
        
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
                "available_endpoints": ["/analyze", "/webhook", "/status"]
            })
        
        @self.app.route('/status')
        def status():
            return jsonify({
                "repositories": list(self.config["repositories"].keys()),
                "last_sync": "Available on request"
            })
        
        @self.app.route('/analyze', methods=['POST'])
        def analyze():
            try:
                data = request.get_json() or {}
                query = data.get('query', 'Analyze this repository')
                repo_name = data.get('repository', self.config["default_repo"])
                
                # Ensure repository is synced
                self.sync_repository(repo_name)
                
                # Read file contents
                file_contents = self.read_file_contents(repo_name)
                
                # Send to Claude
                response = self.send_to_claude(query, file_contents, repo_name)
                
                return jsonify({
                    'success': True,
                    'analysis': response,
                    'repository': repo_name
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
                
                # Sync the repository
                self.sync_repository(repo_name)
                
                # Auto-analyze on push
                if payload.get('ref') == f'refs/heads/{self.config["repositories"][repo_name]["branch"]}':
                    commits = payload.get('commits', [])
                    if commits:
                        latest_commit = commits[-1]
                        modified_files = latest_commit.get('modified', [])
                        
                        if modified_files:
                            query = f"Review the recent changes: {', '.join(modified_files[:5])}"
                            file_contents = self.read_file_contents(repo_name)
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
    
    def sync_repository(self, repo_name):
        """Clone or pull the latest version of a repository"""
        if repo_name not in self.config["repositories"]:
            raise ValueError(f"Unknown repository: {repo_name}")
        
        repo_config = self.config["repositories"][repo_name]
        local_path = Path(repo_config["local_path"])
        
        try:
            if local_path.exists():
                # Pull latest changes
                repo = git.Repo(local_path)
                origin = repo.remotes.origin
                origin.pull()
                logger.info(f"Updated repository {repo_name}")
            else:
                # Clone repository
                local_path.parent.mkdir(parents=True, exist_ok=True)
                git.Repo.clone_from(repo_config["url"], local_path)
                logger.info(f"Cloned repository {repo_name}")
                
        except Exception as e:
            logger.error(f"Error syncing repository {repo_name}: {str(e)}")
            raise
    
    def read_file_contents(self, repo_name):
        """Read and return file contents from repository"""
        repo_config = self.config["repositories"][repo_name]
        local_path = Path(repo_config["local_path"])
        
        if not local_path.exists():
            raise ValueError(f"Repository {repo_name} not found locally")
        
        file_contents = {}
        watched_extensions = self.config["watched_extensions"]
        ignored_paths = self.config["ignored_paths"]
        max_file_size = self.config["max_file_size"]
        
        def should_ignore(path):
            path_str = str(path)
            return any(ignored in path_str for ignored in ignored_paths)
        
        for file_path in local_path.rglob("*"):
            if file_path.is_file() and not should_ignore(file_path):
                if file_path.suffix in watched_extensions:
                    try:
                        if file_path.stat().st_size <= max_file_size:
                            relative_path = file_path.relative_to(local_path)
                            with open(file_path, 'r', encoding='utf-8') as f:
                                file_contents[str(relative_path)] = f.read()
                    except (UnicodeDecodeError, PermissionError):
                        # Skip binary files or files we can't read
                        continue
        
        return file_contents
    
    def send_to_claude(self, query, file_contents, repo_name):
        """Send analysis request to Claude"""
        repo_description = self.config["repositories"][repo_name]["description"]
        
        # Prepare context
        context = f"Repository: {repo_name}\nDescription: {repo_description}\n\n"
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
