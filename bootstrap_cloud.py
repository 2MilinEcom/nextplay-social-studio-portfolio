"""Transfer the authorized AI configuration without printing credentials.

Run locally after Railway is healthy. Deployment credentials are in the ignored
600-mode .deploy-secrets.json file. This does not connect or publish social posts.
"""
import json,os
from pathlib import Path
import requests
from source.social_platform.auth import invite

def main():
    access=json.loads(Path(__file__).with_name('.deploy-secrets.json').read_text())
    origin=access['origin'];session=requests.Session()
    session.get(origin+'/health',timeout=30).raise_for_status()
    session.post(origin+'/auth/login',headers={'X-NextPlay':'marketing'},json={'password':access['bootstrap']},timeout=30).raise_for_status()
    key=os.getenv('OPENAI_API_KEY')
    if key:
        session.post(origin+'/api/marketing/ai-config',headers={'X-NextPlay':'marketing'},json={'key':key},timeout=30).raise_for_status()
    state=session.get(origin+'/api/marketing/state',timeout=30);state.raise_for_status()
    print(json.dumps({'healthy':True,'ai_connected':state.json()['ai'],'hosting':state.json()['hosting']}))
    # Private handoff is generated only after the authenticated setup succeeds.
    target=Path(__file__).with_name('runtime')/'cloud-first-login.txt';target.parent.mkdir(exist_ok=True)
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:f.write(origin+'/auth/link?token='+invite(access['bootstrap']))

if __name__=='__main__':main()
