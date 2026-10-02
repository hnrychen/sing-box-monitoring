import json
import ssl
import urllib.request

with open('/config/config.json') as stream:
    config = json.load(stream)
context = ssl.create_default_context(cafile=config['tls_cert'])
request = urllib.request.Request('https://127.0.0.1:9119/healthz',
                                 headers={'Authorization': 'Bearer ' + config['scrape_token']})
with urllib.request.urlopen(request, context=context, timeout=3) as response:
    assert response.status == 200
