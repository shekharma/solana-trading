import asyncio
import websockets
import requests
import json
import time


# class RealTimeBalanceMonitor:

#     def __init__(self):
#         self.SOL_MINT = "So11111111111111111111111111111111111111112"
#         self.THRESHOLD = 0.001      # filter dust

#         self.prev_balances = {}

#     # ---------------------------------------------------
#     # Jupiter holdings API (Your working correct method)
#     # ---------------------------------------------------
#     def get_balances(self, wallet):
#         url = f"https://lite-api.jup.ag/ultra/v1/holdings/{wallet}"
#         try:
#             data = requests.get(url, timeout=4).json()
#         except:
#             return {}

#         balances = {}

#         # SPL tokens
#         for mint, token_accounts in data.get("tokens", {}).items():
#             if token_accounts:
#                 amt = token_accounts[0].get("uiAmount", 0)
#                 if amt > 0:
#                     balances[mint] = amt

#         # SOL balance
#         sol = data.get("uiAmount", 0)
#         if sol > 0:
#             balances[self.SOL_MINT] = sol

#         return balances

#     # ---------------------------------------------------
#     # SUBSCRIBE to ALL token accounts for the wallet
#     # ---------------------------------------------------
#     async def subscribe_all_token_accounts(self, wallet, WS_URL):

#         print(f"📡 Subscribing to ALL token accounts of wallet: {wallet}")

#         url = f"https://api.helius.xyz/v0/addresses/{wallet}?api-key={WS_URL.split('api-key=')[1]}"
#         meta = requests.get(url).json()

#         owned_accounts = meta.get("tokenAccounts", [])

#         print(f"Found {len(owned_accounts)} token accounts")

#         return owned_accounts

#     # ---------------------------------------------------
#     # Real-time account notifications
#     # ---------------------------------------------------
#     async def monitor(self, wallet, WS_URL):

#         token_accounts = await self.subscribe_all_token_accounts(wallet, WS_URL)

#         # Initial cache
#         self.prev_balances = self.get_balances(wallet)

#         async with websockets.connect(WS_URL) as ws:

#             # Subscribe to each associated token account
#             for acc in token_accounts:
#                 msg = {
#                     "jsonrpc": "2.0",
#                     "id": acc,
#                     "method": "accountSubscribe",
#                     "params": [acc, {"encoding": "jsonParsed"}]
#                 }
#                 await ws.send(json.dumps(msg))

#             print("🟢 Subscribed to all token accounts!")
#             print("Waiting for swaps...\n")

#             while True:
#                 msg = await ws.recv()

#                 # wait 0.2 seconds for Jupiter to update balances
#                 await asyncio.sleep(0.2)

#                 curr = self.get_balances(wallet)
#                 if curr:
#                     self.compare_and_print(self.prev_balances, curr)
#                     self.prev_balances = curr.copy()

#     # ---------------------------------------------------
#     # Compare changes exactly like your working code
#     # ---------------------------------------------------
#     def compare_and_print(self, prev, curr):

#         mints = set(prev.keys()) | set(curr.keys())

#         for mint in mints:
#             old = prev.get(mint, 0)
#             new = curr.get(mint, 0)

#             if abs(new - old) >= self.THRESHOLD:
#                 action = "BUY" if new > old else "SELL"

#                 print("🔥 TRADE DETECTED!")
#                 print("-------------------------")
#                 print(f"Token: {mint}")
#                 print(f"Action: {action}")
#                 print(f"Old Balance: {old}")
#                 print(f"New Balance: {new}")
#                 print(f"Diff: {round(new - old, 6)}")
#                 print("-------------------------\n")


RPC_WSS = "wss://api.mainnet-beta.solana.com"
RPC_HTTPS = "https://api.mainnet-beta.solana.com"

THRESHOLD = 0.0001   # user-defined min token change


def get_token_accounts(owner):
    """Fetch all SPL token accounts of the wallet"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            owner,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"}
        ]
    }
    resp = requests.post(RPC_HTTPS, json=payload).json()

    accounts = {}

    for acc in resp.get("result", {}).get("value", []):
        info = acc["account"]["data"]["parsed"]["info"]
        mint = info["mint"]
        amount = float(info["tokenAmount"]["uiAmount"])
        token_acc = acc["pubkey"]

        accounts[token_acc] = {
            "mint": mint,
            "amount": amount
        }

    return accounts


async def monitor_wallet(owner):
    print("🔄 Fetching token accounts...")
    token_accounts = get_token_accounts(owner)

    print(f"📡 Subscribing to {len(token_accounts)} token accounts...\n")

    async with websockets.connect(RPC_WSS) as ws:

        # Subscribe to each token account
        subs = {}
        sub_id = 1

        for token_acc in token_accounts:
            msg = {
                "jsonrpc": "2.0",
                "id": sub_id,
                "method": "accountSubscribe",
                "params": [
                    token_acc,
                    {"encoding": "jsonParsed", "commitment": "processed"}
                ]
            }
            await ws.send(json.dumps(msg))
            subs[sub_id] = token_acc
            sub_id += 1

        print("✅ Subscriptions done. Waiting for real trades...\n")

        prev = token_accounts.copy()

        # Listen forever
        while True:
            raw = await ws.recv()
            data = json.loads(raw)

            # Not an update event
            if "params" not in data:
                continue

            # Find which account changed
            sub = data["params"]["subscription"]
            token_acc = subs.get(sub, None)
            if not token_acc:
                continue

            # Parse updated balance
            acc_info = data["params"]["result"]["value"]
            new_amt = float(acc_info["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"])
            mint = prev[token_acc]["mint"]
            old_amt = prev[token_acc]["amount"]

            diff = new_amt - old_amt

            # ignore tiny noise
            if abs(diff) < THRESHOLD:
                continue

            # BUY / SELL detection
            action = "BUY" if diff > 0 else "SELL"

            print("\n🔥 REAL-TIME TRADE DETECTED")
            print("-----------------------------")
            print(f"Mint:        {mint}")
            print(f"Action:      {action}")
            print(f"Old Balance: {old_amt}")
            print(f"New Balance: {new_amt}")
            print(f"Difference:  {round(diff, 6)}")
            print("-----------------------------\n")

            # update state
            prev[token_acc]["amount"] = new_amt


# ---------------------------------------------
# RUN
# ---------------------------------------------

# WS_URL = "wss://mainnet.helius-rpc.com/?api-key=988ff79576cf3acc"
WALLET = "Eig2"

asyncio.run(monitor_wallet(WALLET))


# bot = RealTimeBalanceMonitor()
# asyncio.run(bot.monitor(WALLET, WS_URL))
