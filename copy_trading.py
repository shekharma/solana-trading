import time
import base64
import requests

from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solana.rpc.api import Client

# ---------------------------
# CONFIG
# ---------------------------
RPC_URL = "https://mainnet.helius-rpc.com/?api-key=988ff6ca-66d4-402c-8701-179576cf3acc"
client = Client(RPC_URL)

JUP_ORDER_URL = "https://lite-api.jup.ag/ultra/v1/order"
JUP_EXEC_URL = "https://lite-api.jup.ag/ultra/v1/execute"

SOL_MINT = "So11111111111111111111111111111111111111112"

# load signer (base58 64-byte private key)
PRIVATE_KEY = "38LKG5Ru1SG2b4FEJk9dTChUpnBGkb8B1B9cRQ3LY2HwqS5Ny7fduiGwL9p4NsrST793PS4iMFRLbSBYStB9UhLb"
signer = Keypair.from_base58_string(PRIVATE_KEY)
print(f"Your wallet: {signer.pubkey()}")


# ---------------------------
# UTIL: validate Jupiter order (avoid short / broken tx)
# ---------------------------
def is_valid_order(order):
    if not order or not isinstance(order, dict):
        return False
    tx = order.get("transaction")
    # require transaction to exist and be reasonably long (tweak threshold as needed)
    return bool(tx) and isinstance(tx, str) and len(tx) > 500


# ---------------------------
# GET BALANCES (safe)
# ---------------------------
def get_balances(wallet):
    url = f'https://lite-api.jup.ag/ultra/v1/holdings/{wallet}'
    try:
        resp = requests.get(url, timeout=6)
    except Exception as e:
        print("❌ holdings request failed:", e)
        return {}

    if resp.status_code != 200:
        print("❌ holdings API error:", resp.status_code, resp.text[:200])
        return {}

    try:
        data = resp.json()
    except ValueError:
        print("❌ holdings JSON decode failed:", resp.text[:200])
        return {}

    balances = {}
    for mint, token_accounts in data.get("tokens", {}).items():
        try:
            balances[mint] = token_accounts[0].get("uiAmount", 0)
        except Exception:
            balances[mint] = 0
    balances[SOL_MINT] = data.get("uiAmount", 0)
    return balances


# ---------------------------
# WAIT helpers
# ---------------------------
def wait_for_confirmation(signature, timeout=30):
    """Poll get_signature_statuses until confirmed or timeout. Returns True if confirmed."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            res = client.get_signature_statuses([signature])
            # structure: res['result']['value'][0]
            val = res.get("result", {}).get("value", [None])[0]
            if val is None:
                # time.sleep(1)
                continue
            conf = val.get("confirmationStatus") or val.get("status") or val.get("confirmations")
            # confirmationStatus usually 'confirmed' or 'finalized'
            if val.get("confirmationStatus") in ("confirmed", "finalized"):
                return True
            # sometimes status field contains error info; if no error continue
        except Exception as e:
            # network / parse issue -> wait and retry
            # don't crash here
            # print("wait_for_confirmation exception:", e)
            pass
        # time.sleep(1)
    return False


def wait_until_balance_changes(wallet, mint, timeout=30):
    """Wait until the token mint appears in holdings with positive uiAmount (or until timeout)."""
    start = time.time()
    while time.time() - start < timeout:
        bal = get_balances(wallet).get(mint, 0)
        if bal and bal > 0:
            return bal
        # time.sleep(1)
    return 0


# ---------------------------
# JUP: create order (safe)
# ---------------------------
def create_order(inputMint, outputMint, amount, taker):
    params = {
        "inputMint": inputMint,
        "outputMint": outputMint,
        "amount": amount,
        "taker": taker,
        "mode": "swap"
    }
    try:
        resp = requests.get(JUP_ORDER_URL, params=params, timeout=8)
    except Exception as e:
        print("❌ create_order network error:", e)
        return {}

    if resp.status_code != 200:
        print("❌ create_order status:", resp.status_code, resp.text[:300])
        return {}

    try:
        return resp.json()
    except ValueError:
        print("❌ create_order decode failed:", resp.text[:300])
        return {}


# ---------------------------
# JUP: sign and execute (safe)
# ---------------------------
def sign_and_execute(order_res):
    if not is_valid_order(order_res):
        print("❌ Invalid/incomplete order_res:", order_res)
        return None

    unsigned_b64 = order_res["transaction"]
    request_id = order_res.get("requestId")
    if not request_id:
        print("❌ Order missing requestId:", order_res)
        return None

    try:
        raw_tx = base64.b64decode(unsigned_b64)
    except Exception as e:
        print("❌ base64 decode failed:", e)
        return None

    try:
        tx = VersionedTransaction.from_bytes(raw_tx)
    except Exception as e:
        print("❌ from_bytes() failed (short/corrupted tx):", e)
        return None

    # sign if required
    try:
        pubkeys = tx.message.account_keys
        if signer.pubkey() in pubkeys:
            signer_index = pubkeys.index(signer.pubkey())
            message_bytes = to_bytes_versioned(tx.message)
            signature = signer.sign_message(message_bytes)
            # ensure signatures array exists and assign
            sigs = list(tx.signatures)
            # Expand sigs if necessary
            if signer_index >= len(sigs):
                sigs += [b""] * (signer_index - len(sigs) + 1)
            sigs[signer_index] = signature
            tx.signatures = sigs
            print("Signed with your wallet at index", signer_index)
        else:
            print("Your wallet is NOT required to sign. Sending unchanged transaction.")
    except Exception as e:
        print("❌ signing failed:", e)
        return None

    signed_b64 = base64.b64encode(bytes(tx)).decode()

    payload = {
        "signedTransaction": signed_b64,
        "requestId": request_id
    }

    try:
        resp = requests.post(JUP_EXEC_URL, json=payload, timeout=12)
    except Exception as e:
        print("❌ execute network error:", e)
        return None

    try:
        data = resp.json()
    except ValueError:
        print("❌ execute returned non-json:", resp.text[:500])
        return None

    if "error" in data:
        print("❌ execute returned error:", data)
        return None

    return data


# ---------------------------
# BUY -> CONFIRM -> SELL cycle (guaranteed buy)
# ---------------------------
def execute_buy_sell_cycle(token_mint, amount, wallet_pubkey):
    print(f"\n🚀 Starting BUY cycle for {token_mint}")

    # ----------------------------------------------------
    # 1️⃣ GET BUY ORDER (Retry until valid)
    # ----------------------------------------------------
    order_buy = {}
    for attempt in range(8):
        order_buy = create_order(
            inputMint=SOL_MINT,
            outputMint=token_mint,
            amount=str(amount), 
            taker=wallet_pubkey
        )

        if is_valid_order(order_buy):
            break

        print(f"⚠️ Invalid BUY order (attempt {attempt+1}), retrying...")
        time.sleep(1)
    else:
        print("❌ BUY order unavailable for this token. Aborting cycle.")
        return False

    # ----------------------------------------------------
    # 2️⃣ SIGN + EXECUTE BUY
    # ----------------------------------------------------
    res_buy = sign_and_execute(order_buy)
    if not res_buy:
        print("❌ BUY execution failed. Aborting cycle.")
        return False

    print("BUY execution response:", res_buy)

    # Extract the BUY signature (Jupiter varies)
    sig = (
        res_buy.get("signature")
        or res_buy.get("txid")
        or res_buy.get("result")
    )

    # ----------------------------------------------------
    # ⭐ NEW: EXACT RAW TOKEN AMOUNT RECEIVED ⭐
    # ----------------------------------------------------
    if "outputAmountResult" in res_buy:
        raw_received_amount = int(res_buy["outputAmountResult"])
        print(f"📥 Raw amount received (for SELL): {raw_received_amount}")
    else:
        print("❌ Missing outputAmountResult in BUY response. Cannot SELL.")
        return False

    # ----------------------------------------------------
    # 3️⃣ WAIT FOR BUY CONFIRMATION
    # ----------------------------------------------------
    if isinstance(sig, str):
        print("⏳ Waiting for BUY confirmation...")
        ok = wait_for_confirmation(sig, timeout=40)
        if not ok:
            print("⚠️ Buy not confirmed in time — continuing anyway.")

    # ----------------------------------------------------
    # 4️⃣ WAIT FOR TOKEN to APPEAR IN WALLET
    # ----------------------------------------------------
    print("⏳ Waiting for token balance to appear...")
    bal = wait_until_balance_changes(wallet_pubkey, token_mint, timeout=40)

    if not bal or bal <= 0:
        print("❌ Token not received after buy. Aborting SELL.")
        return False

    print(f"📦 Token balance detected: {bal} (uiAmount)")

    # ----------------------------------------------------
    # 5️⃣ GET SELL ORDER using EXACT RAW AMOUNT
    # ----------------------------------------------------
    order_sell = {}
    for attempt in range(8):

        order_sell = create_order(
            inputMint=token_mint,
            outputMint=SOL_MINT,
            amount=str(raw_received_amount),      # ⭐ Correct amount for SELL
            taker=wallet_pubkey
        )

        if is_valid_order(order_sell):
            break

        print(f"⚠️ Invalid SELL order (attempt {attempt+1}), retrying...")
        time.sleep(1)
    else:
        print("❌ SELL order unavailable. Aborting.")
        return False

    # ----------------------------------------------------
    # 6️⃣ SIGN + EXECUTE SELL
    # ----------------------------------------------------
    res_sell = sign_and_execute(order_sell)
    if not res_sell:
        print("❌ SELL execution failed.")
        return False

    print("💰 SELL executed:", res_sell)
    return True


# ---------------------------
# PARENT WATCH + MAIN
# ---------------------------
def get_parent_trade(parent_wallet): 
    prev = get_balances(parent_wallet) 
    print("\n📡 Monitoring parent wallet...") 
    while True: 
        time.sleep(1) 
        curr = get_balances(parent_wallet) 
        all_mints = set(prev.keys()) | set(curr.keys()) 
        for mint in all_mints: 
            old = prev.get(mint, 0) 
            new = curr.get(mint, 0) 
            diff = new - old 
            if abs(diff) > 0.001: 
                print(f"\n🔥 Parent trade detected on {mint}") 
                return mint 
        prev = curr

if __name__ == "__main__":
    PARENT_WALLET = "BAr5csYtpWoNpwhUjixX7ZPHXkUciFZzjBp9uNxZXJPh"
    CYCLE_LIMIT = 1
    COPY_AMOUNT = 2000000  # 0.002 SOL lamports (for SOL input) — keep as string when calling create_order

    cycles_done = 0
    print("\n🔥 Copy Trading Bot Started 🔥")
    while cycles_done < CYCLE_LIMIT:
        token = get_parent_trade(PARENT_WALLET)
        success = execute_buy_sell_cycle(token, COPY_AMOUNT, str(signer.pubkey()))
        cycles_done += 1
        print(f"\n🔁 Completed cycle {cycles_done}/{CYCLE_LIMIT} (success={success})")

    print("\n🎉 All cycles completed. Copy trading stopped.")
