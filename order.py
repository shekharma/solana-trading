import base64
import base58
import requests
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solana.rpc.api import Client
from solders.message import to_bytes_versioned

RPC_URL = "https://mainnet.helius-rpc.com/?api-key=988ff6ca-66d4-402c-8701-179576cf3acc"
client = Client(RPC_URL)
print(client.get_latest_blockhash())



# Load your private key (64-byte array)
PRIVATE_KEY = "38LKG5Ru1SG2b4FEJk9dTChUpnBGkb8B1B9cRQ3LY2HwqS5Ny7fduiGwL9p4NsrST793PS4iMFRLbSBYStB9UhLb"  
signer = Keypair.from_base58_string(PRIVATE_KEY)
# print(client.get_token_accounts_by_owner(
#     signer.pubkey(),
#     {"mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}
# ))

print(f"Created Keypair with public key: {signer.pubkey()}")


def get_order(inputMint, outputMint, amount, taker):
    params = {
        "inputMint": inputMint,
        "outputMint": outputMint,
        "amount": amount,
        "taker": taker,
        "mode": "swap"
    }

    url = "https://lite-api.jup.ag/ultra/v1/order"
    return requests.get(url, params=params).json()


def sign_and_execute(order_res):
    unsigned_b64 = order_res["transaction"]
    request_id = order_res["requestId"]

    raw_tx = base64.b64decode(unsigned_b64)
    tx = VersionedTransaction.from_bytes(raw_tx)

    # Check if your signer is actually required
    pubkeys = tx.message.account_keys

    if signer.pubkey() in pubkeys:
        # Your wallet must sign
        signer_index = pubkeys.index(signer.pubkey())
        message_bytes = to_bytes_versioned(tx.message)
        signature = signer.sign_message(message_bytes)
        sigs = tx.signatures
        sigs[signer_index] = signature
        tx.signatures = sigs
        print("Signed with your wallet at index", signer_index)
    else:
        print("Your wallet is NOT required to sign. Sending unchanged transaction.")

    signed_b64 = base64.b64encode(bytes(tx)).decode()

    exec_url = "https://lite-api.jup.ag/ultra/v1/execute"
    payload = {
        "signedTransaction": signed_b64,
        "requestId": request_id
    }

    return requests.post(exec_url, json=payload).json()



# --------------------------
# TEST TRADE
# --------------------------
order = get_order(
    outputMint="So11111111111111111111111111111111111111112",
    inputMint="9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
    amount="2000000",
    taker="JDjKBPrFs9UTZBecYx2PpHNmn1a2vpjVcwSX2dMCCLKZ"
)

# outputMint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
result = sign_and_execute(order)

# print("ORDER:", order)
print("EXECUTION:", result)
