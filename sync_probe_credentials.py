#!/usr/bin/env python3
"""Synchronize existing central VLESS/Hysteria2 probes from a current Clash YAML.

Requires PyYAML locally. Dry-run by default; secrets only travel over SSH stdin.
Does not add targets, change servers, or modify the source YAML/proxy services.
"""
import argparse
import json
import os
import pathlib
import shlex
import subprocess


def outbound(node):
    protocol = node['type']
    result = dict(type=protocol, server=node['server'], server_port=int(node['port']))
    if protocol == 'ss':
        result.update(type='shadowsocks', method=node['cipher'], password=node['password'])
    elif protocol == 'vless':
        if node.get('network', 'tcp') != 'tcp' or not node.get('tls') or not node.get('reality-opts'):
            raise ValueError('only TCP VLESS REALITY is supported')
        reality = node['reality-opts']
        result.update(uuid=node['uuid'], flow=node.get('flow', ''), tls=dict(
            enabled=True, server_name=node['servername'],
            utls=dict(enabled=True, fingerprint=node.get('client-fingerprint', 'chrome')),
            reality=dict(enabled=True, public_key=reality['public-key'], short_id=reality['short-id'])))
    elif protocol == 'hysteria2':
        # Never silently discard a certificate pin. The older deployed core
        # cannot represent Clash's raw certificate fingerprint directly.
        if node.get('fingerprint') or node.get('certificate'):
            raise ValueError('custom certificate trust requires explicit conversion')
        result.update(password=node['password'], tls=dict(enabled=True,
            server_name=node.get('sni', node['server']), insecure=bool(node.get('skip-cert-verify', False))))
        if node.get('alpn'):
            result['tls']['alpn'] = node['alpn']
        if node.get('obfs'):
            if node['obfs'] != 'salamander':
                raise ValueError('unsupported Hysteria2 obfuscation')
            result['obfs'] = dict(type='salamander', password=node['obfs-password'])
        for source, dest in (('up', 'up_mbps'), ('down', 'down_mbps')):
            if source in node:
                result[dest] = int(node[source])
    else:
        raise ValueError('unsupported protocol')
    return result


def update(config, nodes):
    candidates = {}
    for node in nodes:
        if node.get('type') not in ('vless', 'hysteria2'):
            continue
        key = (node['type'], node['server'], int(node['port']))
        value = outbound(node)
        if key in candidates and candidates[key] != value:
            raise ValueError('ambiguous client nodes for the same endpoint')
        candidates[key] = value
    changed = []
    for target in config['targets']:
        old = target['outbound']
        if target['protocol'] not in ('vless', 'hysteria2'):
            continue
        key = (target['protocol'], old['server'], old['server_port'])
        if key not in candidates:
            raise ValueError('an existing probe has no matching current client node')
        if old != candidates[key]:
            target['outbound'] = candidates[key]
            changed.append(dict(instance=target['instance'], protocol=target['protocol']))
    return changed


REMOTE = r'''
import json, os, pathlib, shutil, subprocess, sys, tempfile, time
from sync_probe_credentials import update
path = pathlib.Path('/etc/sing-box-probe/config.json')
original = path.read_bytes()
config = json.loads(original)
request = json.load(sys.stdin)
changed = update(config, request['nodes'])
if not request['apply'] or not changed:
    print(json.dumps(dict(applied=False, changed=changed)))
    sys.exit(0)
sys.path.insert(0, '/opt/sing-box-probe')
from proxy_probe import client_config, validate
validate(config)
with tempfile.TemporaryDirectory(prefix='probe-sync-') as directory:
    candidate = pathlib.Path(directory) / 'client.json'
    candidate.write_text(json.dumps(client_config(config)))
    os.chmod(candidate, 0o600)
    result = subprocess.run([config.get('sing_box_binary', '/usr/bin/sing-box'), 'check', '-c', str(candidate)], capture_output=True)
    if result.returncode:
        raise RuntimeError('new probe client rejected; private stderr withheld')
if path.read_bytes() != original:
    raise RuntimeError('probe configuration changed during preflight')
backup = pathlib.Path('/var/backups/sing-box-probe')
backup.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(backup, 0o700)
saved = backup / ('config-' + str(time.time_ns()) + '.json')
shutil.copy2(path, saved)
os.chmod(saved, 0o600)
try:
    path.write_text(json.dumps(config, indent=2) + '\n')
    subprocess.run(['systemctl', 'restart', 'sing-box-probe'], check=True, capture_output=True)
    subprocess.run(['systemctl', 'is-active', '--quiet', 'sing-box-probe'], check=True, capture_output=True)
except Exception:
    path.write_bytes(original)
    subprocess.run(['systemctl', 'restart', 'sing-box-probe'], capture_output=True)
    raise
print(json.dumps(dict(applied=True, changed=changed, backup=str(saved))))
'''


def main():
    import yaml
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('clash_yaml', type=pathlib.Path)
    parser.add_argument('--central', required=True, help='SSH alias of the central probe host')
    parser.add_argument('--ssh', default=r'C:\Windows\System32\OpenSSH\ssh.exe' if os.name == 'nt' else 'ssh')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.central.startswith('-'):
        parser.error('invalid SSH host')
    nodes = yaml.safe_load(args.clash_yaml.read_text(encoding='utf-8-sig'))['proxies']
    # Validate locally before transmitting any credentials.
    for node in nodes:
        if node.get('type') in ('vless', 'hysteria2'):
            outbound(node)
    module = pathlib.Path(__file__).read_text(encoding='utf-8')
    # Load only definitions; no local CLI execution on the remote host.
    code = 'import types,sys; m=types.ModuleType("sync_probe_credentials"); sys.modules[m.__name__]=m; exec(' + repr(module) + ',m.__dict__); exec(' + repr(REMOTE) + ')'
    process = subprocess.run([args.ssh, args.central, 'sudo python3 -c ' + shlex.quote(code)],
        input=json.dumps(dict(nodes=nodes, apply=args.apply)), text=True, capture_output=True)
    if process.returncode:
        raise RuntimeError('probe sync failed; private command output withheld')
    print(process.stdout.strip())


if __name__ == '__main__':
    main()
