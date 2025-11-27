import time
import requests

class MONITOR_WALLET():

    def __init__(self):
        self.SOL_MINT = "So11111111111111111111111111111111111111112"

    def get_balances(self, wallet_address):
        url = f'https://lite-api.jup.ag/ultra/v1/holdings/{wallet_address}'
        data = requests.get(url).json()

        balances = {}

        # SPL tokens
        for mint, token_accounts in data.get("tokens", {}).items():
            info = token_accounts[0]
            balances[mint] = info.get("uiAmount", 0)

        # SOL
        balances[self.SOL_MINT] = data.get("uiAmount", 0)

        return balances


    def monitor_wallet(self, wallet_address):
        print("📡 Starting continuous wallet monitor...\n")

        prev = self.get_balances(wallet_address)

        while True:
            time.sleep(1)

            curr = self.get_balances(wallet_address)

            all_mints = set(prev.keys()) | set(curr.keys())

            for mint in all_mints:
                old = prev.get(mint, 0)
                new = curr.get(mint, 0)

                diff = new - old

                # Ignore tiny fluctuations
                if abs(diff) > 0.0001:

                    position = "BUY" if diff > 0 else "SELL"

                    print("\n🔥 TRADE DETECTED!")
                    print("------------------------------")
                    print(f"Token       : {mint}")
                    print(f"Position    : {position}")
                    print(f"Old Balance : {old}")
                    print(f"New Balance : {new}")
                    print(f"Difference  : {round(diff, 6)}")
                    print("------------------------------")

                    # show other token balances
                    print("\n📌 Full Wallet After Trade:")
                    for other_mint, amt in curr.items():
                        print(f"{other_mint}: {amt}")

                    print("\n⏳ Monitoring for next trade...\n")

                    # IMPORTANT: update baseline so same trade isn't detected repeatedly
                    prev = curr.copy()
                    break  # break inner loop, continue monitoring

            # update prev ONLY if no trade happened
            else:
                prev = curr
mw= MONITOR_WALLET() 
mw.monitor_wallet("BTf4A2exGK9BCVDNzy65b9dUzXgMqB4weVkvTMFQsadd")