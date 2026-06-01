from kiteconnect import KiteConnect

api_key = "mo4h7vjpkhbi7693"
api_secret = "ubd6pb74djr8glayw22fsiuo6ig7hvc7"
request_token = "fUg95fAYgDyhy3d5vPQdO118fwqKNcAd"

kite = KiteConnect(api_key=api_key)
data = kite.generate_session(request_token, api_secret=api_secret)
print("Your access_token:", data['access_token'])  # Use this in your bot for today
