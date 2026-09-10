import os
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

bearer = HTTPBearer()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

def admin_required(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    if not creds or creds.credentials != ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
