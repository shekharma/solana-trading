# 1.  Alpha Wallet_address (known)- parent 
# 2.  Detect the token trade by parent wallet eg. Triumph coin
# 3.  Check whether blacklisted wallet address entered or not into the Triumph coin
#     a.  Get the list all wallet address triumph coin
# 4. If it’s there don’t execute trade else do copytrading
#     a.  Copytrading code



alpha_wallet_address = "parent wallet address"

def get_trade_coin(alpha_wallet_address):
    
    tokens at t = get_token_balance(alpha_wallet_address) + extract only tokens and there amount 
    diff_token =0
    while diff_token<=0:
        tokens at t +1 = get_token_balance(alpha_wallet_address) and there amount

        diff_token(dict) = [tokens at t] - [tokens at t-1] 
    token_name = diff_token['token_name']

    return token_name

def do_trade(token_name, blaclisted_wallet):
    """
    for given token check
    1st : do the wallet has the token_amout>0 use while loop here to check balance of blacklisted wallet continuously
    """
    get_token_balance= https://lite-api.jup.ag/ultra/v1/balances/{blaclisted_wallet_address}
    

    token_balance = get_token_balance(blaclisted_wallet + token_name)
    if token_balance==0:
        do_entry()  # <- decision 1
        while token_balance<=0:
            token_balance = get_token_balance( blaclisted_wallet+ token_name)
        exit_trade() # <- decision 2
    else:
        no_entry() # <- decision 3   

