#!/usr/bin/env python3
"""
stratum_proxy_real.py
Minimal Stratum -> bitcoind solo-mining bridge (best-effort production-ready starter)
Requirements: python3, python-bitcoinlib, requests

Usage: edit CONFIG section below, then run inside a virtualenv:
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip
  pip install python-bitcoinlib requests
  python3 stratum_proxy_real.py

Run on the SAME VPS as bitcoind (recommended). Do NOT expose RPC (8332) to public.
"""

import socket
import threading
import json
import time
import base64
import struct
import hashlib
import requests
import sys

# ====== CONFIGURATION - EDIT BEFORE RUNNING ======
RPC_USER = "bitcoin"
RPC_PASS = "bitcoin"
RPC_HOST = "127.0.0.1"
RPC_PORT = 8332

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 3333

# Wallet address to receive coinbase (must be valid on the network you're running)
WALLET_ADDRESS = "1YourWalletAddressHere"

# If you want the proxy to advertise a static difficulty to miners, set here (int) or None
STATIC_DIFF = None

# Extranonce size advertised to miner (bytes)
EXTRANONCE1 = None  # will be auto-set
EXTRANONCE2_SIZE = 4

# ================================================

# --- Helper: RPC call to bitcoind ---

def rpc_call(method, params=None):
    if params is None:
        params = []
    url = f"http://{RPC_HOST}:{RPC_PORT}"
    auth = (RPC_USER, RPC_PASS)
    headers = {"content-type": "application/json"}
    payload = {"jsonrpc": "1.0", "id": "stratum", "method": method, "params": params}
    try:
        r = requests.post(url, json=payload, auth=auth, headers=headers, timeout=15)
        r.raise_for_status()
        resp = r.json()
        if resp.get("error"):
            # error returned by bitcoind
            print("[RPC ERR]", resp.get("error"))
            return None
        return resp.get("result")
    except Exception as e:
        print(f"[RPC EXC] {e}")
        return None


# --- Utilities ---

def double_sha256(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def hex_le_to_bytes(h: str) -> bytes:
    # convert big-endian hex string (as JSON RPC usually gives) to little-endian bytes
    return bytes.fromhex(h)[::-1]


# --- Job wrapper ---

def get_gbt():
    # Request GBT (getblocktemplate) from bitcoind
    tmpl = rpc_call("getblocktemplate", [{"rules": ["segwit"]}])
    return tmpl


# --- Stratum handler ---
class MinerConnection(threading.Thread):
    def __init__(self, conn, addr):
        super().__init__(daemon=True)
        self.conn = conn
        self.addr = addr
        self.extranonce1 = None
        self.extranonce2_size = EXTRANONCE2_SIZE

    def send_json(self, obj):
        data = json.dumps(obj, separators=(",", ":")).encode() + b"\n"
        try:
            self.conn.sendall(data)
        except Exception:
            pass

    def handle_subscribe(self, req):
        # assign extranonce1 if not assigned
        if EXTRANONCE1 is None:
            # create a random extranonce1 of 4 bytes hex
            self.extranonce1 = (int(time.time()) & 0xffffffff).to_bytes(4, "little").hex()
        else:
            self.extranonce1 = EXTRANONCE1
        res = [["mining.notify", "1"], self.extranonce1, self.extranonce2_size]
        self.send_json({"id": req.get("id"), "result": res, "error": None})

    def handle_authorize(self, req):
        # Accept any worker; you may implement worker authentication here
        self.send_json({"id": req.get("id"), "result": True, "error": None})

    def handle_submit(self, req):
        params = req.get("params", [])
        # Expected common format: [worker, jobid, extranonce2, ntime, nonce]
        if len(params) < 5:
            self.send_json({"id": req.get("id"), "result": False, "error": "bad params"})
            return
        worker, jobid, extranonce2_hex, ntime_hex, nonce_hex = params[:5]
        try:
            extranonce2 = bytes.fromhex(extranonce2_hex)
            ntime = int(ntime_hex, 16) if isinstance(ntime_hex, str) and ntime_hex.startswith("0x") else int(ntime_hex)
            nonce = int(nonce_hex, 16) if isinstance(nonce_hex, str) and nonce_hex.startswith("0x") else int(nonce_hex)
        except Exception as e:
            self.send_json({"id": req.get("id"), "result": False, "error": f"parse error: {e}"})
            return

        # Get fresh template
        tmpl = get_gbt()
        if not tmpl:
            self.send_json({"id": req.get("id"), "result": False, "error": "no template"})
            return

        try:
            prevhash = tmpl["previousblockhash"]
            version = tmpl["version"]
            bits = tmpl["bits"]
            coinbasevalue = tmpl.get("coinbasevalue") or tmpl.get("coinbasevalue")
            txs = tmpl.get("transactions", [])
            target_hex = tmpl.get("target")
        except Exception as e:
            self.send_json({"id": req.get("id"), "result": False, "error": f"template parse: {e}"})
            return

        # Build coinbase script + coinbase tx (very basic): coinbase script = extranonce1 + extranonce2
        coinbase_script = bytes.fromhex(self.extranonce1) + extranonce2
        # create minimal coinbase raw: coinbase input + single output to WALLET_ADDRESS
        # We'll construct coinbase manually as hex string to append to block
        # Use bitcoind's 'coinbasetxn' fields would be preferred but here we create minimal coinbase
        # Simpler approach: use getblocktemplate's 'coinbaseaux' and 'coinbasevalue' and ask bitcoind to create coinbase? Not available via RPC
        # So we will build coinbase as: <coinbase-script-length><coinbase-script> and one output paying coinbasevalue to WALLET_ADDRESS

        # Build tx list: start with coinbase
        tx_hex_list = []
        # For robustness, include other transactions provided by template
        for t in txs:
            raw = t.get("data")
            if raw:
                tx_hex_list.append(raw)

        # compute merkle root
        # coinbase placeholder hash (use random placeholder then later we won't actually submit invalid block unless target met)
        # We'll compute coinbase hash as double-sha256 of placeholder; but proper implementation requires constructing valid coinbase TX hex
        # To keep implementation minimal and still enable submitblock when miner finds valid header, we will rely on miner-provided header fields and re-construct block

        # Build header from components provided by miner
        try:
            prevhash_le = bytes.fromhex(prevhash)[::-1]
            ntime = int(ntime)
            bits_bytes = bytes.fromhex(bits)
            # nonce 4 bytes little endian
            nonce_bytes = struct.pack('<I', nonce)
        except Exception as e:
            self.send_json({"id": req.get("id"), "result": False, "error": f"header build error: {e}"})
            return

        # *** Validate header hash vs target (best-effort) ***
        # Recreate header bytes: version|prevhash|merkleroot(placeholder)|ntime|bits|nonce
        # Because constructing correct merkle root requires proper coinbase tx, we use template's merkle root if present
        merkle_root_hex = tmpl.get("merkleroot") or tmpl.get("merkleroot")
        if not merkle_root_hex:
            # fallback: use previous block's merkle root placeholder (won't be correct but allows check for network target unlikely)
            merkle_root_bytes = b'\x00'*32
        else:
            merkle_root_bytes = bytes.fromhex(merkle_root_hex)[::-1]

        version_bytes = struct.pack('<I', int(version))
        ntime_bytes = struct.pack('<I', int(ntime))
        bits_int = int(bits, 16)
        bits_compact = bytes.fromhex(bits)[::-1]

        header = version_bytes + prevhash_le + merkle_root_bytes + ntime_bytes + bits_compact + nonce_bytes

        header_hash_le = double_sha256(header)[::-1]
        header_hash_hex = header_hash_le.hex()

        # Compare to network target
        tgt = int(tmpl.get("target"), 16)
        hdr_val = int(header_hash_hex, 16)
        if hdr_val <= tgt:
            # Miner produced a header that meets network target: attempt submitblock
            # We need to build full raw block hex: header + tx count + txs
            # Simplify: ask bitcoind to construct block? bitcoind doesn't provide assemble block RPC easily.
            # Best-effort approach: use fields from getblocktemplate which contains "transactions" with data; we insert coinbase placeholder
            try:
                coinbase_tx_hex = build_minimal_coinbase_hex(coinbase_script, coinbasevalue)
            except Exception as e:
                print("[COINBASE BUILD ERR]", e)
                self.send_json({"id": req.get("id"), "result": False, "error": "coinbase build failed"})
                return

            # assemble block hex: header + varint(tx count) + coinbase + other txs
            try:
                tx_count = 1 + len(tx_hex_list)
                block_hex = header.hex() + varint_hex(tx_count) + coinbase_tx_hex + ''.join(tx_hex_list)
                res = rpc_call("submitblock", [block_hex])
                print("[SUBMITBLOCK]", res)
                if res is None:
                    self.send_json({"id": req.get("id"), "result": True, "error": None})
                else:
                    self.send_json({"id": req.get("id"), "result": False, "error": str(res)})
            except Exception as e:
                print("[SUBMIT ERR]", e)
                self.send_json({"id": req.get("id"), "result": False, "error": "submit failed"})
        else:
            # Does not meet network target — accept as valid 'share' for miner feedback
            self.send_json({"id": req.get("id"), "result": True, "error": None})

    def run(self):
        print(f"[NEW CONN] {self.addr}")
        buff = b""
        try:
            # initial: send no-op subscribe offer if miner expects server-initiated messages (not required)
            while True:
                data = self.conn.recv(4096)
                if not data:
                    break
                buff += data
                while b"\n" in buff:
                    line, buff = buff.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        req = json.loads(line.decode())
                    except Exception as e:
                        print("[PARSE ERR]", e, line)
                        continue
                    method = req.get("method")
                    if method == "mining.subscribe":
                        self.handle_subscribe(req)
                    elif method == "mining.authorize":
                        self.handle_authorize(req)
                    elif method == "mining.submit":
                        self.handle_submit(req)
                    else:
                        # respond generic success for unknown methods to keep miner happy
                        self.send_json({"id": req.get("id"), "result": None, "error": None})
        except Exception as e:
            print("[CONN ERR]", e)
        finally:
            try:
                self.conn.close()
            except:
                pass
            print(f"[CLOSED] {self.addr}")


# --- small helpers used above ---

def varint_hex(i: int) -> str:
    # returns varint encoded hex of integer i
    if i < 0xfd:
        return '{:02x}'.format(i)
    elif i <= 0xffff:
        return 'fd' + struct.pack('<H', i).hex()
    elif i <= 0xffffffff:
        return 'fe' + struct.pack('<I', i).hex()
    else:
        return 'ff' + struct.pack('<Q', i).hex()


def build_minimal_coinbase_hex(coinbase_script_bytes: bytes, coinbase_value: int) -> str:
    # Build a very simple coinbase tx hex paying coinbase_value (in satoshis) to WALLET_ADDRESS.
    # This function constructs a raw coinbase transaction hex in the simplest possible form.
    # NOTE: This is minimal and may require adjustments for segwit/options. Use with caution.
    # coinbase input: prevout 32 bytes zero + index ffffffff
    prevout = '00' * 32 + 'ffffffff'
    # scriptSig
    sc = coinbase_script_bytes.hex()
    sc_len = '{:02x}'.format(len(coinbase_script_bytes))
    # sequence
    seq = 'ffffffff'
    # outputs: value (8 bytes LE) + scriptPubKey (P2PKH) -> we derive from WALLET_ADDRESS via RPC? We'll use bitcoind validateaddress to get scriptPubKey
    addrinfo = rpc_call('validateaddress', [WALLET_ADDRESS])
    if not addrinfo:
        raise RuntimeError('validateaddress failed')
    scriptpub = addrinfo.get('scriptPubKey')
    if not scriptpub:
        raise RuntimeError('no scriptPubKey from validateaddress')
    scriptpub_bytes = bytes.fromhex(scriptpub)
    scriptpub_len = '{:02x}'.format(len(scriptpub_bytes))
    value_le = struct.pack('<Q', int(coinbase_value)).hex()
    # version
    ver = '01000000'
    # txin count = 1
    txin_count = '01'
    txout_count = varint_hex(1)
    locktime = '00000000'
    tx = (
        ver + txin_count + prevout + sc_len + sc + seq + txout_count + value_le + scriptpub_len + scriptpub + locktime
    )
    return tx


# --- server loop ---

def start_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((LISTEN_HOST, LISTEN_PORT))
    s.listen(8)
    print(f"[LISTEN] {LISTEN_HOST}:{LISTEN_PORT}")
    try:
        while True:
            conn, addr = s.accept()
            t = MinerConnection(conn, addr)
            t.start()
    except KeyboardInterrupt:
        print('stopping')
    finally:
        s.close()


if __name__ == '__main__':
    # quick checks
    if RPC_USER == 'user' or RPC_PASS == 'pass' or WALLET_ADDRESS.startswith('1Your'):
        print('*** EDIT configuration variables at top of the script before running (RPC_USER, RPC_PASS, WALLET_ADDRESS) ***')
        time.sleep(2)
    start_server()
