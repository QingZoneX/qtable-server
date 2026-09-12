#!/usr/bin/env python3
"""
Register browser extension as an OAuth client in QTable

This script registers the qtable Clipper browser extension as an OAuth 2.0 client.
The redirect URI pattern supports Chrome extension IDs dynamically.

Usage:
    python scripts/register_browser_extension_client.py
"""
import asyncio
import sys
import os

# Add the project root to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.oauth import OAuthClient


async def register_browser_extension_client():
    """Register the browser extension as an OAuth client"""
    
    # Browser extension client configuration
    CLIENT_ID = "note-script-clipper"
    CLIENT_NAME = "qtable Clipper (Browser Extension)"
    
    # Your specific extension ID
    YOUR_EXTENSION_ID = "fickmmbgnlpogeellfbmgfcgiijfkhaf"
    
    # Redirect URIs: include both wildcard pattern and specific extension ID
    # Wildcard supports any Chrome extension, specific URI is more restrictive
    REDIRECT_URIS = f"https://*.chromiumapp.org/,https://{YOUR_EXTENSION_ID}.chromiumapp.org/"
    SCOPE = "read write"
    
    async with AsyncSessionLocal() as db:
        # Check if client already exists
        result = await db.execute(
            select(OAuthClient).where(OAuthClient.client_id == CLIENT_ID)
        )
        existing_client = result.scalars().first()
        
        if existing_client:
            print("✅ Browser extension client already registered:")
            print(f"   Client ID: {existing_client.client_id}")
            print(f"   Client Name: {existing_client.client_name}")
            print(f"   Redirect URIs: {existing_client.redirect_uris}")
            print(f"   Scope: {existing_client.scope}")
            print(f"   Active: {existing_client.is_active}")
            
            # Update redirect URIs if needed
            if existing_client.redirect_uris != REDIRECT_URIS:
                print("\n🔄 Updating redirect URIs...")
                existing_client.redirect_uris = REDIRECT_URIS
                await db.commit()
                print("✅ Redirect URIs updated")
            return
        
        # Create new client
        extension_client = OAuthClient(
            client_id=CLIENT_ID,
            client_name=CLIENT_NAME,
            redirect_uris=REDIRECT_URIS,
            scope=SCOPE,
            is_active=True,
        )
        
        db.add(extension_client)
        await db.commit()
        await db.refresh(extension_client)
        
        print("✅ Successfully registered browser extension OAuth client:")
        print(f"   Client ID: {extension_client.client_id}")
        print(f"   Client Name: {extension_client.client_name}")
        print(f"   Redirect URIs: {extension_client.redirect_uris}")
        print(f"   Scope: {extension_client.scope}")
        print("\n📝 Configuration for browser extension:")
        print(f"   - Use client_id: '{CLIENT_ID}'")
        print(f"   - Authorization endpoint: http://localhost:8000/oauth/authorize")
        print(f"   - Token endpoint: http://localhost:8000/oauth/token")
        print(f"   - UserInfo endpoint: http://localhost:8000/user/me")
        print("\n⚠️  IMPORTANT:")
        print("   The redirect_uri in your extension MUST be:")
        print(f"   https://{YOUR_EXTENSION_ID}.chromiumapp.org/")
        print("\n✅ This exact URI is now registered and whitelisted.")
        print("   You can get this from chrome.runtime.id in your extension code:")
        print(f"   console.log('Extension ID:', chrome.runtime.id); // Should show: {YOUR_EXTENSION_ID}")


if __name__ == "__main__":
    try:
        asyncio.run(register_browser_extension_client())
    except Exception as e:
        print(f"❌ Error registering client: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
