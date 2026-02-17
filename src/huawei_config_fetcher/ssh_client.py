from __future__ import annotations

import base64
import hashlib
import time
import socket
from typing import Callable, Optional, Tuple

import paramiko

from .models import Device


HostKeyCallback = Callable[[str, str, Optional[str]], bool]


def _fingerprint_sha256(key: paramiko.PKey) -> str:
    raw = key.asbytes()
    digest = hashlib.sha256(raw).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _read_until_quiet(channel: paramiko.Channel, quiet: float, overall: float) -> str:
    chunks = []
    start = time.time()
    last_data = time.time()

    while (time.time() - start) < overall:
        if channel.recv_ready():
            data = channel.recv(65535)
            if not data:
                break
            chunks.append(data)
            last_data = time.time()
        else:
            if (time.time() - last_data) >= quiet:
                break
            time.sleep(0.1)

    return b"".join(chunks).decode("utf-8", errors="ignore")

def fetch_host_fingerprint(device: Device, connect_timeout: float = 10.0) -> Tuple[Optional[str], Optional[str]]:
    transport: Optional[paramiko.Transport] = None
    try:
        sock = socket.create_connection((device.host, device.port), timeout=connect_timeout)
        transport = paramiko.Transport(sock)
        transport.banner_timeout = connect_timeout
        transport.start_client(timeout=connect_timeout)

        remote_key = transport.get_remote_server_key()
        fingerprint = _fingerprint_sha256(remote_key)
        return fingerprint, None
    except Exception as exc:
        return None, str(exc)
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

def check_host_key(
    device: Device,
    known_hosts: dict,
    host_key_callback: HostKeyCallback,
    connect_timeout: float = 10.0,
) -> Tuple[bool, Optional[str]]:
    transport: Optional[paramiko.Transport] = None
    try:
        sock = socket.create_connection((device.host, device.port), timeout=connect_timeout)
        transport = paramiko.Transport(sock)
        transport.banner_timeout = connect_timeout
        transport.start_client(timeout=connect_timeout)

        remote_key = transport.get_remote_server_key()
        fingerprint = _fingerprint_sha256(remote_key)
        host_id = f"{device.host}:{device.port}"
        known_fp = known_hosts.get(host_id)

        if known_fp is None:
            if not host_key_callback(host_id, fingerprint, None):
                return False, "host-key"
            known_hosts[host_id] = fingerprint
        elif known_fp != fingerprint:
            if not host_key_callback(host_id, fingerprint, known_fp):
                return False, "host-key"
            known_hosts[host_id] = fingerprint

        return True, None
    except Exception as exc:
        return False, str(exc)
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass


def fetch_device_config(
    device: Device,
    password: str,
    known_hosts: dict,
    host_key_callback: HostKeyCallback,
    connect_timeout: float = 10.0,
    command_timeout: float = 120.0,
) -> Optional[str]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=device.host,
            port=device.port,
            username=device.username,
            password=password,
            timeout=connect_timeout,
            banner_timeout=connect_timeout,
            auth_timeout=connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )

        transport = client.get_transport()
        if transport is None:
            return None

        remote_key = transport.get_remote_server_key()
        fingerprint = _fingerprint_sha256(remote_key)
        host_id = f"{device.host}:{device.port}"
        known_fp = known_hosts.get(host_id)

        if known_fp is None:
            if not host_key_callback(host_id, fingerprint, None):
                return None
            known_hosts[host_id] = fingerprint
        elif known_fp != fingerprint:
            if not host_key_callback(host_id, fingerprint, known_fp):
                return None
            known_hosts[host_id] = fingerprint

        channel = client.invoke_shell()
        _read_until_quiet(channel, quiet=0.5, overall=5.0)

        channel.send("screen-length 0 temporary\n")
        _read_until_quiet(channel, quiet=0.5, overall=5.0)

        channel.send("display current-configuration\n")
        output = _read_until_quiet(channel, quiet=2.0, overall=command_timeout)

        channel.send("quit\n")
        return output.strip()
    finally:
        try:
            client.close()
        except Exception:
            pass
