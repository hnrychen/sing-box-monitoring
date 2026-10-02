#!/usr/bin/env python3
"""Small RTT validation helper. No credentials, config changes or traffic load."""
import argparse
import json
import re
import socket
import statistics
import struct
import subprocess
import time
import tcpdiag


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--connect', help='Remote server IPv4/DNS; otherwise observe local TCP listeners')
    parser.add_argument('--ports', default='10443,443')
    parser.add_argument('--seconds', type=int, default=30)
    args = parser.parse_args()
    ports = [int(p) for p in args.ports.split(',')]
    if args.connect:
        sockets = []
        try:
            for port in ports:
                sock = socket.create_connection((args.connect, port), timeout=5)
                sockets.append(sock)
                raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104)
                rtt, variation = struct.unpack_from('=II', raw, 68)
                print(json.dumps(dict(port=port, local_port=sock.getsockname()[1],
                                      client_rtt_ms=rtt / 1000, variation_ms=variation / 1000)), flush=True)
            # At most eight 64-byte ICMP probes, once per second; optional cross-check.
            ping = subprocess.run(['ping', '-n', '-c', '8', '-W', '2', args.connect],
                                  capture_output=True, text=True, timeout=20)
            samples = [float(v) for v in re.findall(r'time=([\d.]+)', ping.stdout)]
            print(json.dumps(dict(ping_samples=len(samples), ping_median_ms=statistics.median(samples) if samples else None)), flush=True)
            time.sleep(max(0, args.seconds - 8))
        finally:
            for sock in sockets:
                sock.close()
    else:
        for _ in range(3):
            started = time.perf_counter()
            records = tcpdiag.snapshot(ports)
            print(json.dumps(dict(duration_ms=(time.perf_counter() - started) * 1000, sockets=records)), flush=True)
            time.sleep(2)
        ss = subprocess.run(['ss', '-tinOH', 'state', 'established',
                             '( ' + ' or '.join(f'sport = :{p}' for p in ports) + ' )'],
                            capture_output=True, text=True, check=True, timeout=3)
        print(ss.stdout, flush=True)


if __name__ == '__main__':
    main()
