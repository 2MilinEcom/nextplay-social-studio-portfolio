"""Owner password and expiring first-login invitations."""
import hashlib,hmac,secrets,time

def password_record(password):
    if not 16<=len(password)<=256:raise ValueError('Choose a password with 16 to 256 characters.')
    salt=secrets.token_hex(16)
    return {'salt':salt,'hash':hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()}

def matches(password,record,bootstrap):
    if record:
        if len(password)>256:return False
        actual=hashlib.scrypt(password.encode(),salt=bytes.fromhex(record['salt']),n=16384,r=8,p=1).hex()
        return hmac.compare_digest(actual,record['hash'])
    return bool(bootstrap) and hmac.compare_digest(password.encode(),bootstrap.encode())

def invite(secret,expires=None):
    payload=str(int(expires or time.time()+900))+'.'+secrets.token_hex(16)
    return payload+'.'+hmac.new(secret.encode(),payload.encode(),hashlib.sha256).hexdigest()

def valid_invite(token,secret):
    try:
        expiry,nonce,signature=token.split('.')
        return bool(secret) and time.time()<int(expiry)<=time.time()+900 and len(nonce)==32 and hmac.compare_digest(signature,hmac.new(secret.encode(),(expiry+'.'+nonce).encode(),hashlib.sha256).hexdigest())
    except (ValueError,TypeError):return False
