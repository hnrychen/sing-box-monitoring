#!/usr/bin/env python3
"""Opt-in cleanup of persistently idle, empty, sing-box-owned FIN-WAIT-2 sockets.

Never touches ESTABLISHED, UDP/QUIC, listening sockets or IP ban rules. No API
credentials are needed. Audit is the default; application requires --apply.
"""
import argparse
import errno
import json
import os
import pathlib
import re
import subprocess
import tempfile
import time
import tcpdiag


def identity(row):
    return ':'.join(map(str, (row['family'], row['inode'], *row['socket_cookie'])))


def eligible(row, inodes, first_seen, now, idle=3600):
    return (row.get('state') == 5 and row.get('inode', 0) in inodes
            and row.get('socket_cookie') not in (None, (0xffffffff, 0xffffffff))
            and row.get('payload_idle_seconds', -1) >= idle
            and row.get('send_queue_bytes', -1) == 0
            and row.get('receive_queue_bytes', -1) == 0
            and now - first_seen >= idle)


def owner_pid(service):
    if not re.fullmatch(r'sing-box(?:-[a-zA-Z0-9_-]+)?(?:\.service)?', service):
        raise ValueError('expected explicit sing-box unit')
    pid = int(subprocess.check_output(['systemctl', 'show', service, '-p', 'MainPID', '--value'], text=True, timeout=3))
    if pid <= 0:
        return 0
    # Do not mistake a reused PID or a custom unit running a different daemon.
    if pathlib.Path(os.readlink(f'/proc/{pid}/exe')).name != 'sing-box':
        raise ValueError('unit does not run sing-box')
    return pid


def owner(service):
    pid = owner_pid(service)
    if not pid:
        return 0, {}
    inodes = {}
    for fd in pathlib.Path(f'/proc/{pid}/fd').iterdir():
        try:
            match = re.fullmatch(r'socket:\[(\d+)\]', os.readlink(fd))
            if match:
                inodes[int(match[1])] = fd
        except FileNotFoundError:
            pass  # FD closed while listing; never infer ownership from UID.
    return pid, inodes


def reconcile(rows, owners, previous, now):
    tracked = {}
    for row in rows:
        for service, (pid, inodes) in owners.items():
            if row['inode'] in inodes:
                key = identity(row)
                old = previous.get(key, {})
                first = old.get('first_seen', now) if old.get('pid') == pid else now
                tracked[key] = dict(first_seen=min(first, now), pid=pid, service=service)
                break
    return tracked  # No history of sockets that disappeared.


def main():
    import fcntl  # Kernel locking is used only by the Linux service entry point.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/sing-box-exporter/config.json')
    parser.add_argument('--state', default='/var/lib/sing-box-session-guard/state.json')
    parser.add_argument('--idle', type=int, default=3600)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-close', type=int, default=256)
    args = parser.parse_args()
    if args.idle < 3600 or not 1 <= args.max_close <= 256:
        parser.error('idle must be >=3600s; max-close must be 1..256')
    if os.geteuid() != 0:
        parser.error('run as root to prove process FD ownership')
    path = pathlib.Path(args.state)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path.parent / 'lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = json.loads(path.read_text()) if path.exists() else {}
        config = json.loads(pathlib.Path(args.config).read_text())
        owners = {s['name']: owner(s['name']) for s in config['services']}
        rows = tcpdiag.snapshot(None, max_sockets=65536, states=(5,))
        now = time.time()
        tracked = reconcile(rows, owners, previous, now)
        candidates = [r for r in rows if identity(r) in tracked and
                      eligible(r, owners[tracked[identity(r)]['service']][1],
                               tracked[identity(r)]['first_seen'], now, args.idle)]
        closed = 0
        for row in candidates[:args.max_close] if args.apply else []:
            saved = tracked[identity(row)]
            pid = owner_pid(saved['service'])
            inodes = owners[saved['service']][1]
            fd = inodes.get(row['inode'])
            if pid != saved['pid'] or not fd:
                continue
            try:
                if os.readlink(fd) != f"socket:[{row['inode']}]":
                    continue
            except FileNotFoundError:
                continue
            fresh = next((r for r in tcpdiag.snapshot([row['local_port']], states=(5,))
                          if identity(r) == identity(row)), None)
            if not fresh or not eligible(fresh, inodes, saved['first_seen'], time.time(), args.idle):
                continue
            try:
                tcpdiag.destroy(fresh)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    continue  # Already closed; cookie does not match a replacement.
                raise
            closed += 1
            del tracked[identity(row)]
            print(json.dumps(dict(event='closed_idle_fin_wait_2', service=saved['service'],
                                  inode=fresh['inode'], payload_idle_seconds=fresh['payload_idle_seconds'])))
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            json.dump(tracked, stream)
            temporary = stream.name
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        print(json.dumps(dict(apply=args.apply, tracked=len(tracked), eligible=len(candidates), closed=closed)))


if __name__ == '__main__':
    main()
