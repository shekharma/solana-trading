import time
import base64
import requests
from typing import Dict, Tuple, Optional, Set

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.api import Client

# Reuse existing config values (Helius key, parent wallet, private key)
from copy_trade_single import HELIUS_API_KEY, PARENT_WALLET, USER_PRIVATE_KEY_BASE58

# =========================
# CONFIG
# =========================

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
client = Client(RPC_URL)

# Jupiter v6 endpoints (non-Ultra)
JUP_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP_URL = "https://quote-api.jup.ag/v6/swap"

# SOL mint
SOL_MINT = "So11111111111111111111111111111111111111112"

# --- Copy-trade settings ---

# Amount you use for each copy trade (input to Jupiter), in lamports (raw SOL units)
# You requested: 2000000 (0.002 SOL)
COPY_AMOUNT_LAMPORTS = 1_000_000  # 0.002 SOL

# Parent wallet must trade with at least this much SOL or we ignore the trade (in SOL)
MIN_PARENT_TRADE_SOL = 0.1

# Ignore tiny per-token balance changes (uiAmount units)
TOKEN_CHANGE_THRESHOLD = 0.0001

# How many times to retry creating a Jupiter quote/swap
MAX_ORDER_RETRIES = 6

# Slippage for quotes (in basis points, 100 = 1%)
DEFAULT_SLIPPAGE_BPS = 500

# SSL verification for Jupiter HTTPS requests.
# Set to False temporarily if you are seeing TLS/SSL errors (e.g. SSLEOFError)
# from quote-api.jup.ag on this machine.
VERIFY_JUP_SSL = False


# =========================
# UTILITIES
# =========================

def get_balances(wallet: str) -> Dict[str, float]:
    """Get aggregated token balances (uiAmount) for a wallet from Jupiter holdings API."""
    url = f"https://lite-api.jup.ag/ultra/v1/holdings/{wallet}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"❌ get_balances({wallet}) failed: {e}")
        return {}

    balances: Dict[str, float] = {}

    # SPL tokens
    for mint, token_accounts in data.get("tokens", {}).items():
        if not token_accounts:
            continue
        try:
            ui_amt = token_accounts[0].get("uiAmount", 0.0)
        except Exception:
            ui_amt = 0.0
        if ui_amt is None:
            ui_amt = 0.0
        balances[mint] = float(ui_amt)

    # SOL
    sol_ui = data.get("uiAmount", 0.0) or 0.0
    balances[SOL_MINT] = float(sol_ui)

    return balances


# =========================
# JUPITER v6 HELPERS
# =========================

def get_jup_quote(
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> Optional[dict]:
    """Call Jupiter v6 /quote endpoint (non-Ultra)."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": slippage_bps,
        "swapMode": "ExactIn",
        "restrictIntermediateTokens": "true",
    }

    try:
        r = requests.get(JUP_QUOTE_URL, params=params, timeout=10, verify=VERIFY_JUP_SSL)
        if r.status_code != 200:
            print("❌ get_jup_quote status:", r.status_code, r.text[:300])
            return None
        data = r.json()
    except Exception as e:
        print("❌ get_jup_quote error:", e)
        return None

    if not isinstance(data, dict):
        print("❌ get_jup_quote non-dict payload:", data)
        return None

    if data.get("error"):
        print("⚠️ get_jup_quote Jupiter error:", data.get("error"))
        return None

    if not data.get("inAmount") or not data.get("outAmount"):
        print("❌ get_jup_quote missing inAmount/outAmount:", data)
        return None

    return data


def get_jup_swap_tx(quote_response: dict, user_pubkey: str) -> Optional[str]:
    """Call Jupiter v6 /swap to get a serialized (base64) transaction for the given quote."""
    body = {
        "quoteResponse": quote_response,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
    }

    try:
        r = requests.post(JUP_SWAP_URL, json=body, timeout=20, verify=VERIFY_JUP_SSL)
        if r.status_code != 200:
            print("❌ get_jup_swap_tx status:", r.status_code, r.text[:300])
            return None
        data = r.json()
    except Exception as e:
        print("❌ get_jup_swap_tx error:", e)
        return None

    if not isinstance(data, dict):
        print("❌ get_jup_swap_tx non-dict payload:", data)
        return None

    swap_tx = data.get("swapTransaction")
    if not swap_tx:
        print("❌ get_jup_swap_tx missing swapTransaction:", data)
        return None

    sim_err = data.get("simulationError")
    if sim_err:
        print("⚠️ Jupiter simulationError:", sim_err)

    return swap_tx


def sign_and_send_jup_tx(unsigned_b64: str, signer: Keypair) -> Optional[str]:
    """Decode, sign and send the Jupiter swap transaction via Helius RPC."""
    try:
        raw_tx = base64.b64decode(unsigned_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
    except Exception as e:
        print("❌ Could not decode swapTransaction:", e)
        return None

    try:
        pubkeys = tx.message.account_keys
        signer_pub = signer.pubkey()
        if signer_pub in pubkeys:
            signer_index = pubkeys.index(signer_pub)
            msg_bytes = to_bytes_versioned(tx.message)
            signature = signer.sign_message(msg_bytes)
            sigs = list(tx.signatures)
            if signer_index >= len(sigs):
                sigs += [b""] * (signer_index - len(sigs) + 1)
            sigs[signer_index] = signature
            tx.signatures = sigs
    except Exception as e:
        print("❌ signing failed:", e)
        return None

    signed_bytes = bytes(tx)

    try:
        res = client.send_raw_transaction(signed_bytes)
    except Exception as e:
        print("❌ send_raw_transaction error:", e)
        return None

    sig = None
    if isinstance(res, dict):
        sig = res.get("result") or res.get("value")
    else:
        sig = str(res)

    if not sig:
        print("❌ No signature returned from RPC:", res)
        return None

    print("✅ Sent swap transaction. Signature:", sig)
    return str(sig)


# =========================
# COPY-TRADE ACTIONS
# =========================

def copy_buy_token(
    token_mint: str,
    copy_amount_lamports: int,
    signer: Keypair,
) -> Tuple[bool, Optional[int]]:
    """Execute a BUY (SOL -> token_mint) from the user wallet using Jupiter v6."""
    wallet_pubkey = str(signer.pubkey())
    print(f"\n🚀 Copy BUY: {token_mint} using {copy_amount_lamports} lamports (~{copy_amount_lamports/1e9:.4f} SOL)")

    quote: Optional[dict] = None
    for attempt in range(1, MAX_ORDER_RETRIES + 1):
        print(f"   Requesting BUY quote (attempt {attempt}/{MAX_ORDER_RETRIES})...")
        quote = get_jup_quote(SOL_MINT, token_mint, copy_amount_lamports)
        if quote:
            break
        time.sleep(1)

    if not quote:
        print("❌ Could not obtain BUY quote from Jupiter.")
        return False, None

    try:
        raw_out_int = int(quote["outAmount"])
    except Exception:
        print("⚠️ quote.outAmount not integer; value:", quote.get("outAmount"))
        raw_out_int = None

    swap_tx_b64 = get_jup_swap_tx(quote, wallet_pubkey)
    if not swap_tx_b64:
        print("❌ Could not get swap transaction from Jupiter.")
        return False, raw_out_int

    sig = sign_and_send_jup_tx(swap_tx_b64, signer)
    if not sig:
        print("❌ BUY transaction failed to send.")
        return False, raw_out_int

    print("✅ BUY sent. Signature:", sig)
    if raw_out_int is not None:
        print(f"📥 Estimated raw amount received (from quote): {raw_out_int}")
    return True, raw_out_int


def copy_sell_all(
    token_mint: str,
    raw_amount: int,
    signer: Keypair,
) -> bool:
    """Execute a SELL ALL (token_mint -> SOL) for the raw_amount we previously bought."""
    wallet_pubkey = str(signer.pubkey())
    print(f"\n💰 Copy SELL ALL: {token_mint}, raw_amount={raw_amount}")

    quote: Optional[dict] = None
    for attempt in range(1, MAX_ORDER_RETRIES + 1):
        print(f"   Requesting SELL quote (attempt {attempt}/{MAX_ORDER_RETRIES})...")
        quote = get_jup_quote(token_mint, SOL_MINT, raw_amount)
        if quote:
            break
        time.sleep(1)

    if not quote:
        print("❌ Could not obtain SELL quote from Jupiter.")
        return False

    swap_tx_b64 = get_jup_swap_tx(quote, wallet_pubkey)
    if not swap_tx_b64:
        print("❌ Could not get SELL swap transaction from Jupiter.")
        return False

    sig = sign_and_send_jup_tx(swap_tx_b64, signer)
    if not sig:
        print("❌ SELL transaction failed to send.")
        return False

    print("✅ SELL sent. Signature:", sig)
    return True


# =========================
# PARENT MONITOR + LOGIC
# =========================

def detect_parent_trade(
    prev: Dict[str, float],
    curr: Dict[str, float],
) -> Tuple[Optional[str], Optional[str], float]:
    """Compare two balance snapshots and return (token_mint, position, sol_diff)."""
    all_mints = set(prev.keys()) | set(curr.keys())
    changes = []
    for mint in all_mints:
        old = prev.get(mint, 0.0)
        new = curr.get(mint, 0.0)
        diff = new - old
        if abs(diff) >= TOKEN_CHANGE_THRESHOLD:
            changes.append((mint, old, new, diff))

    if not changes:
        return None, None, 0.0

    prev_sol = prev.get(SOL_MINT, 0.0)
    curr_sol = curr.get(SOL_MINT, 0.0)
    sol_diff = curr_sol - prev_sol

    if abs(sol_diff) < MIN_PARENT_TRADE_SOL:
        return None, None, sol_diff

    if sol_diff < 0:
        for mint, old, new, diff in changes:
            if mint == SOL_MINT:
                continue
            if diff > 0:
                return mint, "BUY", sol_diff

    if sol_diff > 0:
        for mint, old, new, diff in changes:
            if mint == SOL_MINT:
                continue
            if diff < 0:
                return mint, "SELL", sol_diff

    return None, None, sol_diff


def monitor_and_copy(parent_wallet: str, signer: Keypair) -> None:
    """Main monitoring + copy-trading loop."""
    print("\n🔥 Copy Trading Bot (Jupiter v6 non-Ultra) Started")
    print("Parent wallet:", parent_wallet)
    print("User wallet:", signer.pubkey())
    print(f"Min parent trade size: {MIN_PARENT_TRADE_SOL} SOL")
    print(f"Copy amount per trade: {COPY_AMOUNT_LAMPORTS / 1e9:.4f} SOL\n")

    ever_copied: Set[str] = set()
    open_positions: Dict[str, int] = {}

    prev_balances = get_balances(parent_wallet)
    if not prev_balances:
        print("⚠️ Initial get_balances failed. Waiting for next successful fetch...")

    while True:
        time.sleep(1)
        curr_balances = get_balances(parent_wallet)
        if not curr_balances:
            print("⚠️ Could not fetch parent balances, retrying...")
            continue

        token_mint, position, sol_diff = detect_parent_trade(prev_balances, curr_balances)

        if token_mint is None or position is None:
            prev_balances = curr_balances
            continue

        print("\n🔥 Parent trade detected!")
        print(f"   Token:     {token_mint}")
        print(f"   Position:  {position}")
        print(f"   SOL Δ:     {sol_diff:.6f} SOL")

        if position == "BUY":
            if token_mint in ever_copied:
                print("   ⛔ Already copied this token before. Skipping BUY.")
            else:
                success, raw_out = copy_buy_token(token_mint, COPY_AMOUNT_LAMPORTS, signer)
                if success:
                    ever_copied.add(token_mint)
                    if raw_out is not None:
                        open_positions[token_mint] = raw_out
                        print(f"   ✅ Open position recorded for {token_mint}, raw_amount≈{raw_out}")
                    else:
                        print("   ⚠️ Could not record raw amount; SELL ALL later will be approximate.")
                else:
                    print("   ❌ Copy BUY failed; will allow future attempts for this token.")

        elif position == "SELL":
            if token_mint not in open_positions:
                print("   ℹ️ We do not hold this token (or already sold). Nothing to do.")
            else:
                raw_amount = open_positions[token_mint]
                print(f"   ⏱ Executing SELL ALL for our position in {token_mint}...")
                success = copy_sell_all(token_mint, raw_amount, signer)
                if success:
                    open_positions.pop(token_mint, None)
                    print("   ✅ SELL ALL succeeded.")
                else:
                    print("   ❌ SELL ALL failed. Keeping position to retry on next parent SELL.")

        prev_balances = curr_balances


# =========================
# MAIN
# =========================

if __name__ == "__main__":
    if not HELIUS_API_KEY:
        raise SystemExit("❌ HELIUS_API_KEY is not set.")

    if not USER_PRIVATE_KEY_BASE58:
        raise SystemExit("❌ USER_PRIVATE_KEY_BASE58 is not set.")

    if not PARENT_WALLET:
        raise SystemExit("❌ PARENT_WALLET is not set.")

    signer = Keypair.from_base58_string(USER_PRIVATE_KEY_BASE58)
    print("User wallet pubkey:", signer.pubkey())
    print(f"Jupiter SSL verification enabled: {VERIFY_JUP_SSL}")

    monitor_and_copy(PARENT_WALLET, signer)
