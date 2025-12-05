import asyncio
import websockets
import requests
import json

RPC_URL = "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
WS_URL = "wss://mainnet.helius-rpc.com/?api-key=988ff6ca-66d4-402c-8701-179576cf3acc"

WALLET_TO_MONITOR = "D65vrNzmxbDNL5pGJpVqxJhf3ncoLaFzf2xuWEGsnig2"

# ===========================================
# USER THRESHOLD (minimum trade amount)
# ===========================================
THRESHOLD_SOL = 0.0   # ignore trades smaller than this (in SOL units)
# example: 0.001 SOL ~ $0.20
# set 0 to receive all swaps


TOKEN_CACHE = {}

def fetch_token_map():
    """Load the giant token list once."""
    try:
        url = "https://token.jup.ag/all"
        data = requests.get(url, timeout=10).json()
        for token in data:
            TOKEN_CACHE[token["address"]] = token
    except:
        pass

fetch_token_map()


def get_token_info(mint):
    """Return token name / symbol / decimals."""
    token = TOKEN_CACHE.get(mint)
    if token:
        return (
            token.get("name", "Unknown"),
            token.get("symbol", "UNK"),
            token.get("decimals", 6)
        )
    return ("Unknown", "UNK", 6)


# ------------------------------------------------
# Fetch tx details
# ------------------------------------------------
def get_tx_details(signature):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0
            }
        ]
    }
    try:
        r = requests.post(RPC_URL, json=payload, timeout=10)
        if r.status_code != 200:
            return None
        return r.json().get("result")
    except:
        return None


# ------------------------------------------------
# Extract token mints + amount_in + amount_out
# ------------------------------------------------
def extract_swap(tx):
    if not tx:
        return None

    instructions = tx["meta"].get("innerInstructions", [])
    transfers = []

    for entry in instructions:
        for inst in entry["instructions"]:
            parsed = inst.get("parsed")
            if not parsed:
                continue
            if parsed.get("type") == "transfer":
                info = parsed["info"]
                mint = info.get("mint")
                amount_raw = int(info.get("amount", 0))
                transfers.append((mint, amount_raw))

    # To detect a swap we need at least 2 token transfers
    if len(transfers) < 2:
        return None

    mint_in, amount_in = transfers[0]
    mint_out, amount_out = transfers[-1]

    return mint_in, amount_in, mint_out, amount_out


# ------------------------------------------------
# WebSocket monitor
# ------------------------------------------------
async def monitor_wallet():
    print("\n🔍 Real-Time Trade Monitor With Threshold…\n")
    print(f"📌 Threshold: ignore trades smaller than {THRESHOLD_SOL} SOL\n")

    subscribe_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "transactionSubscribe",
        "params": [
            {
                "vote": False,
                "mentions": [WALLET_TO_MONITOR]
            },
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0
            }
        ]
    }

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps(subscribe_msg))
        print(f"📡 Subscribed to wallet: {WALLET_TO_MONITOR}\n")

        while True:
            msg = await ws.recv()
            data = json.loads(msg)

            if "params" not in data:
                continue

            signature = data["params"]["result"]["signature"]
            print(f"[EVENT] Signature: {signature}")

            tx = get_tx_details(signature)
            swap = extract_swap(tx)

            if not swap:
                print("[SKIP] Not a swap")
                continue

            mint_in, raw_in, mint_out, raw_out = swap

            # get token metadata
            name_in, sym_in, dec_in = get_token_info(mint_in)
            name_out, sym_out, dec_out = get_token_info(mint_out)

            amt_in = raw_in / (10 ** dec_in)
            amt_out = raw_out / (10 ** dec_out)

            # -----------------------------
            # APPLY THRESHOLD
            # -----------------------------
            if amt_in < THRESHOLD_SOL:
                print(f"[SKIP] Swap too small ({amt_in} < {THRESHOLD_SOL})")
                continue

            print("\n💱 TRADE DETECTED")
            print(f"   Sold:   {amt_in} {sym_in} ({name_in}) [{mint_in}]")
            print(f"   Bought: {amt_out} {sym_out} ({name_out}) [{mint_out}]")
            print("-----------------------------------------------------\n")


# ------------------------------------------------
# Run
# ------------------------------------------------
asyncio.run(monitor_wallet())
