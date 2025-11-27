import requests
import time

class TRADE_COIN():

    def __init__(self):
        pass

    """
    Get the wallet balances
    """
    def get_balances(self, wallet_address):
        url = f'https://lite-api.jup.ag/ultra/v1/holdings/{wallet_address}'
        data = requests.get(url).json()

        balances = {}

        # SPL Tokens
        tokens = data.get("tokens", {})
        if tokens:
            for mint, token_accounts in tokens.items():
                token_info = token_accounts[0]
                balances[mint] = token_info.get("uiAmount", 0)

        # SOL
        sol_balance = data.get("uiAmount", 0)
        if sol_balance > 0:
            balances["So11111111111111111111111111111111111111112"] = sol_balance

        return balances


    def get_trade_coin(self, alpha_wallet):
        print("Monitoring wallet for token changes...")

        # initial snapshot
        prev_balances = self.get_balances(alpha_wallet)

        while True:
            time.sleep(1)

            curr_balances = self.get_balances(alpha_wallet)

            # compare both snapshots
            for mint in set(prev_balances.keys()) | set(curr_balances.keys()):
                old = prev_balances.get(mint, 0)
                new = curr_balances.get(mint, 0)

                difference = new - old

                # only return if difference > 0.5
                if abs(difference) > 0.5:

                    position = "BUY" if difference > 0 else "SELL"

                    return {
                        "token_address": mint,
                        "old_amount": old,
                        "new_amount": new,
                        "difference": round(difference, 6),
                        "position": position
                    }

            prev_balances = curr_balances


tc = TRADE_COIN()
result = tc.get_trade_coin("BTf4A2exGK9BCVDNzy65b9dUzXgMqB4weVkvTMFQsadd")
print(result)
