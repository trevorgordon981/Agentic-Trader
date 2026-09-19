"""Public API contract; production-derived narrative omitted."""
import json
import os
import re
from datetime import datetime
from pathlib import Path

BINDING_SHA256 = '1111111111111111111111111111111111111111111111111111111111111111'
BINDING = Path('/opt/agentic-trader/exitmgr-app/example-model-binding.json')
MODEL = 'example-mtp-model'
TARGET = Path('/opt/agentic-trader/models/example-mtp-artifact')
ENDPOINT = 'http://127.0.0.1:18080/v1/chat/completions'
LABEL = 'ai.example.mtp-runtime'


def snapshot(endpoint, health, timeout, opener):
    from exitmgr import provenance as p
    def require(ok, why):
        if not ok:
            raise p.RuntimeIdentityError('DeepSeek identity refused: ' + why)
    require(endpoint == ENDPOINT, 'endpoint')
    require(p._file_sha256(BINDING) == BINDING_SHA256, 'binding digest')
    binding = json.loads(BINDING.read_text())
    require(binding['model'] == MODEL and binding['target'] == str(TARGET), 'binding model')
    for name, digest in binding['files'].items():
        require(p._file_sha256(Path(name)) == digest, 'pinned file ' + name)
    require(health.get('status') == 'healthy' and health.get('default_model') == MODEL, 'health')
    def fetch(path):
        return p._fetch(p._server_url(endpoint,path), timeout, opener, as_json=True)
    models, status, inventory = fetch('/v1/models'), fetch('/api/status'), fetch('/v1/models/status')
    require([r.get('id') for r in models.get('data',[])] == [MODEL], 'advertised model')
    require(status.get('status') == 'ok' and status.get('version') == '0.0.0-public'
            and status.get('loaded_models') == [MODEL] and status.get('models_loading') == 0,
            'loaded runtime')
    rows = [r for r in inventory.get('models',[]) if r.get('id') == MODEL]
    require(len(rows) == 1, 'loaded model inventory')
    row = rows[0]
    require(row.get('loaded') is True and row.get('is_loading') is False, 'loading state')
    require(Path(row['model_path']).resolve(strict=True) == TARGET.resolve(strict=True), 'real model path')
    settings_path = Path(binding['model_settings'])
    settings = json.loads(settings_path.read_text())['models'][MODEL]
    require(settings.get('mtp_enabled') is True and settings.get('mtp_num_draft_tokens') == 5,
            'MTP policy')
    require(settings.get('chat_template_kwargs') == {'enable_thinking':False,'thinking_mode':'chat'},
            'thinking policy')
    launch = p._command_output(('/bin/launchctl','print',f'gui/{os.getuid()}/{LABEL}'))
    match = re.search(r'(?m)^\s*pid = (\d+)\s*$', launch)
    require(match is not None and 'state = running' in launch, 'launchd')
    pid = int(match.group(1))
    started = p._command_output(('/bin/ps','-p',str(pid),'-o','lstart='))
    listener = p._command_output(('/usr/sbin/lsof','-nP','-a','-p',str(pid),'-iTCP:18080','-sTCP:LISTEN','-Fn'))
    require(f'p{pid}' in listener.splitlines() and 'n127.0.0.1:18080' in listener.splitlines(),'listener owner')
    started_unix = int(datetime.strptime(started,'%a %b %d %H:%M:%S %Y').astimezone().timestamp())
    runtime = {'pid':pid,'started':started,'binding_sha256':BINDING_SHA256}
    return dict(artifact_id='omlx-dsv41:' + MODEL,
                artifact_manifest_sha256=binding['artifact_sha256'],
                runtime_receipt_sha256=p.sha256(runtime),
                runtime_contract_sha256=BINDING_SHA256,
                model_realpath=str(TARGET),model_id=MODEL,binding_kind='omlx-openai-local',
                started_unix=started_unix,startup_nonce=p.sha256(runtime),
                readiness_smoke_sha256=binding['readiness_smoke_sha256'],health_url=p.health_url(endpoint))
