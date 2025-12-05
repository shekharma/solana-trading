import time
import requests

class MONITOR_WALLET():

    def __init__(self):
        self.SOL_MINT = "So11111111111111111111111111111111111111112"
        self.CHANGE_THRESHOLD = 0.01

    def get_balances(self, wallet_address):
        url = f"https://lite-api.jup.ag/ultra/v1/holdings/{wallet_address}"
        data = requests.get(url).json()

        balances = {}

        # SPL tokens
        for mint, token_accounts in data.get("tokens", {}).items():
            if token_accounts:
                amt = token_accounts[0].get("uiAmount", 0)
                if amt > 0:
                    balances[mint] = amt

        # SOL balance
        sol_amt = data.get("uiAmount", 0)
        if sol_amt > 0:
            balances[self.SOL_MINT] = sol_amt

        return balances


    def monitor_wallet(self, wallet_address):
        print("📡 Monitoring wallet for real trades...\n")

        prev = self.get_balances(wallet_address)

        while True:
            time.sleep(1)

            curr = self.get_balances(wallet_address)

            # Detect real changes
            changes = []
            all_mints = set(prev.keys()) | set(curr.keys())

            for mint in all_mints:
                old = prev.get(mint, 0)
                new = curr.get(mint, 0)
                diff = new - old

                if abs(diff) > self.CHANGE_THRESHOLD:
                    changes.append((mint, old, new, diff))

            # No changes? Continue.
            if not changes:
                prev = curr
                continue

            # 🔥 TRADE DETECTED!
            print("\n🔥 TRADE DETECTED!")
            print("----------------------------")

            for mint, old, new, diff in changes:
                action = "BUY" if diff > 0 else "SELL"
                print(f"Token:       {mint}")
                print(f"Action:      {action}")
                print(f"Old Balance: {old}")
                print(f"New Balance: {new}")
                print(f"Difference:  {round(diff, 6)}")
                print("----------------------------")

            # Print only tokens involved in the trade
            print("\n📌 Tokens affected in this trade:")
            for mint, _, new, diff in changes:
                print(f"{mint}: {new} (diff {round(diff, 6)})")

            print("\n⏳ Waiting for next trade...\n")

            # update baseline
            prev = curr.copy()

mw= MONITOR_WALLET() 
mw.monitor_wallet("69EAXsf3GLrYvz")