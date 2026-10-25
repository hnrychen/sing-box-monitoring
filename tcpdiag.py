#!/usr/bin/env python3
"""Bounded, unprivileged Linux SOCK_DIAG TCP_INFO snapshots; no probes."""
import errno
import ipaddress
import socket
import struct
import time


def address(family, raw):
    ip = ipaddress.ip_address(bytes(raw[:4] if family == socket.AF_INET else raw))
    return str(ip.ipv4_mapped or ip) if isinstance(ip, ipaddress.IPv6Address) else str(ip)


def decode(payload, states=(1,)):
    if len(payload) < 72:
        raise ValueError('short inet_diag message')
    family, state = payload[:2]
    if family not in (socket.AF_INET, socket.AF_INET6) or state not in states:
        return None
    local_port, peer_port = struct.unpack_from('!HH', payload, 4)
    result = dict(family=family, state=state, socket_id=bytes(payload[4:52]),
                  inode=struct.unpack_from('=I', payload, 68)[0],
                  local_ip=address(family, payload[8:24]), peer_ip=address(family, payload[24:40]),
                  local_port=local_port, peer_port=peer_port,
                  uid=struct.unpack_from('=I', payload, 64)[0],
                  socket_cookie=struct.unpack_from('=II', payload, 44),
                  receive_queue_bytes=struct.unpack_from('=I', payload, 56)[0],
                  send_queue_bytes=struct.unpack_from('=I', payload, 60)[0])
    offset = 72
    while offset + 4 <= len(payload):
        length, kind = struct.unpack_from('=HH', payload, offset)
        if length < 4 or offset + length > len(payload):
            raise ValueError('invalid inet_diag attribute')
        if kind & 0x3fff == 2 and length >= 80:  # INET_DIAG_INFO, stable tcp_info prefix.
            rtt, variation = struct.unpack_from('=II', payload, offset + 4 + 68)
            result.update(rtt_seconds=rtt / 1_000_000, variation_seconds=variation / 1_000_000)
            result['ack_age_seconds'] = struct.unpack_from('=I', payload, offset + 4 + 56)[0] / 1000
            result['payload_idle_seconds'] = min(struct.unpack_from('=I', payload, offset + 4 + pos)[0]
                                                for pos in (44, 52)) / 1000
            # Length-gated Linux UAPI fields: older kernels omit, never fake zero.
            for name, field_offset in (('send_mss_bytes', 16), ('cwnd_segments', 80),
                                       ('total_retransmissions', 100), ('notsent_bytes', 144)):
                if length >= 4 + field_offset + 4:
                    result[name] = struct.unpack_from('=I', payload, offset + 4 + field_offset)[0]
        offset += (length + 3) & ~3
    return result


def snapshot(ports, max_sockets=4096, max_bytes=8 * 1024 * 1024, timeout=3, states=(1,)):
    """By default, ESTABLISHED sockets with a requested LOCAL listener port.

    A failed/interrupted/budget-exceeded dump is rejected, never truncated.
    Limits apply to the whole IPv4+IPv6 snapshot. No root/capabilities needed.
    """
    # None means all local ports, for the separately privileged cleanup guard.
    all_ports = ports is None
    ports = [0] if all_ports else sorted(set(int(p) for p in ports))
    if len(ports) > 32 or any(not (0 if all_ports else 1) <= p <= 65535 for p in ports) or not states or any(s not in range(1, 13) for s in states):
        raise ValueError('invalid TCP listener port inventory')
    records, size, sequence = [], 0, 0
    deadline = time.monotonic() + timeout
    for port in ports:
        for family in (socket.AF_INET, socket.AF_INET6):
            sequence += 1
            request = (struct.pack('=BBBBI', family, socket.IPPROTO_TCP, 2, 0, sum(1 << s for s in set(states)))
                       + struct.pack('!HH', port, 0) + bytes(32)
                       + struct.pack('=III', 0, 0xffffffff, 0xffffffff))
            header = struct.pack('=IHHII', 16 + len(request), 20, 0x301, sequence, 0)
            with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4) as diag:
                diag.bind((0, 0))
                diag.sendto(header + request, (0, 0))
                done = False
                while not done:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('TCP snapshot deadline exceeded')
                    diag.settimeout(remaining)
                    packet, _, flags, sender = diag.recvmsg(65536)
                    if sender[0] != 0 or flags & socket.MSG_TRUNC:
                        raise ValueError('invalid or truncated kernel dump')
                    size += len(packet)
                    if size > max_bytes:
                        raise ValueError('TCP snapshot byte budget exceeded')
                    offset = 0
                    while offset + 16 <= len(packet):
                        length, kind, nlflags, seq, _ = struct.unpack_from('=IHHII', packet, offset)
                        if length < 16 or offset + length > len(packet) or seq != sequence:
                            raise ValueError('invalid netlink message')
                        if nlflags & 0x10:  # NLM_F_DUMP_INTR: inconsistent dump.
                            raise ValueError('TCP dump interrupted')
                        payload = packet[offset + 16:offset + length]
                        if kind in (2, 3):  # NLMSG_ERROR / NLMSG_DONE status.
                            error = struct.unpack_from('=i', payload)[0] if len(payload) >= 4 else 0
                            if error:
                                if family == socket.AF_INET6 and -error in (errno.EAFNOSUPPORT, errno.ENOENT):
                                    done = True  # IPv6 can be disabled on a valid Linux host.
                                else:
                                    raise OSError(-error, 'TCP diagnostic request failed')
                            if kind == 3:
                                done = True
                        elif kind == 20:
                            record = decode(payload, states)
                            if record and (not port or record['local_port'] == port):
                                records.append(record)
                                if len(records) > max_sockets:
                                    raise ValueError('TCP socket budget exceeded')
                        offset += (length + 3) & ~3
    return records


def destroy(record):
    """Abort one exact FIN-WAIT-2 socket. Only the privileged guard calls this.

    Kernel cookie validation prevents killing a new socket reusing the tuple.
    Requires CAP_NET_ADMIN; the read-only exporter never gains this capability.
    """
    cookie = tuple(record.get('socket_cookie', (0xffffffff, 0xffffffff)))
    if record.get('state') != 5 or cookie == (0xffffffff, 0xffffffff) or len(record.get('socket_id', b'')) != 48:
        raise ValueError('refusing non-FIN-WAIT-2 or unidentified socket')
    request = struct.pack('=BBBBI', record['family'], socket.IPPROTO_TCP, 0, 0, 1 << 5) + record['socket_id']
    packet = struct.pack('=IHHII', 16 + len(request), 21, 5, 1, 0) + request  # SOCK_DESTROY, REQUEST|ACK
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4) as diag:
        diag.settimeout(3)
        diag.bind((0, 0))
        diag.sendto(packet, (0, 0))
        reply, _, flags, sender = diag.recvmsg(4096)
    if sender[0] != 0 or flags & socket.MSG_TRUNC or len(reply) < 20:
        raise ValueError('invalid socket destroy acknowledgment')
    length, kind, _, seq, _ = struct.unpack_from('=IHHII', reply)
    if length > len(reply) or kind != 2 or seq != 1:
        raise ValueError('unexpected socket destroy acknowledgment')
    error = struct.unpack_from('=i', reply, 16)[0]
    if error:
        raise OSError(-error, 'socket destroy failed')
