#!/usr/bin/env python3
"""
Copy-trading bot:
- Subscribe to parent wallet logs via Helius WebSocket (logsSubscribe with "mentions")
- For each tx touching the parent, fetch the confirmed transaction
- If the tx looks like a Jupiter swap, extract the outputMint (token bought)
- Create and execute a Jupiter order to buy same token with configured amount
"""

import asyncio
import json
import re
import time
import base64
import requests
import os
from typing import Optional, Dict

import websockets
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.async_api import AsyncClient
from solders.signature import Signature
from solders.pubkey import Pubkey

# ---------- CONFIG ----------
RPC_URL = "https://mainnet.helius-rpc.com/?api-key=988ff6ca-66d4-402c-8701-179576cf3acc"
WS_URL = "wss://mainnet.helius-rpc.com/?api-key=988ff6ca-66d4-402c-8701-179576cf3acc"
JUP_ORDER_URL = "https://lite-api.jup.ag/ultra/v1/order"
JUP_EXEC_URL = "https://lite-api.jup.ag/ultra/v1/execute"

SOL_MINT = "So11111111111111111111111111111111111111112"

# parent wallet you copy
PARENT_WALLET_STR = "DfDuzjdXojWzjCXXhzj"

# your local signer (load below)
PRIVATE_KEY_BASE58 = "3JTv58"  # set as env or modify below

# copy configuration
MIN_TRADE_LAMPORTS = 10000000      # minimum lamports to copy (0.01 SOL)
COPY_AMOUNT_LAMPORTS = 2000000   # lamports to use per copy trade (0.002 SOL)
COOLDOWN_SECONDS = 2             # cooldown after copying
MAX_ORDER_RETRIES = 6

# thresholds
DUST_THRESHOLD_UI = 0.01   # if you use balance logic, but here we rely on logs/tx parsing


# ---------- utilities ----------
async_client = AsyncClient(RPC_URL)
parent_pub = Pubkey.from_string(PARENT_WALLET_STR)


def regex_extract_mint_from_log_line(line: str) -> Optional[str]:
    """
    Try to extract a base58 mint/pubkey from a log line.
    Many Jupiter logs contain `inputMint: <mint>` or JSON-like fragments.
    Returns the first candidate pubkey looking like base58 (approx length 32-44).
    """
    # common pattern: inputMint: <PUBKEY>
    m = re.search(r"(?:inputMint|outputMint)\s*[:=]\s*([A-Za-z0-9]{32,44})", line)
    if m:
        return m.group(1)
    # try JSON-like: "outputMint":"<pubkey>"
    m2 = re.search(r'"(?:inputMint|outputMint)"\s*:\s*"([A-Za-z0-9]{32,44})"', line)
    if m2:
        return m2.group(1)
    # sometimes the mint is printed alone in logs - try to find any pubkey-like token
    m3 = re.search(r"([A-Za-z0-9]{43,44})", line)
    if m3:
        return m3.group(1)
    return None


def detect_jupiter_from_logs(logs: Optional[list]) -> bool:
    if not logs:
        return False
    for l in logs:
        low = l.lower()
        if "jupiter" in low or "jup.ag" in low or "jup" in low:
            return True
        if "inputmint" in low or "outputmint" in low:
            return True
    return False


async def fetch_confirmed_tx_and_meta(sig_str: str) -> Optional[dict]:
    """Fetch confirmed transaction with jsonParsed encoding"""
    try:
        res = await async_client.get_transaction(sig_str, encoding="jsonParsed", commitment="confirmed")
        if res.value:
            return res.value
    except Exception:
        # swallow errors to avoid crashing on transient network issues
        pass
    return None


def create_jupiter_order(input_mint: str, output_mint: str, amount: str, taker: str) -> Optional[dict]:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount,
        "taker": taker,
        "mode": "swap"
    }
    try:
        r = requests.get(JUP_ORDER_URL, params=params, timeout=8)
        if r.status_code == 200:
            return r.json()
        else:
            print("create_order status", r.status_code, "body:", r.text[:300])
    except Exception as e:
        print("create_order error:", e)
    return None


def sign_and_execute_order(order_res: dict, signer: Keypair) -> Optional[dict]:
    """Sign and execute the Jupiter order (blocking). Returns Jupiter execute response."""
    if not order_res or not isinstance(order_res, dict):
        return None

    if not order_res.get("transaction") or not order_res.get("requestId"):
        return None

    unsigned_b64 = order_res["transaction"]
    request_id = order_res["requestId"]
    try:
        raw_tx = base64.b64decode(unsigned_b64)
    except Exception as e:
        print("base64 decode failed:", e)
        return None

    try:
        tx = VersionedTransaction.from_bytes(raw_tx)
    except Exception as e:
        print("from_bytes failed:", e)
        return None

    try:
        # Signer may need to sign: find index if present
        pubkeys = tx.message.account_keys
        signer_pub = signer.pubkey()
        if signer_pub in pubkeys:
            signer_index = pubkeys.index(signer_pub)
            message_bytes = to_bytes_versioned(tx.message)
            signature = signer.sign_message(message_bytes)
            sigs = list(tx.signatures)
            if signer_index >= len(sigs):
                sigs += [b""] * (signer_index - len(sigs) + 1)
            sigs[signer_index] = signature
            tx.signatures = sigs
            # proceed
        else:
            # No taker required to sign (rare) — send as-is
            pass
    except Exception as e:
        print("signing failed:", e)
        return None

    signed_b64 = base64.b64encode(bytes(tx)).decode()
    payload = {"signedTransaction": signed_b64, "requestId": request_id}
    try:
        r = requests.post(JUP_EXEC_URL, json=payload, timeout=15)
        if r.status_code == 200:
            return r.json()
        else:
            print("execute status", r.status_code, r.text[:300])
    except Exception as e:
        print("execute network error:", e)
    return None


async def handle_parent_tx(sig_str: str, logs: list, signer: Keypair, wallet_pubkey_str: str):
    """
    Given a tx signature & logs, confirm it's a Jupiter swap, extract the bought token (outputMint),
    then perform a copy trade (SOL -> outputMint) for the configured amount.
    """
    print(f"\n[EVENT] Parent tx signature: {sig_str}")

    # 1) quick heuristic from logs (fast)
    if not detect_jupiter_from_logs(logs):
        print("[SKIP] Not a Jupiter tx by log heuristics.")
        return False

    # 2) fetch confirmed tx parsed to extract more reliable info
    txinfo = await fetch_confirmed_tx_and_meta(sig_str)
    if not txinfo:
        print("[WARN] Could not fetch confirmed tx. Skipping.")
        return False

    parsed_logs = txinfo.get("meta", {}).get("logMessages") or []
    # 3) try to parse outputMint from logs
    output_mint = None
    input_mint = None

    # parse log messages for inputMint/outputMint tokens (there are varied formats)
    for ln in parsed_logs:
        # attempt to capture outputMint and inputMint
        if "outputMint" in ln or "inputMint" in ln or "outputmint" in ln or "inputmint" in ln:
            maybe = regex_extract_mint_from_log_line(ln)
            if maybe:
                # If both appear, detect which is output vs input
                low = ln.lower()
                if "outputmint" in low or "outputmint" in ln:
                    output_mint = maybe
                elif "inputmint" in low:
                    input_mint = maybe
                else:
                    # fallback: if output not set, set it; else if input not set, set it
                    if not output_mint:
                        output_mint = maybe
                    elif not input_mint:
                        input_mint = maybe

    # Fallback: scan entire logs for any mint-like token and choose the one that seems newly received
    if not output_mint:
        # attempt to find the first mint-looking token in logs
        for ln in parsed_logs:
            maybe = regex_extract_mint_from_log_line(ln)
            if maybe and maybe != SOL_MINT:
                output_mint = maybe
                break

    if not output_mint:
        print("[WARN] Could not determine outputMint. Aborting copy.")
        return False

    # Very small safety filter: ignore SOL->SOL or short nonsense
    if output_mint == SOL_MINT:
        print("[INFO] Parent bought SOL (or outputMint is SOL). Skipping.")
        return False

    print(f"[INFO] Parent bought token: {output_mint}  (input: {input_mint})")

    # Optionally, you could check how much was bought by examining token balances; skip for simplicity.

    # 4) Create order for the same token (SOL -> output_mint)
    # We use COPY_AMOUNT_LAMPORTS as the input SOL amount for our buy.
    order = None
    for attempt in range(1, MAX_ORDER_RETRIES + 1):
        order = create_jupiter_order(SOL_MINT, output_mint, str(COPY_AMOUNT_LAMPORTS), wallet_pubkey_str)
        if order and isinstance(order, dict) and order.get("transaction"):
            break
        print(f"[WARN] create_jupiter_order failed, attempt {attempt}/{MAX_ORDER_RETRIES}")
        time.sleep(1)

    if not order:
        print("[ERROR] Could not get a valid Jupiter order. Skipping copy.")
        return False

    # 5) Sign & execute
    exec_res = sign_and_execute_order(order, signer)
    if not exec_res:
        print("[ERROR] Execution failed.")
        return False

    # Jupiter may return signature/txid
    sig = exec_res.get("signature") or exec_res.get("txid") or exec_res.get("result")
    print(f"[SUCCESS] Copy trade executed. Response signature/tx: {sig}")

    return True


# ------------ websocket subscriber -------------
async def logs_subscribe_loop(signer: Keypair, wallet_pubkey_str: str):
    """
    Native JSON-RPC over websocket for logsSubscribe with mentions: [PARENT_WALLET_STR]
    """
    async with websockets.connect(WS_URL, ping_interval=30) as ws:
        # 1) subscribe
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PARENT_WALLET_STR]},
                {"commitment": "confirmed"}
            ]
        }
        await ws.send(json.dumps(request))
        sub_response = await ws.recv()
        try:
            sub_obj = json.loads(sub_response)
            if "error" in sub_obj:
                print("[ERROR] subscribe error:", sub_obj["error"])
                return
            sub_id = sub_obj.get("result")
            print(f"[WS] Subscribed to logs for {PARENT_WALLET_STR} (sub id: {sub_id})")
        except Exception as e:
            print("[ERROR] invalid subscribe response:", e)
            return

        # 2) receive loop
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            # message structure for notifications:
            # { "jsonrpc":"2.0","method":"logsNotification","params":{"result":{...},"subscription":<id>} }
            if msg.get("method") != "logsNotification":
                # ignore other messages (subscriptions confirmations etc)
                continue

            params = msg.get("params", {}).get("result")
            if not params:
                continue

            # result.value has: signature, err, logs(list), maybe other fields
            value = params.get("value", {})
            signature = value.get("signature")
            logs = value.get("logs", []) or []

            if not signature:
                continue

            # Basic guard: if the logs indicate a program other than Jupiter, skip quick
            # We pass logs to the handler which will do more checks
            # call the handler but do not block websocket loop for too long
            # schedule task
            asyncio.create_task(handle_parent_tx(signature, logs, signer, wallet_pubkey_str))


# ------------ main -------------
def load_key_from_file_or_env() -> Keypair:
    """
    Load base58 64-byte private key from environment or file.
    You must set MY_PRIVATE_KEY_BASE58 env variable to continue, or modify this function.
    """
    b58 = '3yoYzTv58'
    if not b58:
        # try to load from a file path you can customize
        config_path = os.path.expanduser("~/private_key_base58.txt")
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                b58 = f.read().strip()
    if not b58:
        raise RuntimeError("Private key not provided. Set MY_PRIVATE_KEY_BASE58 env or create ~/private_key_base58.txt")
    return Keypair.from_base58_string(b58)


async def main():
    print("🔥 Copy Trading Bot (event-driven, Jupiter-aware)")
    signer = load_key_from_file_or_env()
    wallet_pubkey_str = str(signer.pubkey())
    print("Your wallet:", wallet_pubkey_str)
    print("Monitoring parent wallet:", PARENT_WALLET_STR)
    print("Using Helius RPC & WS:", RPC_URL)

    # Warm up AsyncClient
    await async_client.is_connected()

    # Start logs subscription loop (this function will spawn tasks to handle txs)
    try:
        await logs_subscribe_loop(signer, wallet_pubkey_str)
    finally:
        await async_client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting...")
