"""Pure-Python Multicast DNS (mDNS) discovery for Agentic Memory.

Advertises the sync server and browses for other peer sync servers on the local network.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SERVICE_NAME = "_agentic-memory._tcp.local"


def encode_name(name: str) -> bytes:
    parts = name.split(".")
    res = b""
    for part in parts:
        if not part:
            continue
        part_bytes = part.encode("utf-8")
        res += bytes([len(part_bytes)]) + part_bytes
    res += b"\x00"
    return res


def decode_name(data: bytes, offset: int) -> Tuple[str, int]:
    parts = []
    curr = offset
    while True:
        if curr >= len(data):
            break
        length = data[curr]
        if length == 0:
            curr += 1
            break
        if (length & 0xc0) == 0xc0:
            # Compression pointer
            pointer = ((length & 0x3f) << 8) | data[curr + 1]
            curr += 2
            name, _ = decode_name(data, pointer)
            parts.append(name)
            return ".".join(parts), curr
        else:
            curr += 1
            parts.append(data[curr:curr + length].decode("utf-8", errors="ignore"))
            curr += length
    return ".".join(parts), curr


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip: str = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class MDNSAdvertiser:
    """Advertises the Agentic Memory Sync Server over mDNS."""

    def __init__(self, agent_id: str, port: int) -> None:
        self.agent_id = agent_id
        self.port = port
        self.ip = get_local_ip()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.sock: Optional[socket.socket] = None

    def start(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass

        # Bind to multicast port
        self.sock.bind(("", MDNS_PORT))

        # Join the multicast group
        mreq = struct.pack("4sl", socket.inet_aton(MDNS_GROUP), socket.INADDR_ANY)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="MDNSAdvertiser")
        self.thread.start()
        logger.info("mDNS Advertiser started for agent %s on %s:%d", self.agent_id, self.ip, self.port)

    def _run_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                assert self.sock is not None
                self.sock.settimeout(1.0)
                data, addr = self.sock.recvfrom(2048)
                self._handle_packet(data, addr)
            except socket.timeout:
                continue
            except Exception as e:
                if not self.stop_event.is_set():
                    logger.debug("mDNS Advertiser error: %s", e)

    def _handle_packet(self, data: bytes, addr: Tuple[str, int]) -> None:
        if len(data) < 12:
            return

        # Parse DNS header
        # ID (2B), Flags (2B), QDCOUNT (2B), ANCOUNT (2B), NSCOUNT (2B), ARCOUNT (2B)
        flags = struct.unpack("!H", data[2:4])[0]
        is_query = (flags & 0x8000) == 0

        if not is_query:
            return

        qdcount = struct.unpack("!H", data[4:6])[0]
        offset = 12

        # Read questions
        for _ in range(qdcount):
            name, next_offset = decode_name(data, offset)
            qtype, qclass = struct.unpack("!HH", data[next_offset:next_offset + 4])
            offset = next_offset + 4

            if name.lower() == SERVICE_NAME.lower():
                self._send_response(addr)
                break

    def _send_response(self, addr: Tuple[str, int]) -> None:
        # Construct DNS authoritative response
        # Header: ID=0, Flags=0x8400 (Response, Auth), QD=0, AN=1, NS=0, AR=3
        header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 3)

        # Answer Section: PTR Record
        ptr_name = encode_name(SERVICE_NAME)
        inst_name = f"{self.agent_id}.{SERVICE_NAME}"
        ptr_rdata = encode_name(inst_name)
        ptr_record = ptr_name + struct.pack("!HHIH", 12, 1, 120, len(ptr_rdata)) + ptr_rdata

        # Additional Section: SRV Record
        srv_name = encode_name(inst_name)
        target_host = f"{self.agent_id}.local"
        target_host_enc = encode_name(target_host)
        srv_rdata = struct.pack("!HHH", 0, 0, self.port) + target_host_enc
        srv_record = srv_name + struct.pack("!HHIH", 33, 1, 120, len(srv_rdata)) + srv_rdata

        # Additional Section: TXT Record
        txt_rdata = b""
        for kv in [f"agent_id={self.agent_id}"]:
            kv_bytes = kv.encode("utf-8")
            txt_rdata += bytes([len(kv_bytes)]) + kv_bytes
        txt_record = srv_name + struct.pack("!HHIH", 16, 1, 120, len(txt_rdata)) + txt_rdata

        # Additional Section: A Record
        a_name = encode_name(target_host)
        a_rdata = socket.inet_aton(self.ip)
        a_record = a_name + struct.pack("!HHIH", 1, 1, 120, len(a_rdata)) + a_rdata

        response = header + ptr_record + srv_record + txt_record + a_record

        # Send response packet back to mDNS multicast group
        try:
            # We use a separate socket to send to group
            send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            send_sock.sendto(response, (MDNS_GROUP, MDNS_PORT))
            send_sock.close()
        except Exception as e:
            logger.debug("Failed to send mDNS response: %s", e)

    def stop(self) -> None:
        self.stop_event.set()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=2.0)


class MDNSBrowser:
    """Browses for Agentic Memory Sync Servers over mDNS."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.sock: Optional[socket.socket] = None
        self.discovered_peers: Dict[str, Tuple[str, int, float]] = {}  # agent_id -> (ip, port, last_seen)
        self._lock = threading.Lock()

    def start(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass

        self.sock.bind(("", MDNS_PORT))

        mreq = struct.pack("4sl", socket.inet_aton(MDNS_GROUP), socket.INADDR_ANY)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="MDNSBrowser")
        self.thread.start()
        logger.info("mDNS Browser started")

    def query(self) -> None:
        """Broadcast a PTR query for the service."""
        # Query Header: ID=0, Flags=0x0000 (Query), QD=1, AN=0, NS=0, AR=0
        header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
        question = encode_name(SERVICE_NAME) + struct.pack("!HH", 12, 1)  # PTR, IN

        packet = header + question
        try:
            send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            send_sock.sendto(packet, (MDNS_GROUP, MDNS_PORT))
            send_sock.close()
        except Exception as e:
            logger.debug("Failed to send mDNS query: %s", e)

    def _run_loop(self) -> None:
        # Periodic query trigger
        last_query = 0.0
        while not self.stop_event.is_set():
            now = time.time()
            if now - last_query > 10.0:
                self.query()
                last_query = now

            try:
                assert self.sock is not None
                self.sock.settimeout(1.0)
                data, addr = self.sock.recvfrom(2048)
                self._handle_packet(data)
            except socket.timeout:
                continue
            except Exception as e:
                if not self.stop_event.is_set():
                    logger.debug("mDNS Browser error: %s", e)

    def _handle_packet(self, data: bytes) -> None:
        if len(data) < 12:
            return

        flags = struct.unpack("!H", data[2:4])[0]
        is_response = (flags & 0x8000) != 0

        if not is_response:
            return

        qdcount = struct.unpack("!H", data[4:6])[0]
        ancount = struct.unpack("!H", data[6:8])[0]
        arcount = struct.unpack("!H", data[10:12])[0]

        offset = 12
        # Skip questions if any
        for _ in range(qdcount):
            _, next_offset = decode_name(data, offset)
            offset = next_offset + 4

        records = []
        # Parse Answer and Additional records
        for _ in range(ancount + arcount):
            if offset >= len(data):
                break
            name, next_offset = decode_name(data, offset)
            if next_offset + 10 > len(data):
                break
            rtype, rclass, ttl, rdlen = struct.unpack("!HHIH", data[next_offset:next_offset + 10])
            rdata = data[next_offset + 10:next_offset + 10 + rdlen]
            offset = next_offset + 10 + rdlen
            records.append((name, rtype, rclass, rdata))

        # Reconstruct peer info from parsed records
        srv_port = None
        srv_target = None
        a_ip = None
        agent_id = None

        for name, rtype, rclass, rdata in records:
            if rtype == 33:  # SRV
                if len(rdata) >= 6:
                    srv_port = struct.unpack("!H", rdata[4:6])[0]
                    srv_target, _ = decode_name(rdata, 6)
            elif rtype == 16:  # TXT
                # Parse key-values
                txt_offset = 0
                while txt_offset < len(rdata):
                    length = rdata[txt_offset]
                    txt_offset += 1
                    kv = rdata[txt_offset:txt_offset + length].decode("utf-8", errors="ignore")
                    txt_offset += length
                    if "agent_id=" in kv:
                        agent_id = kv.split("=", 1)[1]
            elif rtype == 1:  # A
                if len(rdata) == 4:
                    a_ip = socket.inet_ntoa(rdata)

        # If we have Port and IP, we can resolve the peer
        if srv_target and (agent_id or srv_target.split(".")[0]):
            resolved_agent_id = agent_id or srv_target.split(".")[0]
            resolved_ip = a_ip or "127.0.0.1"
            if srv_port:
                with self._lock:
                    self.discovered_peers[resolved_agent_id] = (resolved_ip, srv_port, time.time())

    def get_peers(self) -> List[Dict[str, Any]]:
        now = time.time()
        peers_list = []
        with self._lock:
            # Filter out stale peers (older than 30s)
            for aid, (ip, port, last_seen) in list(self.discovered_peers.items()):
                if now - last_seen < 30.0:
                    peers_list.append({
                        "agent_id": aid,
                        "ip": ip,
                        "port": port,
                        "url": f"http://{ip}:{port}"
                    })
                else:
                    self.discovered_peers.pop(aid, None)
        return peers_list

    def stop(self) -> None:
        self.stop_event.set()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=2.0)
