import time
import base64
import requests
from typing import Dict, Tuple, Optional, Set

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.api import Client

# =========================
# CONFIG
# =========================

# Helius RPC (fill your API key)
HELIUS_API_KEY = "988ff6ca-66d4-402c-8701-179576cf3acc"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
client = Client(RPC_URL)

# Jupiter endpoints
JUP_ORDER_URL = "https://lite-api.jup.ag/ultra/v1/order"
JUP_EXEC_URL = "https://lite-api.jup.ag/ultra/v1/execute"

# SOL mint
SOL_MINT = "So11111111111111111111111111111111111111112"

# --- Wallets ---

# Parent (alpha) wallet you are copying  <-- YOU FILL THIS
PARENT_WALLET = "beatXW1PmeVVXxebbyLuc3uKy2Vj8mt6vedBhP9AYXo"

# Your private key in base58 (64-byte secret key)  <-- YOU FILL THIS
USER_PRIVATE_KEY_BASE58 = "3tPjyoYzTv58"

# --- Copy-trade settings ---

# Amount you use for each copy trade (input to Jupiter), in raw units (lamports for SOL)
# You requested: 2000000
COPY_AMOUNT_LAMPORTS = 5_000_000  # 0.002 SOL

# Parent wallet must trade with at least this much SOL or we ignore the trade (in SOL)
MIN_PARENT_TRADE_SOL = 0.1

# Ignore tiny per-token balance changes (uiAmount units)
TOKEN_CHANGE_THRESHOLD = 0.0001

# How many times to retry creating a Jupiter order
MAX_ORDER_RETRIES = 6


# =========================
# UTILS
# =========================

def get_balances(wallet: str) -> Dict[str, float]:
    """
    Get aggregated token balances (uiAmount) for a wallet from Jupiter holdings API.
    Keys are mint addresses, values are uiAmount (float).
    """
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


def create_jupiter_order(input_mint: str, output_mint: str, amount_raw: int, taker: str) -> Optional[dict]:
    """Call Jupiter /order endpoint for Ultra API.

    Notes:
    - `amount_raw` is integer raw units (lamports for SOL / token atoms for SPL).
    - For very small trades, Ultra may return an error like "Minimum $10 for gasless"
      and the `transaction` field will be empty. In that case we return None so the
      caller can decide what to do.
    """
    params = {
    "inputMint": input_mint,
    "outputMint": output_mint,
    "amount": str(amount_raw),
    "taker": taker,
    "mode": "swap",
    "slippageBps": 500,   # 🟢 NEW: slippage support!
}
    try:
        r = requests.get(JUP_ORDER_URL, params=params, timeout=10)
        if r.status_code != 200:
            print("❌ create_jupiter_order status:", r.status_code, r.text[:300])
            return None
        data = r.json()

        # Ultra sometimes returns a quote object with an error but no transaction
        # (e.g. small amounts: "Minimum $10 for gasless"). Treat that as "no order".
        if not isinstance(data, dict):
            print("❌ create_jupiter_order non-dict payload:", data)
            return None

        if data.get("error") or data.get("errorCode"):
            print("⚠️ create_jupiter_order error from Jupiter:", data.get("error"), data.get("errorMessage"))
            return None

        if not data.get("transaction") or not data.get("requestId"):
            print("❌ create_jupiter_order returned payload without transaction:", data)
            return None

        return data
    except Exception as e:
        print("❌ create_jupiter_order error:", e)
        return None


def sign_and_execute(order_res: dict, signer: Keypair) -> Optional[dict]:
    """Sign the Jupiter transaction with the given signer and send it to /execute.

    Returns the execute response dict **only if** Jupiter reports success.
    If the response has `status != "Success"` or an `error` field, we treat it
    as a failure and return None so the caller can retry or handle it.
    """
    if not order_res or not isinstance(order_res, dict):
        print("❌ sign_and_execute: order_res is invalid")
        return None

    unsigned_b64 = order_res.get("transaction")
    request_id = order_res.get("requestId")
    if not unsigned_b64 or not request_id:
        print("❌ sign_and_execute: missing transaction or requestId")
        return None

    try:
        raw_tx = base64.b64decode(unsigned_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
    except Exception as e:
        print("❌ Could not decode transaction:", e)
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
        else:
            # No taker signature required
            pass
    except Exception as e:
        print("❌ signing failed:", e)
        return None

    signed_b64 = base64.b64encode(bytes(tx)).decode()
    payload = {"signedTransaction": signed_b64, "requestId": request_id}

    try:
        r = requests.post(JUP_EXEC_URL, json=payload, timeout=15)
        if r.status_code != 200:
            print("❌ execute status:", r.status_code, r.text[:300])
            return None
        data = r.json()
    except Exception as e:
        print("❌ execute network error:", e)
        return None

    # If Jupiter explicitly says Failed or returns an error, surface that and
    # let the caller handle it as a failure.
    status = data.get("status")
    err = data.get("error")
    if status and status != "Success":
        print(f"❌ execute returned non-success status: {status} — error: {err}")
        return None
    if err:
        print("❌ execute returned error:", err)
        return None

    # Optional: if Jupiter returns signature, we could confirm via Helius
    sig = data.get("signature") or data.get("txid") or data.get("result")
    if isinstance(sig, str):
        try:
            client.get_signature_statuses([sig])
        except Exception:
            pass

    return data


# =========================
# COPY-TRADE ACTIONS
# =========================

def copy_buy_token(
    token_mint: str,
    copy_amount_lamports: int,
    signer: Keypair,
) -> Tuple[bool, Optional[int]]:
    """
    Execute a BUY (SOL -> token_mint) from the user wallet.

    Returns (success, raw_received_amount) where raw_received_amount is the integer
    output amount from Jupiter's response, suitable for SELL later.
    """
    wallet_pubkey = str(signer.pubkey())
    print(f"\n🚀 Copy BUY: {token_mint} using {copy_amount_lamports} lamports (~{copy_amount_lamports/1e9:.4f} SOL)")

    order: Optional[dict] = None
    for attempt in range(1, MAX_ORDER_RETRIES + 1):
        print(f"   Requesting BUY order (attempt {attempt}/{MAX_ORDER_RETRIES})...")
        order = create_jupiter_order(SOL_MINT, token_mint, copy_amount_lamports, wallet_pubkey)
        if order:
            break
        time.sleep(1)

    if not order:
        print("❌ Could not obtain BUY order from Jupiter.")
        return False, None

    res = sign_and_execute(order, signer)
    if not res:
        print("❌ BUY execution failed.")
        return False, None

    print("✅ BUY execute response:", res)

    raw_out = res.get("outputAmountResult")
    if raw_out is None:
        print("⚠️ Jupiter response missing outputAmountResult; cannot know exact amount to SELL later.")
        return True, None

    try:
        raw_out_int = int(raw_out)
    except Exception:
        print("⚠️ outputAmountResult not integer; value:", raw_out)
        return True, None

    print(f"📥 Bought raw amount: {raw_out_int}")
    return True, raw_out_int


def copy_sell_all(
    token_mint: str,
    raw_amount: int,
    signer: Keypair,
) -> bool:
    """
    Execute a SELL ALL (token_mint -> SOL) for the raw_amount we previously bought.
    """
    wallet_pubkey = str(signer.pubkey())
    print(f"\n💰 Copy SELL ALL: {token_mint}, raw_amount={raw_amount}")

    order: Optional[dict] = None
    for attempt in range(1, MAX_ORDER_RETRIES + 1):
        print(f"   Requesting SELL order (attempt {attempt}/{MAX_ORDER_RETRIES})...")
        order = create_jupiter_order(token_mint, SOL_MINT, raw_amount, wallet_pubkey)
        if order:
            break
        time.sleep(1)

    if not order:
        print("❌ Could not obtain SELL order from Jupiter.")
        return False

    res = sign_and_execute(order, signer)
    if not res:
        print("❌ SELL execution failed.")
        return False

    print("✅ SELL execute response:", res)
    return True


# =========================
# PARENT MONITOR + LOGIC
# =========================

def detect_parent_trade(
    prev: Dict[str, float],
    curr: Dict[str, float],
) -> Tuple[Optional[str], Optional[str], float]:
    """
    Compare two balance snapshots and return:
      (token_mint, position, sol_diff)

    position: "BUY" or "SELL" from parent perspective.
    sol_diff: change in SOL balance (curr_sol - prev_sol) in SOL units.

    Gates:
    - |sol_diff| must be >= MIN_PARENT_TRADE_SOL (0.5 SOL) to count as a trade.
    - BUY: SOL decreases, some token's balance increases.
    - SELL: SOL increases, some token's balance decreases.
    """
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
    sol_diff = curr_sol - prev_sol  # + = more SOL, - = spent SOL

    # Gate 1: minimum trade size in SOL
    if abs(sol_diff) < MIN_PARENT_TRADE_SOL:
        return None, None, sol_diff

    # Parent BUY: SOL decreases, some token(s) increase
    if sol_diff < 0:
        for mint, old, new, diff in changes:
            if mint == SOL_MINT:
                continue
            if diff > 0:
                return mint, "BUY", sol_diff

    # Parent SELL: SOL increases, some token(s) decrease
    if sol_diff > 0:
        for mint, old, new, diff in changes:
            if mint == SOL_MINT:
                continue
            if diff < 0:
                return mint, "SELL", sol_diff

    return None, None, sol_diff


def monitor_and_copy(parent_wallet: str, signer: Keypair) -> None:
    """
    Main logic:

    - Monitor parent wallet balances via Jupiter holdings.
    - When parent does a BUY >= 0.5 SOL:
        - If we never copied that token before, execute one BUY from our wallet.
    - When parent does a SELL >= 0.5 SOL:
        - If we have an open position for that token, SELL ALL once.

    Criteria/Gates satisfied:
    - Parent wallet trades only if |SOL change| >= 0.5 SOL.
    - Buy trade is not executed twice for the same token.
    - First SELL from parent → execute SELL ALL on user wallet.
    - Next BUY only when parent buys a new token (new mint).
    """
    print("\n🔥 Copy Trading Bot Started")
    print("Parent wallet:", parent_wallet)
    print("User wallet:", signer.pubkey())
    print(f"Min parent trade size: {MIN_PARENT_TRADE_SOL} SOL")
    print(f"Copy amount per trade: {COPY_AMOUNT_LAMPORTS / 1e9:.4f} SOL\n")

    # Tokens we have ever copied (to avoid double-buy)
    ever_copied: Set[str] = set()
    # Open positions: mint -> raw_amount bought (for SELL ALL later)
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

        # --- Parent BUY -> our BUY (only if new token) ---
        if position == "BUY":
            if token_mint in ever_copied:
                print("   ⛔ Already copied this token before. Skipping BUY.")
            else:
                success, raw_out = copy_buy_token(token_mint, COPY_AMOUNT_LAMPORTS, signer)
                if success:
                    # Mark as copied so we never BUY this token again
                    ever_copied.add(token_mint)
                    if raw_out is not None:
                        open_positions[token_mint] = raw_out
                        print(f"   ✅ Open position recorded for {token_mint}, raw_amount={raw_out}")
                    else:
                        print("   ⚠️ Could not record raw amount; SELL ALL later will be approximate.")
                else:
                    print("   ❌ Copy BUY failed; will allow future attempts for this token.")

        # --- Parent SELL -> our SELL ALL (once) ---
        elif position == "SELL":
            if token_mint not in open_positions:
                print("   ℹ️ We do not hold this token (or already sold). Nothing to do.")
            else:
                raw_amount = open_positions[token_mint]
                print(f"   ⏱ Executing SELL ALL for our position in {token_mint}...")
                success = copy_sell_all(token_mint, raw_amount, signer)
                if success:
                    # Only remove from open_positions if the SELL actually succeeded.
                    open_positions.pop(token_mint, None)
                    print("   ✅ SELL ALL succeeded.")
                else:
                    print("   ❌ SELL ALL failed. Keeping position to retry on next parent SELL.")

        prev_balances = curr_balances


# =========================
# MAIN
# =========================

if __name__ == "__main__":
    # if not HELIUS_API_KEY or HELIUS_API_KEY == "988ff6ca-66d4-402c-8701-179576cf3acc":
    #     raise SystemExit("❌ Please set HELIUS_API_KEY.")

    # if not USER_PRIVATE_KEY_BASE58 or USER_PRIVATE_KEY_BASE58 == "3J1odb8x89Q6t774D8sY9iPjDo44z4khCbenVKbaa5vPrQF2ikNhYcQ2N83mMeejM31r8TSrjs1eatPjyoYzTv58":
    #     raise SystemExit("❌ Please set USER_PRIVATE_KEY_BASE58 to your base58 private key.")

    # if not PARENT_WALLET or PARENT_WALLET == "beatXW1PmeVVXxebbyLuc3uKy2Vj8mt6vedBhP9AYXo":
    #     raise SystemExit("❌ Please set PARENT_WALLET to the parent wallet address you want to copy.")

    signer = Keypair.from_base58_string("3J1YzTv58")
    print("User wallet pubkey:", signer.pubkey())

    monitor_and_copy(PARENT_WALLET, signer)