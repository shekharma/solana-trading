import requests

RPC_URL = "https://api.mainnet-beta.solana.com"

def get_transaction(signature: str):
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
    res = requests.post(RPC_URL, json=payload).json()
    return res.get("result")


def parse_token_transfers(tx):
    if not tx:
        return []

    transfers = []
    instructions = tx["meta"]["postTokenBalances"]
    pre_balances = {x["accountIndex"]: x["uiTokenAmount"] for x in tx["meta"]["preTokenBalances"]}
    post_balances = {x["accountIndex"]: x["uiTokenAmount"] for x in tx["meta"]["postTokenBalances"]}

    for post in tx["meta"]["postTokenBalances"]:
        index = post["accountIndex"]
        mint = post["mint"]
        owner = post["owner"]

        pre_amount = float(pre_balances.get(index, {"amount": "0"})["amount"])
        post_amount = float(post["uiTokenAmount"]["amount"])
        diff = post_amount - pre_amount

        if diff != 0:
            transfers.append({
                "owner": owner,
                "mint": mint,
                "change": diff / (10 ** post["uiTokenAmount"]["decimals"]),
                "decimals": post["uiTokenAmount"]["decimals"]
            })

    return transfers


def detect_swap(signature: str):
    tx = get_transaction(signature)

    print("\n==============================")
    print(" PARSED TOKEN TRANSFERS")
    print("==============================")
    print("Signature:", signature)

    transfers = parse_token_transfers(tx)

    if not transfers:
        print("[INFO] No SPL token transfers found.")
        return None

    for t in transfers:
        direction = "RECEIVED" if t["change"] > 0 else "SENT"
        print(f"""
Token Mint:  {t['mint']}
Owner:       {t['owner']}
Amount:      {t['change']}
Direction:   {direction}
---------------------------
""")

    return transfers


# -------------------------
# RUN THE FUNCTION
# -------------------------

sig = "29AWASBxeK1KGENEbEHGJzHKGyp8BBfDPx7TfZ3GSwQHooSBat686qC4MU6VUeLhGwWoDvbHxC9rpeJQ6St9k48p"
detect_swap(sig)
