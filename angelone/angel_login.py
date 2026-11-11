"""
Angel One SmartAPI Login Helper
Generates jwtToken and feedToken for WebSocket streaming
Run this script each morning before starting the trader.
"""

import json
import os
import pyotp
from SmartApi import SmartConnect

def login_and_save_tokens():
    """Login to Angel One and save session tokens to .angel_session.json"""
    
    # Credentials come from environment variables (ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP).
    # Falling back to config.json is deprecated: that file must stay out of git (see .gitignore / .env.example).
    env = {
        'api_key': os.getenv('ANGEL_API_KEY'),
        'client_id': os.getenv('ANGEL_CLIENT_ID'),
        'password': os.getenv('ANGEL_PASSWORD'),
        'totp_token': os.getenv('ANGEL_TOTP'),
    }
    if all(env.values()):
        api_data = env
    else:
        try:
            with open('config.json', 'r') as f:
                api_data = json.load(f)['smartapi']
            print("WARNING: using credentials from config.json; set the ANGEL_* environment variables instead.")
        except FileNotFoundError:
            print("ERROR: set ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP (see .env.example)")
            return False
        except KeyError:
            print("ERROR: Invalid config.json structure. Check 'smartapi' section.")
            return False

    api_key = api_data.get('api_key')
    client_id = api_data.get('client_id')
    password = api_data.get('password')
    totp_token = api_data.get('totp_token')

    if not all([api_key, client_id, password, totp_token]):
        print("ERROR: Missing credentials")
        print("   Required: ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP")
        return False

    try:
        obj = SmartConnect(api_key=api_key)
        totp = pyotp.TOTP(totp_token).now()

        print(f"Logging in as {client_id}...")
        data = obj.generateSession(client_id, password, totp)

        if data['status']:
            jwt_token = data['data']['jwtToken']
            refresh_token = data['data']['refreshToken']
            feed_token = obj.getfeedToken()

            # Session tokens are short-lived but still secret: keep them in a git-ignored file, never in config.json.
            with open('.angel_session.json', 'w') as f:
                json.dump({'jwtToken': jwt_token, 'refreshToken': refresh_token, 'feedToken': feed_token}, f, indent=2)

            print("Login successful!")
            print(f"   JWT Token: {jwt_token[:20]}...")
            print(f"   Feed Token: {feed_token[:20]}...")
            print("   Tokens saved to .angel_session.json (git-ignored)")
            print("   NOTE: Tokens expire at market close. Re-run tomorrow.")

            try:
                profile = obj.getProfile(refresh_token)
                if profile['status']:
                    name = profile['data']['name']
                    exchanges = profile['data']['exchanges']
                    print(f"   Account: {name}")
                    print(f"   Exchanges: {', '.join(exchanges)}")
            except:
                pass

            return True
        else:
            print(f"ERROR: Login failed: {data.get('message', 'Unknown error')}")
            return False

    except Exception as e:
        print(f"ERROR: Error during login: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("="*60)
    print("  Angel One SmartAPI Login")
    print("="*60)
    
    success = login_and_save_tokens()
    
    if success:
        print("\nReady to trade.")
    else:
        print("\nLogin failed. Check your credentials.")
        print("   1. Verify the ANGEL_* environment variables")
        print("   2. Ensure TOTP token is correct (from Angel One app)")
        print("   3. Check if account has API access enabled")
