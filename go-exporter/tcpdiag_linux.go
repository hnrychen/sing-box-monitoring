package main

import (
	"encoding/binary"
	"fmt"
	"math"
	"net/netip"
	"sort"
	"syscall"
	"time"
)

var native = binary.NativeEndian

func decodeRecord(payload []byte) (*Record, error) {
	if len(payload) < 72 {
		return nil, fmt.Errorf("short inet_diag message")
	}
	family, state := payload[0], payload[1]
	if (family != 2 && family != 10) || state != 1 {
		return nil, nil
	}
	address := func(raw []byte) string {
		if family == 2 {
			return netip.AddrFrom4([4]byte(raw[:4])).String()
		}
		return netip.AddrFrom16([16]byte(raw[:16])).Unmap().String()
	}
	cookie := [2]uint32{native.Uint32(payload[44:48]), native.Uint32(payload[48:52])}
	r := &Record{LocalIP: address(payload[8:24]), PeerIP: address(payload[24:40]), LocalPort: int(binary.BigEndian.Uint16(payload[4:6])), PeerPort: int(binary.BigEndian.Uint16(payload[6:8])), UID: native.Uint32(payload[64:68]), Cookie: &cookie, ReceiveQueue: float64(native.Uint32(payload[56:60])), SendQueue: float64(native.Uint32(payload[60:64])), AckAge: math.Inf(1), PayloadAge: math.Inf(1)}
	for offset := 72; offset+4 <= len(payload); {
		length, kind := int(native.Uint16(payload[offset:offset+2])), native.Uint16(payload[offset+2:offset+4])&0x3fff
		if length < 4 || offset+length > len(payload) {
			return nil, fmt.Errorf("invalid inet_diag attribute")
		}
		if kind == 2 && length >= 80 {
			info := payload[offset+4 : offset+length]
			u32 := func(pos int) uint32 { return native.Uint32(info[pos : pos+4]) }
			r.RTT = float64(u32(68)) / 1e6
			r.Variation = float64(u32(72)) / 1e6
			r.AckAge = float64(u32(56)) / 1000
			r.PayloadAge = float64(min(u32(44), u32(52))) / 1000
			for pos, target := range map[int]**uint32{16: &r.MSS, 80: &r.Cwnd, 100: &r.Retrans, 144: &r.Notsent} {
				if len(info) >= pos+4 {
					v := u32(pos)
					*target = &v
				}
			}
		}
		offset += (length + 3) &^ 3
	}
	return r, nil
}

// Reject partial dumps, truncation, sequence mismatch and byte/socket overflow.
func parsePacket(packet []byte, sequence uint32, family, port int, records *[]Record, limit int) (bool, error) {
	done := false
	for offset := 0; offset+16 <= len(packet); {
		length := int(native.Uint32(packet[offset : offset+4]))
		kind := native.Uint16(packet[offset+4 : offset+6])
		flags := native.Uint16(packet[offset+6 : offset+8])
		seq := native.Uint32(packet[offset+8 : offset+12])
		if length < 16 || offset+length > len(packet) || seq != sequence || flags&0x10 != 0 {
			return false, fmt.Errorf("invalid/interrupted netlink dump")
		}
		payload := packet[offset+16 : offset+length]
		if kind == 2 || kind == 3 {
			status := int32(0)
			if len(payload) >= 4 {
				status = int32(native.Uint32(payload[:4]))
			}
			if status != 0 {
				if family == 10 && (status == -int32(syscall.EAFNOSUPPORT) || status == -int32(syscall.ENOENT)) {
					done = true
				} else {
					return false, fmt.Errorf("kernel socket dump error")
				}
			}
			if kind == 3 {
				done = true
			}
		} else if kind == 20 {
			r, err := decodeRecord(payload)
			if err != nil {
				return false, err
			}
			if r != nil && r.LocalPort == port {
				*records = append(*records, *r)
				if len(*records) > limit {
					return false, fmt.Errorf("socket budget exceeded")
				}
			}
		}
		offset += (length + 3) &^ 3
	}
	return done, nil
}
func socketSnapshot(ports map[int]bool, limit int) ([]Record, error) {
	if len(ports) > 32 {
		return nil, fmt.Errorf("port budget exceeded")
	}
	ordered := []int{}
	for port := range ports {
		if port < 1 || port > 65535 {
			return nil, fmt.Errorf("invalid port")
		}
		ordered = append(ordered, port)
	}
	sort.Ints(ordered)
	records := []Record{}
	size := 0
	sequence := uint32(0)
	deadline := time.Now().Add(3 * time.Second)
	for _, port := range ordered {
		for _, family := range []int{2, 10} {
			sequence++
			request := make([]byte, 72)
			native.PutUint32(request[:4], 72)
			native.PutUint16(request[4:6], 20)
			native.PutUint16(request[6:8], 0x301)
			native.PutUint32(request[8:12], sequence)
			request[16] = byte(family)
			request[17] = 6
			request[18] = 2
			native.PutUint32(request[20:24], 2)
			binary.BigEndian.PutUint16(request[24:26], uint16(port))
			native.PutUint32(request[64:68], 0xffffffff)
			native.PutUint32(request[68:72], 0xffffffff)
			fd, err := syscall.Socket(syscall.AF_NETLINK, syscall.SOCK_RAW|syscall.SOCK_CLOEXEC, 4)
			if err != nil {
				return nil, err
			}
			err = func() error {
				defer syscall.Close(fd)
				if err := syscall.Bind(fd, &syscall.SockaddrNetlink{Family: syscall.AF_NETLINK}); err != nil {
					return err
				}
				if err := syscall.Sendto(fd, request, 0, &syscall.SockaddrNetlink{Family: syscall.AF_NETLINK}); err != nil {
					return err
				}
				buffer := make([]byte, 65536)
				for {
					remaining := time.Until(deadline)
					if remaining <= 0 {
						return fmt.Errorf("TCP deadline exceeded")
					}
					tv := syscall.NsecToTimeval(remaining.Nanoseconds())
					if err := syscall.SetsockoptTimeval(fd, syscall.SOL_SOCKET, syscall.SO_RCVTIMEO, &tv); err != nil {
						return err
					}
					n, _, flags, sender, err := syscall.Recvmsg(fd, buffer, nil, 0)
					if err != nil {
						return err
					}
					address, ok := sender.(*syscall.SockaddrNetlink)
					if !ok || address.Pid != 0 || flags&syscall.MSG_TRUNC != 0 {
						return fmt.Errorf("untrusted/truncated kernel dump")
					}
					size += n
					if size > maxResponse {
						return fmt.Errorf("TCP byte budget exceeded")
					}
					done, err := parsePacket(buffer[:n], sequence, family, port, &records, limit)
					if err != nil {
						return err
					}
					if done {
						return nil
					}
				}
			}()
			if err != nil {
				return nil, err
			}
		}
	}
	return records, nil
}
