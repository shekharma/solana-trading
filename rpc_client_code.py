# from solana.rpc.api import Client

# rpc_url = "https://api.mainnet-beta.solana.com"    # Your RPC
# client = Client(rpc_url)

# response = client.get_slot()
# print('response------->',response)
# print("Current slot:", response['result'])


from solana.rpc.api import Client
from solders.pubkey import Pubkey

client = Client("https://api.mainnet-beta.solana.com")

wallet = Pubkey.from_string("C4JRuZx3nAFGqJ8ZcYuwWhRGxL4N9ZwpsErjfhGqxxAf")

balance = client.get_balance(wallet)
print("balance---->", balance)
# balance is a GetBalanceResp object
sol_balance = balance.value / 1_000_000_000

print("SOL balance:", sol_balance, "SOL")

# print("SOL balance:", balance['result']['value'] / 1e9, "SOL")

